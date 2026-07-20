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
from oauth_service import OAuthServiceError, get_valid_access_token
from proxy_service import get_session_proxy


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

# 邮件列表查询字段
LIST_SELECT = "id,subject,from,toRecipients,receivedDateTime,body"
# 单封邮件详情查询字段
DETAIL_SELECT = "id,subject,from,toRecipients,ccRecipients,bccRecipients,replyTo,receivedDateTime,body"

# 请求超时（秒）
GRAPH_TIMEOUT = 30
IMAP_TIMEOUT = 30
POP3_TIMEOUT = 30


class MailServiceError(Exception):
    def __init__(self, message: str, tag: str | None = None):
        super().__init__(message)
        self.message = message
        self.tag = tag


PRE_CONTENT_PATTERN = re.compile(r"^<pre[^>]*>([\s\S]*)</pre>$", re.IGNORECASE)


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
        return "<p>No content</p>"
    content_type = (body_obj.get("contentType") or "text").lower()
    content = body_obj.get("content") or ""
    if not content:
        return "<p>No content</p>"
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
    发起 Graph API 请求，自动处理 token 过期重试。
    遇到 401 时强制刷新 token 后重试一次。
    """
    proxies = get_session_proxy(db, account)
    headers = {
        "Prefer": 'outlook.body-content-type="html"',
    }

    for attempt in range(2):
        try:
            access_token = get_valid_access_token(account, db)
        except requests.HTTPError as exc:
            raise MailServiceError(f"token refresh failed: {exc}", tag="token_invalid") from exc
        except OAuthServiceError as exc:
            raise MailServiceError(f"token refresh failed: {exc}", tag="token_invalid") from exc

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

        if response.status_code == 401 and attempt == 0:
            # token 可能有效但作用域不匹配或已失效，强制刷新后重试
            # 记录旧 token 的实际 scope，便于诊断
            old_scope = _decode_jwt_scope(access_token)
            logger.warning(
                "邮箱 %s Graph API 返回 401，强制刷新 token 重试（旧 token scope: %s）",
                account.email, old_scope[:120] or "(无法解析)",
            )
            account.cached_access_token = ""
            account.access_token_expire_time = 0
            db.commit()
            continue

        if response.status_code == 401:
            # 两次都 401，说明这个 client_id 拿到的 token 不含 Mail.Read 权限
            token_scope = _decode_jwt_scope(access_token)
            # 读 Graph API 返回的错误信息
            graph_error = ""
            try:
                err_data = response.json()
                err_obj = err_data.get("error") or {}
                graph_error = err_obj.get("message") or str(err_data)[:200]
            except Exception:
                graph_error = response.text[:200]

            raise MailServiceError(
                f"graph token invalid after refresh（token 实际 scope: {token_scope[:80] or '未知'}, "
                f"Graph 错误: {graph_error[:120]}）。"
                f"可能原因：1) client_id 未在 Azure 注册 Mail.Read 权限；"
                f"2) refresh_token 已被用户撤销授权。"
                f"建议：在导入时把此账号的协议改为 imap 或 pop3（用邮箱密码取件）。",
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

        try:
            return response.json()
        except ValueError as exc:
            raise MailServiceError(f"graph response parse error: {exc}") from exc

    raise MailServiceError("graph api request failed after retry")


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
        items.append(
            {
                "id": msg.get("id", ""),
                "subject": msg.get("subject") or "",
                "mail_from": _format_graph_address(msg.get("from")),
                "mail_to": _format_graph_addresses(msg.get("toRecipients")),
                "mail_dt": _format_graph_datetime(msg.get("receivedDateTime")),
                "body": _extract_graph_body(msg.get("body")),
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
        return bool((account.refresh_token or "").strip() and (account.client_id or "").strip())
    if protocol in ("imap", "pop3"):
        return bool((account.password or "").strip())
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

    # 自动选择模式
    # 构造尝试顺序：上次成功的协议优先 + 标准 graph → imap → pop3
    if last_used and last_used in _PROTOCOL_CHAIN:
        # 上次成功的协议放第一个，其余按标准顺序
        chain = [last_used] + [p for p in _PROTOCOL_CHAIN if p != last_used]
    else:
        chain = list(_PROTOCOL_CHAIN)

    last_error: MailServiceError | None = None
    tried: list[str] = []

    for protocol in chain:
        # 跳过没有凭据的协议
        if not _can_use_protocol(protocol, account):
            logger.info(
                "邮箱 %s 跳过 %s 协议（缺少凭据）",
                account.email, protocol.upper(),
            )
            continue

        tried.append(protocol)
        logger.info(
            "邮箱 %s 尝试 %s 协议取件",
            account.email, protocol.upper(),
        )

        try:
            items = _load_by_protocol_name(protocol, account, db, folder=folder, limit=limit)
            # 成功 → 记录到 last_used_protocol（不修改 protocol 字段）
            if last_used != protocol:
                account.last_used_protocol = protocol
                # 清空旧的 token_invalid 标签
                _remove_tag(account, "token_invalid")
                _remove_tag(account, "imap_auth_failed")
                _remove_tag(account, "pop3_auth_failed")
                db.commit()
                logger.info(
                    "邮箱 %s %s 协议取件成功，已记录到 last_used_protocol",
                    account.email, protocol.upper(),
                )
            return items
        except MailServiceError as exc:
            last_error = exc
            logger.warning(
                "邮箱 %s %s 协议取件失败: %s",
                account.email, protocol.upper(), str(exc)[:150],
            )
            # 自动选择模式下：任何错误都继续尝试下一个协议
            continue

    # 所有协议都失败
    if last_error:
        raise last_error
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
) -> list[dict]:
    """按协议名分发到对应的取件函数"""
    if protocol == "imap":
        return load_imap_messages(account, db, folder=folder, limit=limit)
    if protocol == "pop3":
        return load_pop3_messages(account, db, folder=folder, limit=limit)
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
    """根据账号 protocol 选择对应取件方式。"""
    protocol = getattr(account, "protocol", None) or "graph"
    protocol = protocol.lower().strip()

    if protocol == "imap":
        return load_imap_messages(account, db, folder=folder, limit=limit)
    if protocol == "pop3":
        return load_pop3_messages(account, db, folder=folder, limit=limit)
    return load_mail_messages(account, db, folder=folder, limit=limit)


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


def _parse_imap_message(msg) -> dict:
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
        "id": (msg.get("Message-ID") or "")[:200],
        "subject": subject,
        "mail_from": mail_from,
        "mail_to": mail_to,
        "mail_dt": _parse_rfc2822_date(received),
        "body": body_html or "<p>No content</p>",
    }


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


def load_imap_messages(
    account: MailAccount,
    db: Session,
    folder: str = "inbox",
    limit: int = 20,
) -> list[dict]:
    """通过 IMAP 协议取件。
    - 如果账号有 refresh_token + client_id，优先用 OAuth2 access_token (XOAUTH2) 认证
    - 否则用邮箱密码认证
    两种方式自动切换，无需用户手动选择。
    """
    server, port, use_ssl = _resolve_imap_config(account)
    password = account.password or ""

    # 判定是否可用 OAuth2 access_token 认证
    use_oauth2 = bool(account.refresh_token and account.client_id)
    access_token: str | None = None

    if use_oauth2:
        try:
            access_token = get_valid_access_token(account, db)
        except OAuthServiceError as exc:
            # OAuth2 失败时若仍有密码，fallback 到密码认证
            if password:
                logger.warning(
                    "邮箱 %s IMAP OAuth2 取 token 失败，回退到密码认证: %s",
                    account.email, str(exc)[:150],
                )
                use_oauth2 = False
            else:
                raise MailServiceError(
                    f"IMAP OAuth2 令牌获取失败: {exc}",
                    tag="oauth_token_failed",
                ) from exc

    if not use_oauth2 and not password:
        raise MailServiceError(
            "IMAP 取件需要邮箱密码或 OAuth2 令牌（refresh_token + client_id），请补全其中之一",
            tag="auth_missing",
        )

    try:
        if use_ssl:
            mail = imaplib.IMAP4_SSL(host=server, port=port, timeout=IMAP_TIMEOUT)
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
                # XOAUTH2 失败时如果有密码，fallback 到密码
                if password:
                    logger.warning(
                        "邮箱 %s IMAP XOAUTH2 认证失败，回退到密码认证: %s",
                        account.email, str(exc)[:150],
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

        target_folder = _select_imap_folder(mail, folder)
        status, _data = mail.select(target_folder, readonly=True)
        if status != "OK":
            # 如果文件夹不存在，回退到 INBOX
            mail.select("INBOX", readonly=True)

        # 取最近 limit 封
        status, data = mail.search(None, "ALL")
        if status != "OK":
            return []

        ids = data[0].split() if data and data[0] else []
        if not ids:
            return []

        # 倒序后取前 limit 封
        recent_ids = ids[-limit:][::-1]

        items: list[dict] = []
        for mail_id in recent_ids:
            status, fetched = mail.fetch(mail_id, "(RFC822)")
            if status != "OK" or not fetched or not fetched[0]:
                continue
            try:
                raw = fetched[0][1]
                msg = email_lib.message_from_bytes(raw)
                items.append(_parse_imap_message(msg))
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
) -> list[dict]:
    """通过 POP3 协议取件（密码认证），POP3 仅支持收件箱。"""
    server, port, use_ssl = _resolve_pop3_config(account)
    password = account.password or ""

    if not password:
        raise MailServiceError(
            "POP3 取件需要邮箱密码，请在导入或备注中补全密码",
            tag="password_missing",
        )

    try:
        if use_ssl:
            pop = poplib.POP3_SSL(host=server, port=port, timeout=POP3_TIMEOUT)
        else:
            pop = poplib.POP3(host=server, port=port, timeout=POP3_TIMEOUT)
    except Exception as exc:
        raise MailServiceError(f"POP3 连接失败 ({server}:{port}): {exc}") from exc

    try:
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
        items: list[dict] = []
        for idx in range(total, start - 1, -1):
            try:
                resp, lines, _octets = pop.retr(idx)
                raw = b"\r\n".join(lines)
                msg = email_lib.message_from_bytes(raw)
                items.append(_parse_imap_message(msg))
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


def load_single_mail_with_protocol(
    account: MailAccount,
    db: Session,
    mail_id: str,
    folder: str = "inbox",
) -> dict | None:
    """根据协议获取单封邮件内容。"""
    protocol = (getattr(account, "protocol", None) or "auto").lower().strip()
    last_used = (getattr(account, "last_used_protocol", "") or "").lower().strip()
    effective_protocol = last_used if protocol == "auto" and last_used else protocol
    if effective_protocol == "graph":
        return load_single_mail(account, db, mail_id=mail_id, folder=folder)
    # IMAP / POP3 在列表里已经返回完整正文，按 id 找回
    items = load_account_mails_with_protocol(account, db, folder=folder, limit=50)
    for item in items:
        if item.get("id") == mail_id:
            return item
    return None
