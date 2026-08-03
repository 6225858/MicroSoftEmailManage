import html
import json
import logging
import re
import imaplib
import poplib
import email as email_lib
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header

import requests
from sqlalchemy.orm import Session

from models import MailAccount
from oauth_service import OAuthServiceError, get_valid_access_token, _is_msauth_token
from proxy_service import get_session_proxy, get_proxied_socket_factory


logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


GRAPH_BASE = "https://graph.microsoft.com/v1.0"
CHINA_TZ = timezone(timedelta(hours=8))

# Graph API 文件夹映射
FOLDER_MAP = {
    "inbox": "inbox",
    "junk": "junkemail",
}

# IMAP / POP3 默认服务器配置
IMAP_DEFAULT_SERVER = "outlook.office365.com"
IMAP_DEFAULT_PORT_SSL = 993
IMAP_DEFAULT_PORT_PLAIN = 143
POP3_DEFAULT_SERVER = "outlook.office365.com"
POP3_DEFAULT_PORT_SSL = 995
POP3_DEFAULT_PORT_PLAIN = 110

# IMAP 文件夹名映射（兼容大小写/中文/英文别名）
IMAP_FOLDER_ALIASES = {
    "inbox": ["inbox", "收件箱", "inbox"],
    "junk": ["junk", "junk email", "junkemail", "垃圾邮件", "垃圾箱"],
}

# 邮件列表查询字段（列表不需要正文，详情才拉取 body，避免无谓流量）
LIST_SELECT = "id,subject,from,toRecipients,receivedDateTime,isRead"
# 单封邮件详情查询字段
DETAIL_SELECT = "id,subject,from,toRecipients,ccRecipients,bccRecipients,replyTo,receivedDateTime,isRead,body"

# 请求超时（秒）
GRAPH_TIMEOUT = 30
IMAP_TIMEOUT = 10
POP3_TIMEOUT = 10


class MailServiceError(Exception):
    def __init__(self, message: str, tag: str | None = None):
        super().__init__(message)
        self.message = message
        self.tag = tag


SAFE_MAIL_LOG_TAGS = frozenset({
    "auth_missing",
    "imap_auth_failed",
    "missing_credentials_for_graph",
    "missing_credentials_for_imap",
    "missing_credentials_for_pop3",
    "no_available_protocol",
    "oauth_token_failed",
    "pop3_auth_failed",
    "token_invalid",
})


def safe_mail_error_tag(error: BaseException) -> str:
    tag = str(getattr(error, "tag", "") or "")
    return tag if tag in SAFE_MAIL_LOG_TAGS else "mail_service_error"


# 成功取件后应被清空的瞬态错误标签（避免脏标签一直残留）
TRANSIENT_ERROR_TAGS = SAFE_MAIL_LOG_TAGS

# 进程内记录已知 Graph 必然 401 的账号（如未识别的 M.C token），
# 后续取件跳过必败的 Graph 首试，避免每次都浪费一次 30s 超时。
_graph_401_accounts: set[int] = set()


def _mark_graph_401(account: MailAccount) -> None:
    account_id = getattr(account, "id", None)
    if account_id is not None:
        _graph_401_accounts.add(account_id)


def clear_graph_401(account: MailAccount) -> None:
    account_id = getattr(account, "id", None)
    if account_id is not None:
        _graph_401_accounts.discard(account_id)


def _account_log_id(account: MailAccount) -> str:
    account_id = getattr(account, "id", None)
    return str(account_id) if account_id is not None else "unknown"


PRE_CONTENT_PATTERN = re.compile(r"^<pre[^>]*>([\s\S]*)</pre>$", re.IGNORECASE)

# 取不到正文时的统一占位符。
# 注意：它只应出现在「确实拉取过正文但邮件本身没有正文」的场景；
# 列表取件（不请求 body 字段）必须留空字符串，否则占位符会被写进缓存，
# 导致详情接口误判为「已有正文」而永远不再补取真正的正文。
NO_CONTENT_PLACEHOLDER = "<p>No content</p>"


def is_body_missing(body: str | None) -> bool:
    """判断一封邮件的正文是否「尚未取到」。

    空字符串、纯空白、以及占位符 <p>No content</p> 都视为未取到，
    需要走详情接口真正拉取一次。缓存命中判断必须用这个函数，
    否则占位符会被当成有效正文永久命中脏缓存。
    """
    content = (body or "").strip()
    if not content:
        return True
    return content.casefold() == NO_CONTENT_PLACEHOLDER.casefold()


def _normalize_tags(tags: str) -> str:
    seen = []
    for item in tags.replace("\uff0c", ",").split(","):
        value = item.strip()
        if value and value not in seen:
            seen.append(value)
    return ",".join(seen)


def _add_tag(account: MailAccount, tag: str) -> None:
    account.tags = _normalize_tags(",".join(filter(None, [account.tags or "", tag])))


def _looks_like_escaped_html(value: str) -> bool:
    sample = value.strip().lower()
    return any(
        marker in sample
        for marker in (
            "&lt;!doctype",
            "&lt;html",
            "&lt;body",
            "&lt;table",
            "&lt;div",
            "&lt;section",
            "&lt;p",
        )
    )


def _extract_pre_content(value: str) -> str:
    match = PRE_CONTENT_PATTERN.match(value)
    return match.group(1) if match else ""


def get_mail_body_render_mode(body: str | None) -> dict[str, str]:
    content = (body or "").strip()
    if not content:
        return {
            "type": "inline",
            "content": "<p>No content</p>",
        }

    if content.lower().startswith("<pre"):
        pre_content = _extract_pre_content(content)
        if pre_content and _looks_like_escaped_html(pre_content):
            return {
                "type": "iframe",
                "content": html.unescape(pre_content),
            }
        return {
            "type": "inline",
            "content": content,
        }

    return {
        "type": "iframe",
        "content": html.unescape(content) if _looks_like_escaped_html(content) else content,
    }


def _format_graph_datetime(value: str | None) -> str:
    """将 Graph API 返回的 ISO 8601 时间转换为北京时间字符串。"""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        dt = dt.astimezone(CHINA_TZ)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _format_graph_address(from_obj: dict | None) -> str:
    """格式化 Graph API 的邮箱地址对象为 'Name (address)' 格式。"""
    if not from_obj:
        return ""
    email_addr = from_obj.get("emailAddress") or {}
    name = (email_addr.get("name") or "").strip()
    address = (email_addr.get("address") or "").strip()
    if name and address:
        return f"{name} ({address})"
    return address or ""


def _format_graph_addresses(recipients: list | None) -> str:
    """格式化 Graph API 的收件人列表为逗号分隔的字符串。"""
    if not recipients:
        return ""
    addrs = []
    for r in recipients:
        email_addr = r.get("emailAddress") or {}
        name = (email_addr.get("name") or "").strip()
        address = (email_addr.get("address") or "").strip()
        if name and address:
            addrs.append(f"{name} ({address})")
        elif address:
            addrs.append(address)
    return ", ".join(addrs)


def _extract_graph_body(body_obj: dict | None) -> str:
    """从 Graph API 的 body 对象提取邮件正文 HTML。"""
    if not body_obj:
        return NO_CONTENT_PLACEHOLDER
    content_type = (body_obj.get("contentType") or "text").lower()
    content = body_obj.get("content") or ""
    if not content:
        return NO_CONTENT_PLACEHOLDER
    if content_type == "html":
        return content
    # 纯文本正文转义后包裹在 <pre> 标签中
    return f"<pre>{html.escape(content)}</pre>"


def _decode_jwt_scope(access_token: str) -> str:
    """从 JWT access_token 中解析 scope（不验证签名，仅供诊断）"""
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return ""
        # JWT 第二段是 payload，base64url 编码
        import base64
        payload_b64 = parts[1]
        # 补齐 base64 padding
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        payload = json.loads(payload_bytes)
        return str(payload.get("scp", "") or payload.get("scope", "") or "")
    except Exception:
        return ""


def _graph_request(
    url: str,
    account: MailAccount,
    db: Session,
    params: dict | None = None,
    method: str = "GET",
) -> dict:
    """
    发起 Graph API 请求。
    遇到 401 时直接抛出错误（不清空 token 缓存），让协议选择链 fallback 到 IMAP。
    之前 401 会强制刷新 token 重试，但 M.C 格式 token 刷新后还是同样的 scope，
    重试注定 401，且清空缓存导致 IMAP 需要重新刷新 token，浪费时间。
    """
    proxies = get_session_proxy(db, account)
    headers = {
        "Prefer": 'outlook.body-content-type="html"',
    }

    try:
        access_token = get_valid_access_token(account, db, required_scope="graph")
    except requests.HTTPError as exc:
        raise MailServiceError(f"token refresh failed: {exc}", tag="token_invalid") from exc
    except OAuthServiceError as exc:
        raise MailServiceError(f"token refresh failed: {exc}", tag="token_invalid") from exc
    except Exception as exc:  # noqa: BLE001
        # 任何非预期异常（网络/解析等）也必须转成 MailServiceError，
        # 否则会穿透成 unexpected_error 且不触发协议回退到 IMAP
        raise MailServiceError(
            f"token refresh failed: {type(exc).__name__}: {exc}", tag="token_invalid"
        ) from exc

    headers["Authorization"] = f"Bearer {access_token}"

    try:
        response = requests.request(
            method,
            url,
            headers=headers,
            params=params,
            proxies=proxies,
            timeout=GRAPH_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise MailServiceError(f"graph request network error: {exc}") from exc

    if response.status_code == 401:
        # token 不含 Mail.Read 权限或已失效
        # 不清空缓存（IMAP 可以复用这个 token 做 XOAUTH2 认证）
        # 直接抛出错误，让协议选择链 fallback 到 IMAP
        token_scope = _decode_jwt_scope(access_token)
        graph_error = ""
        try:
            err_data = response.json()
            err_obj = err_data.get("error") or {}
            graph_error = err_obj.get("message") or str(err_data)[:200]
        except Exception:
            graph_error = response.text[:200]

        logger.info(
            "邮箱 account=%s Graph API 401，error=token_invalid，将 fallback 到 IMAP",
            _account_log_id(account),
        )
        # 记录该账号 Graph 必然失败，后续自动取件跳过 Graph 首试
        _mark_graph_401(account)
        raise MailServiceError(
            f"Graph API 401（token scope: {token_scope[:80] or '未知'}, "
            f"错误: {graph_error[:120]}），将尝试 IMAP/POP3",
            tag="token_invalid",
        )

    if not response.ok:
        error_detail = ""
        try:
            error_data = response.json()
            error_obj = error_data.get("error") or {}
            error_detail = error_obj.get("message") or str(error_data)[:300]
        except Exception:
            error_detail = response.text[:300]
        raise MailServiceError(
            f"graph api error: HTTP {response.status_code}: {error_detail}"
        )

    clear_graph_401(account)

    try:
        return response.json()
    except ValueError as exc:
        raise MailServiceError(f"graph response parse error: {exc}") from exc


def load_mail_messages(
    account: MailAccount,
    db: Session,
    folder: str = "inbox",
    limit: int = 20,
) -> list[dict]:
    """通过 Graph API 加载邮件列表。"""
    graph_folder = FOLDER_MAP.get(folder, "inbox")
    url = f"{GRAPH_BASE}/me/mailFolders/{graph_folder}/messages"
    params = {
        "$top": min(limit, 100),
        "$orderby": "receivedDateTime desc",
        "$select": LIST_SELECT,
    }

    data = _graph_request(url, account, db, params=params)
    items = []
    for msg in data.get("value") or []:
        # LIST_SELECT 不包含 body 字段，msg["body"] 必然缺失。
        # 这里绝不能调 _extract_graph_body(None)——它会返回占位符
        # "<p>No content</p>"，一旦写进缓存，详情接口就会把它当成
        # 「已有正文」永久命中，真正的正文再也不会被拉取。
        # 与 IMAP 列表保持一致：留空字符串，由详情接口按需补取。
        body = _extract_graph_body(msg["body"]) if msg.get("body") else ""
        items.append(
            {
                "id": msg.get("id", ""),
                "subject": msg.get("subject") or "",
                "mail_from": _format_graph_address(msg.get("from")),
                "mail_to": _format_graph_addresses(msg.get("toRecipients")),
                "mail_dt": _format_graph_datetime(msg.get("receivedDateTime")),
                "is_read": bool(msg.get("isRead")),
                "body": body,
            }
        )
    return items


def list_account_folders(
    account: MailAccount,
    db: Session,
) -> list[dict[str, str]]:
    """通过 Graph API 获取邮箱的所有文件夹列表。"""
    url = f"{GRAPH_BASE}/me/mailFolders"
    params = {
        "$top": 50,
        "$select": "id,displayName,totalItemCount,unreadItemCount",
    }

    data = _graph_request(url, account, db, params=params)
    items = []
    for folder in data.get("value") or []:
        items.append(
            {
                "name": folder.get("displayName") or "",
                "raw_name": folder.get("id") or "",
                "flags": "",
            }
        )
    return items


def load_single_mail(
    account: MailAccount,
    db: Session,
    mail_id: str,
    folder: str = "inbox",
) -> dict | None:
    """通过 Graph API 获取单封邮件的完整内容（含所有头部和正文）。"""
    # 对 mail_id 做 URL 编码以处理特殊字符
    encoded_id = requests.utils.quote(mail_id, safe="")
    url = f"{GRAPH_BASE}/me/messages/{encoded_id}"
    params = {
        "$select": DETAIL_SELECT,
    }

    msg = _graph_request(url, account, db, params=params)
    if not msg:
        return None

    return {
        "id": msg.get("id", ""),
        "subject": msg.get("subject") or "",
        "mail_from": _format_graph_address(msg.get("from")),
        "mail_to": _format_graph_addresses(msg.get("toRecipients")),
        "cc": _format_graph_addresses(msg.get("ccRecipients")),
        "bcc": _format_graph_addresses(msg.get("bccRecipients")),
        "reply_to": _format_graph_addresses(msg.get("replyTo")),
        "mail_dt": _format_graph_datetime(msg.get("receivedDateTime")),
        "is_read": bool(msg.get("isRead")),
        "body": _extract_graph_body(msg.get("body")),
    }


def load_account_mails(
    account: MailAccount,
    db: Session,
    folder: str = "inbox",
    limit: int = 20,
) -> list[dict]:
    """加载邮箱邮件，失败时自动添加标签。

    支持两种模式：
    1. 自动选择（protocol='auto'，默认）：每次取件按 graph → imap → pop3 顺序尝试，
       第一个成功的就用，不修改 protocol 字段，用 last_used_protocol 字段记录
       上次成功的协议（下次优先尝试它，避免每次都从头尝试）
    2. 手动指定（protocol='graph'/'imap'/'pop3'）：按指定协议取件
    """
    try:
        return _load_with_protocol_selection(account, db, folder=folder, limit=limit)
    except MailServiceError as exc:
        if exc.tag:
            _add_tag(account, exc.tag)
            db.commit()
        raise


# 协议尝试顺序（自动选择模式）
_PROTOCOL_CHAIN = ["graph", "imap", "pop3"]


def _can_use_protocol(protocol: str, account: MailAccount) -> bool:
    """检查该协议是否可执行（有相应凭据）"""
    if protocol == "graph":
        has_creds = bool((account.refresh_token or "").strip() and (account.client_id or "").strip())
        if not has_creds:
            logger.warning(
                "邮箱 account=%s Graph 协议不可用: refresh_token=%s, client_id=%s",
                _account_log_id(account),
                "有" if (account.refresh_token or "").strip() else "空",
                "有" if (account.client_id or "").strip() else "空",
            )
        return has_creds
    if protocol in ("imap", "pop3"):
        # 密码认证或 OAuth2(XOAUTH2) 认证都可取件，二选一即可
        has_password = bool((account.password or "").strip())
        has_oauth = bool(
            (account.refresh_token or "").strip() and (account.client_id or "").strip()
        )
        return has_password or has_oauth
    return False


def _load_with_protocol_selection(
    account: MailAccount,
    db: Session,
    folder: str = "inbox",
    limit: int = 20,
) -> list[dict]:
    """
    按账号 protocol 字段选择策略：
    - 'auto'：自动选择，按 graph → imap → pop3 顺序尝试
    - 其他：按指定协议取件，失败抛错
    """
    current_protocol = (getattr(account, "protocol", None) or "auto").lower().strip()
    last_used = (getattr(account, "last_used_protocol", "") or "").lower().strip()

    # 手动指定模式：直接按指定协议取件
    if current_protocol in ("graph", "imap", "pop3"):
        if not _can_use_protocol(current_protocol, account):
            raise MailServiceError(
                f"协议 {current_protocol.upper()} 不可用：缺少必要凭据",
                tag=f"missing_credentials_for_{current_protocol}",
            )
        return _load_by_protocol_name(current_protocol, account, db, folder=folder, limit=limit)

    # 自动选择模式 —— OAuth2 登录优先
    # ------------------------------------------------------------------
    # 设计目标：凡具备 OAuth2 凭据(refresh_token + client_id)的账号，
    # 一律优先尝试 OAuth2 认证(XOAUTH2 / Graph)，仅在 OAuth2 链全部失败后
    # 才回退到纯密码认证。不再允许“上次成功的密码协议”被顶到 OAuth2 之前。
    #
    # 认证类型与协议映射：
    #   • OAuth2 链(graph → imap → pop3)：均走 XOAUTH2 / Graph(Bearer)
    #   • 密码链(imap → pop3)：走基础密码认证
    # 注意 imap/pop3 在两条链里都可能出现，但认证方式不同，需分开尝试。
    has_oauth = bool((account.refresh_token or "").strip() and (account.client_id or "").strip())

    def _order_oauth2(base):
        # OAuth2 链内部排序：优先上次成功的 OAuth2 协议，再按 graph→imap→pop3
        if last_used and last_used in base:
            return [last_used] + [p for p in base if p != last_used]
        return list(base)

    def _apply_graph_optimizations(chain_in):
        # M.C / 个人版 token 没有 Mail.Read，Graph 必 401 → 跳过 Graph
        if _is_msauth_token(account.refresh_token):
            chain_in = [p for p in ("imap", "pop3", "graph") if p in chain_in]
        # 已知账号 Graph 必然 401 → Graph 移到最后
        account_id = getattr(account, "id", None)
        if account_id is not None and account_id in _graph_401_accounts and "graph" in chain_in:
            chain_in = [p for p in chain_in if p != "graph"] + ["graph"]
        return chain_in

    if has_oauth:
        # OAuth2 优先：先跑 OAuth2 链(graph → imap → pop3)
        oauth_chain = _order_oauth2(_PROTOCOL_CHAIN)
        oauth_chain = _apply_graph_optimizations(oauth_chain)
        # 密码链仅作为 OAuth2 全部失败后的兜底(imap → pop3)
        pwd_chain = [p for p in ("imap", "pop3") if p in _PROTOCOL_CHAIN]
        # 上次成功的“密码协议”在密码链内部优先
        if last_used and last_used in pwd_chain:
            pwd_chain = [last_used] + [p for p in pwd_chain if p != last_used]
        chain = ("oauth2", oauth_chain, pwd_chain)
    else:
        # 无 OAuth2 凭据 → 直接走密码链(上次成功的密码协议优先)
        if last_used and last_used in _PROTOCOL_CHAIN:
            pwd_chain = [last_used] + [p for p in _PROTOCOL_CHAIN if p != last_used]
        else:
            pwd_chain = list(_PROTOCOL_CHAIN)
        chain = ("password", [], pwd_chain)

    # 标记：OAuth2 链是否已全部尝试完毕，用于衔接密码链
    _oauth_done = False
    last_error: MailServiceError | None = None
    tried: list[str] = []
    errors: list[str] = []  # 收集所有协议的失败原因

    def _try_protocol(protocol: str, oauth2_only: bool):
        """尝试单个协议；成功则返回邮件列表（由调用方决定是否 return），失败记录错误后返回 None。"""
        # 跳过没有凭据的协议
        if not _can_use_protocol(protocol, account):
            logger.info(
                "邮箱 account=%s 跳过 %s 协议（缺少凭据）",
                _account_log_id(account), protocol.upper(),
            )
            return None

        # 优化：如果 IMAP 连接超时，跳过 POP3
        # IMAP 和 POP3 连接同一个服务器 outlook.office365.com，
        # IMAP 超时说明 IP 被限制，POP3 也会超时，跳过可节省 10 秒
        if protocol == "pop3" and errors and "timed out" in errors[-1].lower():
            logger.info(
                "邮箱 account=%s IMAP 连接超时，跳过 POP3（同一服务器也会超时）",
                _account_log_id(account),
            )
            errors.append("POP3: 跳过（IMAP 连接超时，同一服务器也会超时）")
            return None

        tried.append(protocol)
        logger.info(
            "邮箱 account=%s 尝试 %s 协议取件%s",
            _account_log_id(account), protocol.upper(),
            "（OAuth2 专用）" if oauth2_only else "",
        )

        try:
            items = _load_by_protocol_name(
                protocol, account, db, folder=folder, limit=limit, oauth2_only=oauth2_only
            )
            # 成功 → 记录到 last_used_protocol（不修改 protocol 字段）
            if last_used != protocol:
                account.last_used_protocol = protocol
                # 成功取件后清空所有瞬态错误标签（避免脏标签残留）
                for err_tag in TRANSIENT_ERROR_TAGS:
                    _remove_tag(account, err_tag)
                db.commit()
                logger.info(
                    "邮箱 account=%s %s 协议取件成功，已记录到 last_used_protocol",
                    _account_log_id(account), protocol.upper(),
                )
            return items
        except MailServiceError as exc:
            nonlocal last_error
            last_error = exc
            errors.append(f"{protocol.upper()}: {exc.message}")
            logger.warning(
                "邮箱 account=%s %s 协议取件失败: error=%s",
                _account_log_id(account), protocol.upper(), safe_mail_error_tag(exc),
            )
            # 自动选择模式下：任何错误都继续尝试下一个协议
            return None

    # ── 阶段一：OAuth2 链（graph → imap → pop3，均走 XOAUTH2/Bearer）──
    #    仅当账号具备 OAuth2 凭据时执行；OAuth2 失败不回退密码。
    #    任意一个 OAuth2 协议成功即返回；全失败则进入阶段二密码兜底。
    oauth_chain = chain[1] if isinstance(chain, tuple) else []
    pwd_chain = chain[2] if isinstance(chain, tuple) else list(chain)
    if isinstance(chain, tuple) and chain[0] == "oauth2":
        for protocol in oauth_chain:
            result = _try_protocol(protocol, oauth2_only=True)
            if result is not None:
                return result
        # 阶段一全部失败 → 进入下方阶段二密码兜底（last_error 已记录）

    # ── 阶段二：密码链（imap → pop3，走基础密码认证）──
    #    OAuth2 全部失败后的兜底；无 OAuth2 凭据的账号直接走这里。
    for protocol in pwd_chain:
        result = _try_protocol(protocol, oauth2_only=False)
        if result is not None:
            return result

    if last_error:
        # 构建详细的诊断信息
        detail = "\n".join(f"  • {e}" for e in errors)
        suggestions = []
        email_domain = (account.email or "").split("@")[-1].lower()
        is_outlook = any(d in email_domain for d in ["outlook.", "hotmail.", "live.", "msn."])

        # 检查是否有连接超时错误（IP 限制）
        has_timeout = any("timed out" in e.lower() or "timeout" in e.lower() for e in errors)
        # 检查 Graph API 是否被尝试
        graph_tried = "graph" in tried

        if has_timeout and is_outlook:
            suggestions.append(
                "Outlook/Hotmail 的 IMAP/POP3 服务器(outlook.office365.com)连接超时，"
                "很可能是因为服务器 IP 被微软限制。请在「代理管理」中配置 SOCKS5 或 HTTP 代理"
            )
        if is_outlook and not graph_tried:
            suggestions.append(
                "Graph API 未被尝试，请确认账号已正确导入 refresh_token 和 client_id"
            )
        if is_outlook:
            suggestions.append("Outlook/Hotmail 邮箱已禁用基本密码认证，必须使用有效的 refresh_token")
        if account.refresh_token and any("token" in e.lower() or "oauth" in e.lower() for e in errors):
            suggestions.append("refresh_token 可能已过期或无效，请重新获取并更新")
        if not account.refresh_token:
            suggestions.append("未配置 refresh_token + client_id，仅靠密码可能无法通过认证")
        if not suggestions:
            suggestions.append("请检查邮箱密码是否正确，或网络连接是否正常")

        suggestion_text = "\n建议：\n" + "\n".join(f"  {i+1}. {s}" for i, s in enumerate(suggestions))
        raise MailServiceError(
            f"所有取件协议均失败（尝试了 {len(tried)} 个：{', '.join(t.upper() for t in tried)}）：\n{detail}{suggestion_text}",
            tag=last_error.tag,
        )
    # 没有任何协议可尝试（缺凭据）
    raise MailServiceError(
        f"无可尝试的取件协议（已尝试: {tried or '无'}）",
        tag="no_available_protocol",
    )


def _load_by_protocol_name(
    protocol: str,
    account: MailAccount,
    db: Session,
    folder: str = "inbox",
    limit: int = 20,
    oauth2_only: bool = False,
) -> list[dict]:
    """按协议名分发到对应的取件函数。
    oauth2_only=True 时，imap/pop3 强制仅 XOAUTH2 认证（OAuth2 优先策略专用）。
    """
    if protocol == "imap":
        return load_imap_messages(account, db, folder=folder, limit=limit, oauth2_only=oauth2_only)
    if protocol == "pop3":
        return load_pop3_messages(account, db, folder=folder, limit=limit, oauth2_only=oauth2_only)
    return load_mail_messages(account, db, folder=folder, limit=limit)


def _remove_tag(account: MailAccount, tag: str) -> None:
    """移除指定 tag"""
    if not tag:
        return
    current_tags = [t.strip() for t in (account.tags or "").split(",") if t.strip()]
    if tag in current_tags:
        current_tags.remove(tag)
        account.tags = ",".join(current_tags)


# ────────── 保留向后兼容的旧函数（不再使用，但保留避免外部调用报错）──────────
def _should_fallback_to_next_protocol(current_protocol: str, exc: MailServiceError) -> bool:
    """已废弃：自动选择模式下所有错误都会触发下一个协议尝试"""
    return True


def _load_with_protocol_fallback(
    account: MailAccount,
    db: Session,
    folder: str = "inbox",
    limit: int = 20,
) -> list[dict]:
    """已废弃：保留向后兼容，内部调用 _load_with_protocol_selection"""
    return _load_with_protocol_selection(account, db, folder=folder, limit=limit)


def load_account_mails_with_protocol(
    account: MailAccount,
    db: Session,
    folder: str = "inbox",
    limit: int = 20,
) -> list[dict]:
    """根据账号 protocol 选择对应取件方式。
    protocol='auto' 时委托给自动选择逻辑（按 last_used_protocol 优先），
    避免把 auto 当成缺失而退化为 Graph；其余按指定协议取件。
    """
    protocol = (getattr(account, "protocol", None) or "auto").lower().strip()

    if protocol == "auto":
        return load_account_mails(account, db, folder=folder, limit=limit)
    if protocol == "imap":
        # OAuth2 优先：账号具备 OAuth2 凭据时先以 XOAUTH2 专用方式尝试，
        # 失败（如 token 过期且无密码兜底）再回退到密码认证。
        if account.refresh_token and account.client_id:
            try:
                return load_imap_messages(account, db, folder=folder, limit=limit, oauth2_only=True)
            except MailServiceError:
                pass
        return load_imap_messages(account, db, folder=folder, limit=limit)
    if protocol == "pop3":
        if account.refresh_token and account.client_id:
            try:
                return load_pop3_messages(account, db, folder=folder, limit=limit, oauth2_only=True)
            except MailServiceError:
                pass
        return load_pop3_messages(account, db, folder=folder, limit=limit)
    # 未知 / 显式 graph 协议统一走 Graph
    return load_mail_messages(account, db, folder=folder, limit=limit)


def _resolve_effective_protocol(account: MailAccount) -> str:
    """返回实际生效的取件协议（auto 模式以 last_used_protocol 为准）。"""
    protocol = (getattr(account, "protocol", None) or "auto").lower().strip()
    if protocol == "auto":
        last_used = (getattr(account, "last_used_protocol", "") or "").lower().strip()
        return last_used or "imap"
    return protocol


def list_mail_refs(account: MailAccount, db: Session, folder: str, limit: int) -> list[dict]:
    """列出最近 limit 封邮件的元数据(不含正文)，供增量刷新比对哪些是新邮件。

    复用现有的列表取件逻辑：IMAP/Graph 列表本就只取元数据（正文在打开单封时按需拉取），
    POP3 列表本身含正文。返回结构与 load_account_mails 一致。
    """
    return load_account_mails(account, db, folder=folder, limit=limit)


def fetch_mail_full(
    account: MailAccount, db: Session, folder: str, ref: dict
) -> dict | None:
    """根据元数据引用补取单封邮件的完整正文（增量刷新时对"新邮件"调用）。

    - graph/imap：按 ref["id"] 精准拉取正文；
    - pop3：列表已含正文，直接复用 ref，无需额外请求。
    """
    mail_id = ref.get("id") or ""
    protocol = _resolve_effective_protocol(account)
    # pop3 的列表本身就含正文，直接复用 ref，无需额外请求
    if protocol == "pop3":
        return ref
    detail = _load_single_by_protocol(protocol, account, db, mail_id, folder)
    # 补取失败/正文为空时退回元数据 ref，避免把 None 写进缓存
    if detail is None:
        return ref
    if is_body_missing(detail.get("body")) and not is_body_missing(ref.get("body")):
        return ref
    return detail


def merge_incremental_mails(
    account: MailAccount,
    db: Session,
    folder: str,
    limit: int,
    existing_items: list[dict],
) -> tuple[list[dict], int]:
    """增量合并最近邮件：已缓存的复用(含正文)，新邮件补取正文。

    返回 (合并后的邮件列表, 新邮件数量)。合并结果按服务端顺序(最新在前)，截断到 limit。
    已缓存的邮件（其正文可能来自上一次打开时写回的缓存）直接复用，避免重复下载正文，
    因此"刷新"只会对真正新增的邮件取正文，显著提速。
    """
    refs = list_mail_refs(account, db, folder, limit) or []
    existing_map: dict[str, dict] = {}
    for item in existing_items or []:
        if item.get("id"):
            existing_map[item["id"]] = item
        if item.get("message_id"):
            existing_map[item["message_id"]] = item
    merged: list[dict] = []
    new_count = 0
    for ref in refs:  # refs 为最新在前
        mid = ref.get("id")
        cached = existing_map.get(mid) or existing_map.get(ref.get("message_id"))
        if cached is not None:
            item = dict(ref)
            if not is_body_missing(cached.get("body")):
                item["body"] = cached.get("body")
                for extra in ("html", "attachments"):
                    if cached.get(extra) is not None:
                        item[extra] = cached.get(extra)
            merged.append(item)
            continue
        merged.append(ref)
        new_count += 1
    if len(merged) > limit:
        merged = merged[:limit]
    return merged, new_count


# ──────────────────────────── IMAP 取件 ────────────────────────────


def _resolve_imap_config(account: MailAccount) -> tuple[str, int, bool]:
    server = (account.mail_server or "").strip() or IMAP_DEFAULT_SERVER
    port = int(account.mail_port or 0)
    use_ssl = (account.mail_use_ssl if account.mail_use_ssl is not None else 1) == 1
    if port <= 0:
        port = IMAP_DEFAULT_PORT_SSL if use_ssl else IMAP_DEFAULT_PORT_PLAIN
    return server, port, use_ssl


def _decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value or ""


def _format_email_address_list(values: list) -> str:
    if not values:
        return ""
    parts = []
    for item in values:
        name = _decode_mime_header(getattr(item, "header_name", None) or "")
        addr = getattr(item, "addr_spec", "") or str(item)
        if name and addr:
            parts.append(f"{name} ({addr})")
        elif addr:
            parts.append(addr)
    return ", ".join(parts)


def _parse_imap_message(
    msg,
    *,
    mail_id: str | None = None,
    is_read: bool | None = None,
) -> dict:
    subject = _decode_mime_header(msg.get("Subject", ""))
    from_field = msg.get("From", "")
    to_field = msg.get("To", "")
    received = msg.get("Date", "")

    from_addresses = email_lib.utils.getaddresses([from_field])
    to_addresses = email_lib.utils.getaddresses([to_field])

    mail_from = ", ".join(
        f"{name} ({addr})" if name else addr
        for name, addr in from_addresses
        if addr
    )
    mail_to = ", ".join(
        f"{name} ({addr})" if name else addr
        for name, addr in to_addresses
        if addr
    )

    body_html = _extract_email_body(msg)
    return {
        "id": mail_id or msg.get("Message-ID") or "",
        "message_id": msg.get("Message-ID") or "",
        "subject": subject,
        "mail_from": mail_from,
        "mail_to": mail_to,
        "mail_dt": _parse_rfc2822_date(received),
        "is_read": bool(is_read),
        "body": body_html or "<p>No content</p>",
    }


def _imap_uidvalidity(mail) -> str:
    try:
        _code, values = mail.response("UIDVALIDITY")
        if values and values[0]:
            value = values[0]
            return value.decode("ascii", errors="ignore") if isinstance(value, bytes) else str(value)
    except Exception:
        pass
    return "0"


def _imap_public_id(uidvalidity: str, uid: str) -> str:
    return f"imap:{uidvalidity}:{uid}"


def _parse_imap_public_id(mail_id: str) -> tuple[str, str] | None:
    match = re.fullmatch(r"imap:([^:]+):(\d+)", str(mail_id or ""))
    return (match.group(1), match.group(2)) if match else None


def _extract_email_body(msg) -> str:
    """优先返回 HTML 正文，没有则返回纯文本。"""
    html_part = None
    text_part = None

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = (part.get_content_disposition() or "").lower()
            if disposition == "attachment":
                continue
            if content_type == "text/html" and html_part is None:
                html_part = part
            elif content_type == "text/plain" and text_part is None:
                text_part = part
    else:
        content_type = msg.get_content_type()
        if content_type == "text/html":
            html_part = msg
        elif content_type == "text/plain":
            text_part = msg

    if html_part is not None:
        payload = _decode_payload(html_part)
        if payload:
            return payload

    if text_part is not None:
        payload = _decode_payload(text_part)
        if payload:
            return f"<pre>{html.escape(payload)}</pre>"

    return "<p>No content</p>"


def _decode_payload(part) -> str:
    try:
        charset = part.get_content_charset() or "utf-8"
        payload = part.get_payload(decode=True)
        if payload is None:
            content = part.get_payload()
            if isinstance(content, str):
                return content
            return ""
        try:
            return payload.decode(charset, errors="replace")
        except (LookupError, TypeError):
            return payload.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _parse_rfc2822_date(value: str) -> str:
    if not value:
        return ""
    try:
        dt = email_lib.utils.parsedate_to_datetime(value)
        if dt is None:
            return ""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return value


def _select_imap_folder(mail: imaplib.IMAP4_SSL, folder: str) -> str:
    """根据 inbox/junk 找到真实文件夹名。"""
    candidates = IMAP_FOLDER_ALIASES.get(folder, [folder])
    typ, data = mail.list()
    available = []
    if typ == "OK" and data:
        for item in data:
            if not item:
                continue
            try:
                parts = item.decode().split('"/"')
            except Exception:
                continue
            if parts:
                name = parts[-1].strip().strip('"').lower()
                available.append(name)

    for candidate in candidates:
        if candidate.lower() in available:
            return candidate
        # 大小写不敏感比较
        for real_name in available:
            if real_name.lower() == candidate.lower():
                return real_name

    # 兜底：直接用原始名
    return candidates[0]


def _build_xoauth2_auth_string(user: str, access_token: str) -> str:
    """构造 IMAP XOAUTH2 认证字符串（SASL）。
    格式: user=<user>\x01auth=Bearer <token>\x01\x01
    """
    return f"user={user}\x01auth=Bearer {access_token}\x01\x01"


# ── 带代理支持的 IMAP/POP3 子类 ──
# 重写 _create_socket 方法，通过代理创建 TCP socket
# SSL 包装由父类的 open() 方法自动完成

class _ProxiedIMAP4_SSL(imaplib.IMAP4_SSL):
    """支持代理的 IMAP4_SSL"""
    def __init__(self, *args, socket_factory=None, **kwargs):
        self._proxy_factory = socket_factory
        super().__init__(*args, **kwargs)

    def _create_socket(self, timeout=None):
        if self._proxy_factory:
            return self._proxy_factory(self.host, self.port, timeout)
        return super()._create_socket(timeout)


class _ProxiedIMAP4(imaplib.IMAP4):
    """支持代理的非 SSL IMAP4"""
    def __init__(self, *args, socket_factory=None, **kwargs):
        self._proxy_factory = socket_factory
        super().__init__(*args, **kwargs)

    def _create_socket(self, timeout=None):
        if self._proxy_factory:
            return self._proxy_factory(self.host, self.port, timeout)
        return super()._create_socket(timeout)


class _ProxiedPOP3_SSL(poplib.POP3_SSL):
    """支持代理的 POP3_SSL"""
    def __init__(self, *args, socket_factory=None, **kwargs):
        self._proxy_factory = socket_factory
        super().__init__(*args, **kwargs)

    def _create_socket(self, timeout):
        if self._proxy_factory:
            return self._proxy_factory(self.host, self.port, timeout)
        return super()._create_socket(timeout)


class _ProxiedPOP3(poplib.POP3):
    """支持代理的非 SSL POP3"""
    def __init__(self, *args, socket_factory=None, **kwargs):
        self._proxy_factory = socket_factory
        super().__init__(*args, **kwargs)

    def _create_socket(self, timeout):
        if self._proxy_factory:
            return self._proxy_factory(self.host, self.port, timeout)
        return super()._create_socket(timeout)


def get_imap_conn(account: MailAccount, db: Session, oauth2_only: bool = False):
    """建立并返回已认证的 IMAP 连接（含 SSL/代理/OAuth2(XOAUTH2) 或密码认证）。

    抽取自 load_imap_messages，供单封取件复用，避免重复连接逻辑。
    oauth2_only=True 时：强制仅使用 XOAUTH2 认证，OAuth2 失败直接抛错，
    绝不回退到密码认证（用于 OAuth2 优先策略下对 imap 协议的专用尝试）。
    """
    server, port, use_ssl = _resolve_imap_config(account)
    password = account.password or ""

    # 判定是否可用 OAuth2 access_token 认证
    use_oauth2 = bool(account.refresh_token and account.client_id)
    access_token: str | None = None

    if use_oauth2:
        try:
            access_token = get_valid_access_token(account, db, required_scope="imap")
        except OAuthServiceError as exc:
            # OAuth2 失败时若仍有密码，fallback 到密码认证（非 oauth2_only 模式）
            if password and not oauth2_only:
                logger.warning(
                    "邮箱 account=%s IMAP OAuth2 取 token 失败，回退到密码认证: error=oauth_token_failed",
                    _account_log_id(account),
                )
                use_oauth2 = False
            else:
                raise MailServiceError(
                    f"IMAP OAuth2 令牌获取失败: {exc}",
                    tag="oauth_token_failed",
                ) from exc
        except Exception as exc:  # noqa: BLE001
            # 任何非 OAuthServiceError 的异常（网络错误/解析错误等）也必须转成
            # MailServiceError，否则会穿透成 unexpected_error 且不触发协议回退
            if password and not oauth2_only:
                logger.warning(
                    "邮箱 account=%s IMAP 取 token 异常，回退到密码认证: error=%s",
                    _account_log_id(account), type(exc).__name__,
                )
                use_oauth2 = False
            else:
                raise MailServiceError(
                    f"IMAP 令牌获取异常: {type(exc).__name__}: {exc}",
                    tag="oauth_token_failed",
                ) from exc

    if not use_oauth2 and not password:
        raise MailServiceError(
            "IMAP 取件需要邮箱密码或 OAuth2 令牌（refresh_token + client_id），请补全其中之一",
            tag="auth_missing",
        )

    # 获取代理 socket 工厂（如果有可用代理）
    proxy_factory = get_proxied_socket_factory(db)
    if proxy_factory:
        logger.info("邮箱 account=%s IMAP 使用代理连接 %s:%s", _account_log_id(account), server, port)

    try:
        if use_ssl:
            if proxy_factory:
                mail = _ProxiedIMAP4_SSL(host=server, port=port, timeout=IMAP_TIMEOUT, socket_factory=proxy_factory)
            else:
                mail = imaplib.IMAP4_SSL(host=server, port=port, timeout=IMAP_TIMEOUT)
        else:
            if proxy_factory:
                mail = _ProxiedIMAP4(host=server, port=port, timeout=IMAP_TIMEOUT, socket_factory=proxy_factory)
            else:
                mail = imaplib.IMAP4(host=server, port=port, timeout=IMAP_TIMEOUT)
    except Exception as exc:
        raise MailServiceError(f"IMAP 连接失败 ({server}:{port}): {exc}") from exc

    try:
        # 认证
        if use_oauth2 and access_token:
            auth_string = _build_xoauth2_auth_string(account.email, access_token)
            try:
                # imaplib 的 authenticate 第二参数是回调，回调接收 token 字节并返回 SASL 响应
                mail.authenticate("XOAUTH2", lambda _x: auth_string.encode("utf-8"))
            except imaplib.IMAP4.error as exc:
                # XOAUTH2 失败时如果有密码且非 oauth2_only，fallback 到密码
                if password and not oauth2_only:
                    logger.warning(
                        "邮箱 account=%s IMAP XOAUTH2 认证失败，回退到密码认证: error=imap_auth_failed",
                        _account_log_id(account),
                    )
                    mail.login(account.email, password)
                else:
                    raise MailServiceError(
                        f"IMAP XOAUTH2 login failed: {exc}",
                        tag="imap_auth_failed",
                    ) from exc
        else:
            try:
                mail.login(account.email, password)
            except imaplib.IMAP4.error as exc:
                raise MailServiceError(
                    f"IMAP login failed: {exc}",
                    tag="imap_auth_failed",
                ) from exc
    except Exception:
        # 认证失败要释放连接
        try:
            mail.logout()
        except Exception:
            pass
        raise

    return mail


def load_imap_messages(
    account: MailAccount,
    db: Session,
    folder: str = "inbox",
    limit: int = 20,
    oauth2_only: bool = False,
) -> list[dict]:
    """通过 IMAP 协议取件。
    - 如果账号有 refresh_token + client_id，优先用 OAuth2 access_token (XOAUTH2) 认证
    - 否则用邮箱密码认证
    两种方式自动切换，无需用户手动选择。
    oauth2_only=True 时强制仅 XOAUTH2 认证，失败即抛错（OAuth2 优先策略专用）。
    """
    mail = get_imap_conn(account, db, oauth2_only=oauth2_only)
    try:
        target_folder = _select_imap_folder(mail, folder)
        status, _data = mail.select(target_folder, readonly=True)
        if status != "OK":
            # 如果文件夹不存在，回退到 INBOX
            mail.select("INBOX", readonly=True)

        # 取最近 limit 封（按序号倒序，最新在前）
        try:
            status, data = mail.uid("search", None, "ALL")
        except imaplib.IMAP4.abort:
            return []
        if status != "OK" or not data or not data[0]:
            return []

        ids = data[0].split() if data and data[0] else []
        if not ids:
            return []
        recent_ids = ids[-limit:][::-1]
        uidvalidity = _imap_uidvalidity(mail)

        # 优化：列表阶段只批量拉取邮件头（BODY.PEEK[HEADER]），一次网络往返拿全部，
        # 避免逐封拉取完整 RFC822（含附件）带来的多轮往返与带宽浪费；
        # 正文在打开单封邮件时（load_single_mail_with_protocol）再按需拉取。
        ids_csv = b",".join(recent_ids).decode()
        try:
            status, chunks = mail.uid("fetch", ids_csv, "(BODY.PEEK[HEADER] FLAGS)")
        except imaplib.IMAP4.abort:
            return []
        if status != "OK" or not chunks:
            return []

        items: list[dict] = []
        for item in chunks:
            # fetch 多封时返回结构为 [(b'N FETCH (...)', bytes), b')', ...]，
            # 仅处理含正文数据的元组
            if not isinstance(item, tuple) or len(item) != 2:
                continue
            try:
                meta = item[0] if isinstance(item[0], bytes) else str(item[0]).encode()
                uid_match = re.search(rb"\bUID\s+(\d+)\b", meta)
                if not uid_match:
                    continue
                uid = uid_match.group(1).decode("ascii")
                flags_match = re.search(rb"FLAGS\s+\(([^)]*)\)", meta, re.IGNORECASE)
                flags = flags_match.group(1).lower() if flags_match else b""
                msg = email_lib.message_from_bytes(item[1])
                parsed = _parse_imap_message(
                    msg,
                    mail_id=_imap_public_id(uidvalidity, uid),
                    is_read=b"\\seen" in flags,
                )
                # 列表不返回正文，减小体积；单封取件时才拉完整正文
                parsed["body"] = ""
                items.append(parsed)
            except Exception:
                continue

        return items
    finally:
        try:
            mail.logout()
        except Exception:
            pass


# ──────────────────────────── POP3 取件 ────────────────────────────


def _resolve_pop3_config(account: MailAccount) -> tuple[str, int, bool]:
    server = (account.mail_server or "").strip() or POP3_DEFAULT_SERVER
    port = int(account.mail_port or 0)
    use_ssl = (account.mail_use_ssl if account.mail_use_ssl is not None else 1) == 1
    if port <= 0:
        port = POP3_DEFAULT_PORT_SSL if use_ssl else POP3_DEFAULT_PORT_PLAIN
    return server, port, use_ssl


def load_pop3_messages(
    account: MailAccount,
    db: Session,
    folder: str = "inbox",
    limit: int = 20,
    oauth2_only: bool = False,
) -> list[dict]:
    """通过 POP3 协议取件。
    - 如果账号有 refresh_token + client_id，优先用 OAuth2 access_token (XOAUTH2) 认证
    - 否则用邮箱密码认证
    两种方式自动切换，无需用户手动选择。
    POP3 仅支持收件箱。
    oauth2_only=True 时强制仅 XOAUTH2 认证，失败即抛错（OAuth2 优先策略专用）。
    """
    server, port, use_ssl = _resolve_pop3_config(account)
    password = account.password or ""

    # 判定是否可用 OAuth2 access_token 认证
    use_oauth2 = bool(account.refresh_token and account.client_id)
    access_token: str | None = None

    if use_oauth2:
        try:
            access_token = get_valid_access_token(account, db, required_scope="imap")
        except OAuthServiceError as exc:
            # OAuth2 失败时若仍有密码且非 oauth2_only，fallback 到密码认证
            if password and not oauth2_only:
                logger.warning(
                    "邮箱 account=%s POP3 OAuth2 取 token 失败，回退到密码认证: error=oauth_token_failed",
                    _account_log_id(account),
                )
                use_oauth2 = False
            else:
                raise MailServiceError(
                    f"POP3 OAuth2 令牌获取失败: {exc}",
                    tag="oauth_token_failed",
                ) from exc
        except Exception as exc:  # noqa: BLE001
            if password and not oauth2_only:
                logger.warning(
                    "邮箱 account=%s POP3 取 token 异常，回退到密码认证: error=%s",
                    _account_log_id(account), type(exc).__name__,
                )
                use_oauth2 = False
            else:
                raise MailServiceError(
                    f"POP3 令牌获取异常: {type(exc).__name__}: {exc}",
                    tag="oauth_token_failed",
                ) from exc

    if not use_oauth2 and not password:
        raise MailServiceError(
            "POP3 取件需要邮箱密码或 OAuth2 令牌（refresh_token + client_id），请补全其中之一",
            tag="auth_missing",
        )

    # 获取代理 socket 工厂（如果有可用代理）
    proxy_factory = get_proxied_socket_factory(db)
    if proxy_factory:
        logger.info("邮箱 account=%s POP3 使用代理连接 %s:%s", _account_log_id(account), server, port)

    try:
        if use_ssl:
            if proxy_factory:
                pop = _ProxiedPOP3_SSL(host=server, port=port, timeout=POP3_TIMEOUT, socket_factory=proxy_factory)
            else:
                pop = poplib.POP3_SSL(host=server, port=port, timeout=POP3_TIMEOUT)
        else:
            if proxy_factory:
                pop = _ProxiedPOP3(host=server, port=port, timeout=POP3_TIMEOUT, socket_factory=proxy_factory)
            else:
                pop = poplib.POP3(host=server, port=port, timeout=POP3_TIMEOUT)
    except Exception as exc:
        raise MailServiceError(f"POP3 连接失败 ({server}:{port}): {exc}") from exc

    try:
        # 认证
        if use_oauth2 and access_token:
            try:
                # POP3 XOAUTH2 认证:发送 AUTH XOAUTH2 <base64(auth_string)>
                import base64
                auth_string = f"user={account.email}\x01auth=Bearer {access_token}\x01\x01"
                encoded_auth = base64.b64encode(auth_string.encode("utf-8")).decode("ascii")
                pop._shortcmd(f"AUTH XOAUTH2 {encoded_auth}")
                logger.info("邮箱 account=%s POP3 XOAUTH2 认证成功", _account_log_id(account))
            except poplib.error_proto as exc:
                # XOAUTH2 失败:服务器可能返回 continuation response(+ <base64_error>)
                # 必须发送空行清除 continuation 状态,否则连接处于脏状态
                try:
                    pop.sock.sendall(b"\r\n")
                    pop.file.readline()
                except Exception:
                    pass

                if not password or oauth2_only:
                    raise MailServiceError(
                        f"POP3 XOAUTH2 login failed: {exc}",
                        tag="pop3_auth_failed",
                    ) from exc

                # 有密码 → 关闭旧连接,重新连接后用密码认证
                # (不能在旧连接上直接 USER/PASS,因为 XOAUTH2 失败后连接状态已脏)
                logger.warning(
                    "邮箱 account=%s POP3 XOAUTH2 认证失败(error=pop3_auth_failed),重新连接后用密码认证",
                    _account_log_id(account),
                )
                try:
                    pop.quit()
                except Exception:
                    pass
                try:
                    if use_ssl:
                        if proxy_factory:
                            pop = _ProxiedPOP3_SSL(host=server, port=port, timeout=POP3_TIMEOUT, socket_factory=proxy_factory)
                        else:
                            pop = poplib.POP3_SSL(host=server, port=port, timeout=POP3_TIMEOUT)
                    else:
                        if proxy_factory:
                            pop = _ProxiedPOP3(host=server, port=port, timeout=POP3_TIMEOUT, socket_factory=proxy_factory)
                        else:
                            pop = poplib.POP3(host=server, port=port, timeout=POP3_TIMEOUT)
                except Exception as conn_exc:
                    raise MailServiceError(
                        f"POP3 重新连接失败: {conn_exc}",
                        tag="pop3_auth_failed",
                    ) from conn_exc
                try:
                    pop.user(account.email)
                    pop.pass_(password)
                    logger.info("邮箱 account=%s POP3 密码认证成功", _account_log_id(account))
                except poplib.error_proto as exc2:
                    raise MailServiceError(
                        f"POP3 login failed: {exc2}",
                        tag="pop3_auth_failed",
                    ) from exc2
        else:
            try:
                pop.user(account.email)
                pop.pass_(password)
            except poplib.error_proto as exc:
                raise MailServiceError(
                    f"POP3 login failed: {exc}",
                    tag="pop3_auth_failed",
                ) from exc

        # POP3 没有 "junk" 文件夹概念
        stat = pop.stat()
        total = stat[0]
        if total == 0:
            return []

        start = max(1, total - limit + 1)
        uidl_by_index: dict[int, str] = {}
        try:
            _resp, uidl_lines, _octets = pop.uidl()
            for line in uidl_lines:
                number, uidl = line.decode("utf-8", errors="replace").split(None, 1)
                uidl_by_index[int(number)] = uidl.strip()
        except Exception:
            uidl_by_index = {}
        items: list[dict] = []
        for idx in range(total, start - 1, -1):
            try:
                resp, lines, _octets = pop.retr(idx)
                raw = b"\r\n".join(lines)
                msg = email_lib.message_from_bytes(raw)
                uidl = uidl_by_index.get(idx)
                public_id = f"pop3:{uidl}" if uidl else f"pop3-index:{idx}"
                items.append(_parse_imap_message(msg, mail_id=public_id))
            except Exception:
                continue

        return items
    finally:
        try:
            pop.quit()
        except Exception:
            pass


def list_account_folders_with_protocol(
    account: MailAccount,
    db: Session,
) -> list[dict[str, str]]:
    """根据协议获取文件夹列表。"""
    protocol = (getattr(account, "protocol", None) or "auto").lower().strip()
    last_used = (getattr(account, "last_used_protocol", "") or "").lower().strip()
    # auto 模式下，按 last_used_protocol 决定展示
    effective_protocol = last_used if protocol == "auto" and last_used else protocol
    if effective_protocol == "graph":
        return list_account_folders(account, db)
    # IMAP / POP3 仅展示收件箱与垃圾箱（POP3 实际只有收件箱）
    return [
        {"name": "Inbox", "raw_name": "inbox", "flags": ""},
        {"name": "Junk", "raw_name": "junk", "flags": ""},
    ]


def _load_single_pop3_mail(
    account: MailAccount, db: Session, mail_id: str, folder: str
) -> dict | None:
    """POP3 不支持按 Message-ID 检索，只能拉列表后匹配（列表本身含正文）。"""
    # OAuth2 优先：先以 XOAUTH2 专用方式尝试，失败再密码兜底
    if account.refresh_token and account.client_id:
        try:
            items = load_pop3_messages(account, db, folder=folder, limit=100, oauth2_only=True)
        except MailServiceError:
            items = load_pop3_messages(account, db, folder=folder, limit=100)
    else:
        items = load_pop3_messages(account, db, folder=folder, limit=100)
    for item in items or []:
        if item.get("id") == mail_id:
            return item
    return None


def _load_single_by_protocol(
    protocol: str, account: MailAccount, db: Session, mail_id: str, folder: str
) -> dict | None:
    """按指定协议拉取单封邮件的完整内容（含正文）。协议不认识时返回 None。"""
    if protocol == "graph":
        return load_single_mail(account, db, mail_id=mail_id, folder=folder)
    if protocol == "imap":
        return _load_single_imap_mail(account, db, mail_id, folder)
    if protocol == "pop3":
        return _load_single_pop3_mail(account, db, mail_id, folder)
    return None


def load_single_mail_with_protocol(
    account: MailAccount,
    db: Session,
    mail_id: str,
    folder: str = "inbox",
) -> dict | None:
    """根据协议获取单封邮件内容（务必带回正文）。

    关键点：graph / imap 的「列表」只取元数据、不含正文，所以这里必须走
    单封详情接口。历史实现在 protocol='auto' 且 last_used_protocol 为空时
    会穿透到 load_account_mails 兜底，直接把不含正文的「列表项」当详情返回，
    这正是「邮件详情里无内容」的主因。现在的做法：
      1. 能确定协议 → 直接按该协议取单封；
      2. 取回来正文仍然缺失（或协议未知）→ 跑一次自动协议选择，
         借此确定真正可用的协议，再用它补取一次单封正文。
    """
    protocol = (getattr(account, "protocol", None) or "auto").lower().strip()
    last_used = (getattr(account, "last_used_protocol", "") or "").lower().strip()
    effective_protocol = last_used if protocol == "auto" and last_used else protocol

    fallback_detail: dict | None = None

    if effective_protocol in ("graph", "imap", "pop3"):
        try:
            detail = _load_single_by_protocol(
                effective_protocol, account, db, mail_id, folder
            )
        except MailServiceError as exc:
            # 指定协议取件失败（token 失效 / 连接超时等）：
            # auto 模式下不应直接报错，下面还要再试一次自动协议选择
            if protocol != "auto":
                raise
            logger.info(
                "邮箱 account=%s 按 %s 协议取单封失败，改走自动协议选择: error=%s",
                _account_log_id(account), effective_protocol.upper(),
                safe_mail_error_tag(exc),
            )
            detail = None
        if detail is not None and not is_body_missing(detail.get("body")):
            return detail
        fallback_detail = detail
        # 非 auto（用户手动指定协议）→ 按用户意图返回，不再自作主张换协议
        if protocol != "auto":
            return detail
        # auto 模式且已知协议（last_used 已指明）成功取回了内容（即便正文缺失），
        # 单封接口已尽力，不再重跑自动发现——graph/imap 列表取件本就不含正文，
        # 重跑只会再次拿到无正文的列表项。仅当协议取件彻底失败（detail 为 None）时才继续兜底。
        if last_used and detail is not None:
            return detail

    # ── auto 模式兜底 ──
    # 走一次自动协议选择：它内部会按 OAuth2/密码链逐个尝试，
    # 成功后把可用协议写进 account.last_used_protocol。
    try:
        items = load_account_mails(account, db, folder=folder, limit=50)
    except MailServiceError:
        # 自动选择也失败了：若前面已拿到元数据就先返回，否则如实抛错
        if fallback_detail is not None:
            return fallback_detail
        raise
    matched = None
    for item in items or []:
        if item.get("id") == mail_id:
            matched = item
            break

    # POP3 列表自带正文，命中即可直接用
    if matched is not None and not is_body_missing(matched.get("body")):
        return matched

    # graph / imap 列表不含正文 → 用刚探测出来的协议真正补取单封
    resolved = _resolve_effective_protocol(account)
    if resolved and resolved != effective_protocol:
        try:
            detail = _load_single_by_protocol(resolved, account, db, mail_id, folder)
        except MailServiceError as exc:
            logger.warning(
                "邮箱 account=%s 自动协议 %s 补取单封正文失败: error=%s",
                _account_log_id(account), resolved.upper(), safe_mail_error_tag(exc),
            )
            detail = None
        if detail is not None and not is_body_missing(detail.get("body")):
            return detail
        if detail is not None:
            fallback_detail = detail

    # 仍拿不到正文：返回能拿到的最完整结果（至少带主题/发件人等元数据）
    return fallback_detail or matched


def _load_single_imap_mail(
    account: MailAccount, db: Session, mail_id: str, folder: str
) -> dict | None:
    """优先按 UID 定位单封 IMAP 邮件；兼容旧缓存的 Message-ID。"""
    # OAuth2 优先：账号具备 OAuth2 凭据时先以 XOAUTH2 专用方式连接，
    # 失败再回退到密码认证（get_imap_conn 默认 oauth2_only=False）。
    if account.refresh_token and account.client_id:
        try:
            mail = get_imap_conn(account, db, oauth2_only=True)
        except MailServiceError:
            mail = get_imap_conn(account, db)
    else:
        mail = get_imap_conn(account, db)
    try:
        target_folder = _select_imap_folder(mail, folder)
        try:
            mail.select(target_folder, readonly=True)
        except imaplib.IMAP4.abort:
            return None

        parsed_id = _parse_imap_public_id(mail_id)
        if parsed_id is not None:
            expected_uidvalidity, uid_text = parsed_id
            if expected_uidvalidity != _imap_uidvalidity(mail):
                return None
            uid = uid_text.encode("ascii")
        else:
            try:
                status, data = mail.uid("search", None, "HEADER", "Message-ID", mail_id)
            except imaplib.IMAP4.abort:
                return None
            if status != "OK" or not data or not data[0]:
                return None
            uids = data[0].split()
            if not uids:
                return None
            uid = uids[-1]
        try:
            status, fetched = mail.uid("fetch", uid, "(BODY.PEEK[] FLAGS)")
        except imaplib.IMAP4.abort:
            return None
        if status != "OK" or not fetched or not fetched[0] or not fetched[0][1]:
            return None
        try:
            msg = email_lib.message_from_bytes(fetched[0][1])
            meta = fetched[0][0] if isinstance(fetched[0][0], bytes) else b""
            flags_match = re.search(rb"FLAGS\s+\(([^)]*)\)", meta, re.IGNORECASE)
            flags = flags_match.group(1).lower() if flags_match else b""
            return _parse_imap_message(
                msg,
                mail_id=mail_id,
                is_read=b"\\seen" in flags,
            )
        except Exception:
            return None
    finally:
        try:
            mail.logout()
        except Exception:
            pass

