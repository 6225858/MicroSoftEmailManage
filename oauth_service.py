import time
import logging
import threading
from typing import Optional

import requests
from sqlalchemy.orm import Session

from models import MailAccount, MailRefreshTokenHistory
from proxy_service import get_session_proxy


TOKEN_URL_CONSUMER = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
TOKEN_URL_COMMON = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
MSAUTH_TOKEN_URL = "https://login.live.com/oauth20_token.srf"
# token 缓存时间（fallback，当 OAuth 响应不含 expires_in 时使用）
# MSAuth 端点返回的 token 有效期通常为 1 小时（3600 秒），
# 缓存 50 分钟留 10 分钟缓冲，避免使用即将过期的 token
TOKEN_CACHE_SECONDS = 50 * 60

# Graph API 端点用 Graph scope（标准 v2.0 端点）
# MSAuth 端点（login.live.com）必须用 wl.* 格式的旧 scope，否则会返回
# "The request was denied because one or more scopes requested are unauthorized or expired"
GRAPH_SCOPE = "https://graph.microsoft.com/Mail.Read offline_access"
# MSAuth 专用 scope（旧版 wl.* 格式，对应 live.com 端点）
# wl.imap = IMAP 访问，wl.basic = 基础资料，wl.offline_access = 离线访问
# 这是 M.C/M.R 格式 refresh_token 必须使用的 scope 格式
MSAUTH_SCOPE = "wl.imap wl.basic wl.offline_access"
# 备选 scope：如果 wl.imap 失败，用更宽松的 wl.basic（仅基础资料 + 离线访问）
# 这种情况下拿到的 access_token 不能直接调 Graph API，但能完成基础认证
MSAUTH_SCOPE_FALLBACK = "wl.basic wl.offline_access"
# 组织账号 IMAP/POP3 XOAUTH2 需要的 scope（标准 v2.0 端点，与 Graph 的 Mail.Read 不同）
IMAP_SCOPES = "https://outlook.office.com/IMAP.AccessAsUser.All https://outlook.office.com/POP.AccessAsUser.All offline_access"
IMAP_SCOPES_RELAXED = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"

MSAUTH_TOKEN_PREFIXES = ("M.C", "M.R", "EwA", "EwB")


# ══════════════════════════════════════════════════════════════════════════
# 三种标准取件模式（新GR / 老GR / IMAP-OAuth2）
# ──────────────────────────────────────────────────────────────────────────
# 市面上流通的 Hotmail/Outlook refresh_token 按其注册应用授权的 scope 不同，
# 分为三类，必须用「匹配的 scope」去 consumers 端点换 access_token，
# 用错 scope 会直接被 Azure AD 拒绝（invalid_grant / unauthorized scope）。
#
#   • 新GR ：client_id 注册时授权了 Mail.Read 委托权限，用短格式 scope。
#            适用于近年流通的 Graph 取件 token，可直接调 Graph API。
#   • 老GR ：client_id 授权范围写死在应用注册里，用 .default 让 Azure AD
#            返回该应用「全部已授权 scope」。注意 .default 不能与其它
#            scope 混用（会报 AADSTS28000），因此这里单独发送、不带
#            offline_access。
#   • IMAP ：只授权了 Outlook IMAP 委托权限的 token，拿到的 access_token
#            不能调 Graph，只能用于 IMAP/POP3 的 XOAUTH2 认证。
#
# 三者统一走 consumers 端点（个人版微软账号），按上述顺序自动兜底。
# ══════════════════════════════════════════════════════════════════════════

OAUTH_MODE_NEW_GR = "new_gr"
OAUTH_MODE_OLD_GR = "old_gr"
OAUTH_MODE_IMAP = "imap_oauth2"

# 各模式对应的 scope（与参考实现逐字对齐，不要随意增删）
NEW_GR_SCOPE = "User.Read Mail.Read offline_access"
OLD_GR_SCOPE = "https://graph.microsoft.com/.default"
IMAP_OAUTH_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"

# 模式定义表：scope / 端点 / 该模式产出的 token 属于哪个用途槽位
OAUTH_MODE_SPECS: dict[str, dict] = {
    OAUTH_MODE_NEW_GR: {
        "label": "新GR",
        "scope": NEW_GR_SCOPE,
        "token_url": TOKEN_URL_CONSUMER,
        "slot": "graph",
    },
    OAUTH_MODE_OLD_GR: {
        "label": "老GR",
        "scope": OLD_GR_SCOPE,
        "token_url": TOKEN_URL_CONSUMER,
        "slot": "graph",
    },
    OAUTH_MODE_IMAP: {
        "label": "IMAP",
        "scope": IMAP_OAUTH_SCOPE,
        "token_url": TOKEN_URL_CONSUMER,
        "slot": "imap",
    },
}

# Graph 用途的模式尝试顺序（新GR 优先，其次老GR）
GRAPH_MODE_CHAIN = (OAUTH_MODE_NEW_GR, OAUTH_MODE_OLD_GR)
# IMAP/POP3 用途的模式尝试顺序
IMAP_MODE_CHAIN = (OAUTH_MODE_IMAP,)
# 统一入口 auto_get_token 的完整兜底顺序：新GR → 老GR → IMAP
AUTO_MODE_CHAIN = (OAUTH_MODE_NEW_GR, OAUTH_MODE_OLD_GR, OAUTH_MODE_IMAP)


def get_oauth_mode_label(mode: str) -> str:
    """返回模式的中文名，用于日志与前端展示。"""
    spec = OAUTH_MODE_SPECS.get(mode or "")
    return spec["label"] if spec else "未知"


logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


_OAUTH_ENDPOINT_CATEGORIES = {
    TOKEN_URL_CONSUMER: "consumer",
    TOKEN_URL_COMMON: "common",
    MSAUTH_TOKEN_URL: "msauth",
    "token_store": "token_store",
    "all": "all",
}
_OAUTH_LOG_TAGS = frozenset({
    "all_endpoints_failed",
    "fallback_to_msauth",
    "http_error",
    "missing_mail_read",
    "network_retry",
    "provider_error",
    "refresh_attempt",
    "refresh_failed",
    "refresh_succeeded",
    "token_rotated",
})


def _oauth_log(level: int, *, account_id, endpoint: str, attempt: int, tag: str) -> None:
    endpoint_category = _OAUTH_ENDPOINT_CATEGORIES.get(endpoint, "all")
    safe_tag = tag if tag in _OAUTH_LOG_TAGS else "refresh_failed"
    try:
        safe_account_id = int(account_id)
    except (TypeError, ValueError):
        safe_account_id = 0
    try:
        safe_attempt = max(0, int(attempt))
    except (TypeError, ValueError):
        safe_attempt = 0
    logger.log(
        level,
        "oauth account_id=%d endpoint=%s attempt=%d tag=%s",
        safe_account_id,
        endpoint_category,
        safe_attempt,
        safe_tag,
    )


class OAuthServiceError(Exception):
    pass


# 网络重试:遇到瞬时网络错误(连接重置、超时、代理不可用)自动重试 2 次,
# 间隔递增(1s, 2s)。永久性错误(4xx)由调用方处理。
_NETWORK_RETRYABLE_EXC = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


def _post_with_retry(
    url: str,
    *,
    data,
    timeout: int,
    proxies=None,
    retries: int = 2,
    account_id=0,
):
    """带瞬时网络错误重试的 POST 请求。

    只对连接错误 / 超时 / 代理瞬时不可用重试,不对 HTTP 4xx/5xx 重试
    (这些由上层根据具体状态码处理)。
    """
    last_exc: Optional[requests.RequestException] = None
    for attempt in range(retries + 1):
        try:
            return requests.post(url, data=data, timeout=timeout, proxies=proxies)
        except _NETWORK_RETRYABLE_EXC as exc:
            last_exc = exc
            if attempt < retries:
                _oauth_log(
                    logging.INFO,
                    account_id=account_id,
                    endpoint=url,
                    attempt=attempt + 1,
                    tag="network_retry",
                )
                sleep_sec = 1.0 * (attempt + 1)
                time.sleep(sleep_sec)
                continue
            raise
    # 理论上不会到这里
    if last_exc:  # pragma: no cover
        raise last_exc


def _sanitize_token(token: str) -> str:
    if not token:
        return ""
    return token.strip().replace("\r", "").replace("\n", "")


def _is_msauth_token(token: str) -> bool:
    cleaned = _sanitize_token(token)
    return any(cleaned.startswith(prefix) for prefix in MSAUTH_TOKEN_PREFIXES)


def _try_oauth2_refresh(
    token_url: str,
    account: MailAccount,
    proxies: dict | None,
    relax_scope_check: bool = False,
    candidate_scopes: list | None = None,
    require_scope_keyword: str | None = None,
) -> dict:
    """
    标准 OAuth2 端点刷新。不同 client_id 注册时授权的 scope 不同，
    因此按以下顺序尝试，第一个成功【且 token 实际含 Mail.Read 权限】的就返回：

    1. Graph Mail.Read scope（首选，client_id 已授权 Graph 时直接用）
    2. .default + offline_access（用应用注册时的所有 scope）
    3. 不传 scope（用 refresh_token 原始 scope，最通用）

    关键修复：之前会接受任何刷新成功的 token，但有些 scope（如 'openid profile email'）
    拿到的 token 没有 Mail.Read 权限，调 Graph API 会 401，导致 "graph token invalid after refresh"。
    现在通过 OAuth2 响应中的 scope 字段验证 token 是否真的含 Mail.Read。

    relax_scope_check=True 时不验证 Mail.Read（用于 M.C 格式 token）：
    某些 MSA 应用的 client_id 能通过标准端点刷新出含 Mail.Read 的 token，
    但 OAuth2 响应的 scope 字段可能不明确列出 "mail.read"，
    严格验证会错误拒绝有效 token，改为让 Graph API 自行验证。
    """
    cleaned_token = _sanitize_token(account.refresh_token)

    # 多 scope fallback 顺序（删掉了 'openid profile email offline_access'，
    # 因为它拿到的 token 一定不含 Mail.Read）
    candidate_scopes = candidate_scopes or [
        GRAPH_SCOPE,
        "https://graph.microsoft.com/.default offline_access",
        None,  # 不传 scope 参数，让 Azure AD 用 refresh_token 默认 scope
    ]

    last_error: OAuthServiceError | None = None

    for attempt, scope in enumerate(candidate_scopes, start=1):
        request_data = {
            "client_id": account.client_id,
            "grant_type": "refresh_token",
            "refresh_token": cleaned_token,
        }
        if scope:
            request_data["scope"] = scope

        try:
            response = _post_with_retry(
                token_url,
                data=request_data,
                timeout=20,
                proxies=proxies,
                account_id=account.id,
            )
        except requests.RequestException as exc:
            last_error = OAuthServiceError(f"network error: {exc}")
            _oauth_log(
                logging.WARNING,
                account_id=account.id,
                endpoint=token_url,
                attempt=attempt,
                tag="refresh_failed",
            )
            continue

        if not response.ok:
            error_detail = ""
            try:
                error_payload = response.json()
                error_detail = error_payload.get("error_description") or error_payload.get("error") or ""
            except Exception:
                error_detail = response.text[:300]
            last_error = OAuthServiceError(
                f"HTTP {response.status_code}: {error_detail} (endpoint: {token_url})"
            )
            _oauth_log(
                logging.DEBUG,
                account_id=account.id,
                endpoint=token_url,
                attempt=attempt,
                tag="http_error",
            )
            continue

        payload = response.json()
        if payload.get("error"):
            last_error = OAuthServiceError(
                payload.get("error_description") or payload["error"]
            )
            _oauth_log(
                logging.WARNING,
                account_id=account.id,
                endpoint=token_url,
                attempt=attempt,
                tag="provider_error",
            )
            continue

        if not payload.get("access_token"):
            last_error = OAuthServiceError("token response missing access_token")
            _oauth_log(
                logging.WARNING,
                account_id=account.id,
                endpoint=token_url,
                attempt=attempt,
                tag="provider_error",
            )
            continue

        # 关键：验证返回的 token 实际包含所需权限
        # OAuth2 响应的 scope 字段表示实际授予的 scope
        granted_scope = str(payload.get("scope", "")).lower()
        if require_scope_keyword:
            has_required = require_scope_keyword in granted_scope
        else:
            has_required = "mail.read" in granted_scope or "mail.readwrite" in granted_scope

        if not has_required and not relax_scope_check:
            # 严格模式：token 缺少所需权限，跳过
            _oauth_log(
                logging.WARNING,
                account_id=account.id,
                endpoint=token_url,
                attempt=attempt,
                tag="missing_required_scope",
            )
            last_error = OAuthServiceError(
                f"刷新成功但 token 缺少所需权限（实际 scope: {granted_scope[:100]}）"
            )
            continue

        # 成功
        _oauth_log(
            logging.INFO,
            account_id=account.id,
            endpoint=token_url,
            attempt=attempt,
            tag="refresh_succeeded",
        )
        return payload

    raise last_error or OAuthServiceError("unknown OAuth2 refresh error")


# ══════════════════════════════════════════════════════════════════════════
# 三种取件模式的具体实现
# ══════════════════════════════════════════════════════════════════════════


def _payload_granted_scope(payload: dict) -> str:
    return str(payload.get("scope", "") or "").lower()


def _scope_has_graph_mail(payload: dict) -> bool:
    """判断该 payload 的 token 是否真的具备 Graph 读信权限。

    .default 模式下 Azure AD 会把应用注册时授权的全部 scope 原样返回，
    因此这里做关键字包含判断即可。部分端点不返回 scope 字段（空串），
    此时无法判定，交由调用方按「宽松」策略处理。
    """
    granted = _payload_granted_scope(payload)
    return "mail.read" in granted or "mail.readwrite" in granted


def _scope_has_imap(payload: dict) -> bool:
    """判断该 payload 的 token 是否具备 IMAP/POP3 XOAUTH2 权限。"""
    granted = _payload_granted_scope(payload)
    return "imap" in granted or "pop" in granted or "wl.imap" in granted


def request_token_by_mode(
    account: MailAccount,
    mode: str,
    proxies: dict | None = None,
) -> dict:
    """按指定模式（新GR / 老GR / IMAP）换取 access_token。

    对应参考实现中的 getNewGRToken / getOldGRToken / getImapToken 三个函数：
    统一 POST 到 consumers 端点，区别只在 scope。成功返回完整的 OAuth2
    响应 payload（含 access_token / refresh_token / expires_in / scope），
    失败抛出 OAuthServiceError。
    """
    spec = OAUTH_MODE_SPECS.get(mode)
    if not spec:
        raise OAuthServiceError(f"未知的取件模式: {mode}")

    label = spec["label"]
    cleaned_token = _sanitize_token(account.refresh_token)
    client_id = (account.client_id or "").strip()
    if not cleaned_token:
        raise OAuthServiceError(f"{label} 模式缺少 refresh_token")
    if not client_id:
        raise OAuthServiceError(f"{label} 模式缺少 client_id")

    request_data = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": cleaned_token,
        "scope": spec["scope"],
    }

    try:
        response = _post_with_retry(
            spec["token_url"],
            data=request_data,
            timeout=20,
            proxies=proxies,
            account_id=account.id,
        )
    except requests.RequestException as exc:
        raise OAuthServiceError(f"{label} 模式网络错误: {exc}") from exc

    if not response.ok:
        detail = ""
        try:
            error_payload = response.json()
            detail = (
                error_payload.get("error_description")
                or error_payload.get("error")
                or ""
            )
        except Exception:
            detail = response.text[:300]
        raise OAuthServiceError(f"{label} 模式 HTTP {response.status_code}: {detail}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise OAuthServiceError(f"{label} 模式响应解析失败: {exc}") from exc

    if payload.get("error"):
        raise OAuthServiceError(
            f"{label} 模式返回错误: "
            f"{payload.get('error_description') or payload['error']}"
        )
    if not payload.get("access_token"):
        raise OAuthServiceError(f"{label} 模式响应缺少 access_token")

    return payload


def get_new_gr_token(account: MailAccount, proxies: dict | None = None) -> dict:
    """新GR 专用：scope = User.Read Mail.Read offline_access"""
    return request_token_by_mode(account, OAUTH_MODE_NEW_GR, proxies)


def get_old_gr_token(account: MailAccount, proxies: dict | None = None) -> dict:
    """老GR 专用：scope = https://graph.microsoft.com/.default"""
    return request_token_by_mode(account, OAUTH_MODE_OLD_GR, proxies)


def get_imap_oauth_token(account: MailAccount, proxies: dict | None = None) -> dict:
    """IMAP 专用：scope = https://outlook.office.com/IMAP.AccessAsUser.All offline_access"""
    return request_token_by_mode(account, OAUTH_MODE_IMAP, proxies)


def auto_get_token(
    account: MailAccount,
    proxies: dict | None = None,
    modes: tuple | list | None = None,
    verify_scope: bool = True,
) -> tuple[str, dict]:
    """统一入口：按顺序自动三模式兜底，返回 (成功的模式, OAuth2 payload)。

    对应参考实现中的 autoGetMail。默认顺序 新GR → 老GR → IMAP。

    verify_scope=True 时做两轮：
      第一轮要求「拿到的 token 确实含该模式期望的权限」，
      避免某些 client_id 刷新成功却返回一个没有读信权限的 token
      （这种 token 调 Graph 必 401，属于假成功）；
      第一轮全部落空后，第二轮放宽校验，接受任意刷新成功的 token，
      让上层协议链自己去试（与参考实现的宽松行为一致）。

    若账号记录过上次成功的模式（oauth_mode），该模式会被提到最前面优先尝试。
    """
    chain = list(modes) if modes else list(AUTO_MODE_CHAIN)

    # 上次成功的模式优先，省掉重复的失败尝试
    remembered = (getattr(account, "oauth_mode", "") or "").strip()
    if remembered in chain:
        chain = [remembered] + [m for m in chain if m != remembered]

    last_error: OAuthServiceError | None = None
    relaxed_hit: tuple[str, dict] | None = None

    for mode in chain:
        spec = OAUTH_MODE_SPECS[mode]
        try:
            payload = request_token_by_mode(account, mode, proxies)
        except OAuthServiceError as exc:
            last_error = exc
            _oauth_log(
                logging.WARNING,
                account_id=account.id,
                endpoint=spec["token_url"],
                attempt=1,
                tag="refresh_failed",
            )
            continue

        if not verify_scope:
            return mode, payload

        # 校验拿到的 token 是否具备该模式应有的权限
        if spec["slot"] == "imap":
            ok = _scope_has_imap(payload)
        else:
            ok = _scope_has_graph_mail(payload)

        if ok:
            _oauth_log(
                logging.INFO,
                account_id=account.id,
                endpoint=spec["token_url"],
                attempt=1,
                tag="refresh_succeeded",
            )
            return mode, payload

        # 刷新成功但权限存疑：先记下来，等严格轮全部落空再用
        if relaxed_hit is None:
            relaxed_hit = (mode, payload)
        last_error = OAuthServiceError(
            f"{spec['label']} 模式刷新成功但 token 缺少预期权限"
            f"（实际 scope: {_payload_granted_scope(payload)[:100] or '未返回'}）"
        )

    if relaxed_hit is not None:
        # 第二轮：放宽校验，接受刷新成功但 scope 存疑的 token
        _oauth_log(
            logging.INFO,
            account_id=account.id,
            endpoint=OAUTH_MODE_SPECS[relaxed_hit[0]]["token_url"],
            attempt=2,
            tag="refresh_succeeded",
        )
        return relaxed_hit

    raise last_error or OAuthServiceError("三种取件模式均失败")


def _try_msauth_refresh(account: MailAccount, proxies: dict | None) -> dict:
    """
    MSAuth 端点刷新（login.live.com）。
    必须用 wl.* 格式 scope，不能用 Graph scope。

    尝试顺序：
    1. refresh_token grant + wl.imap（用 refresh_token 刷新，最可靠）
    2. refresh_token grant + wl.basic（fallback，scope 更宽松）
    3. password grant + wl.imap（最后手段，Hotmail 通常已禁用密码认证）
    """
    cleaned_token = _sanitize_token(account.refresh_token)

    # 收集所有候选请求
    # 优化：refresh_token grant 优先（最可靠），password grant 最后（Hotmail 已禁用密码认证）
    candidates = []
    if cleaned_token:
        candidates.append({
            "grant_type": "refresh_token",
            "refresh_token": cleaned_token,
            "scope": MSAUTH_SCOPE,
        })
        candidates.append({
            "grant_type": "refresh_token",
            "refresh_token": cleaned_token,
            "scope": MSAUTH_SCOPE_FALLBACK,
        })
    if account.password:
        candidates.append({
            "grant_type": "password",
            "username": account.email,
            "password": account.password,
            "scope": MSAUTH_SCOPE,
        })

    if not candidates:
        raise OAuthServiceError("MSAuth 刷新失败：没有 password 也没有 refresh_token")

    last_error: OAuthServiceError | None = None
    for idx, req_data in enumerate(candidates):
        req_data_full = {"client_id": account.client_id, **req_data}
        _oauth_log(
            logging.INFO,
            account_id=account.id,
            endpoint=MSAUTH_TOKEN_URL,
            attempt=idx + 1,
            tag="refresh_attempt",
        )
        try:
            response = _post_with_retry(
                MSAUTH_TOKEN_URL,
                data=req_data_full,
                timeout=20,
                proxies=proxies,
                account_id=account.id,
            )
        except requests.RequestException as exc:
            # 网络错误（连接重置/超时/代理不可用）必须转成 OAuthServiceError，
            # 否则会穿透所有 except MailServiceError/OAuthServiceError，直达 worker 顶层
            # 变成 "unexpected_error" 且无法触发协议回退，导致整账号拉取失败。
            last_error = OAuthServiceError(f"MSAuth token 请求网络错误: {exc}")
            _oauth_log(
                logging.WARNING,
                account_id=account.id,
                endpoint=MSAUTH_TOKEN_URL,
                attempt=idx + 1,
                tag="network_error",
            )
            continue

        if response.ok:
            payload = response.json()
            if not payload.get("error") and payload.get("access_token"):
                _oauth_log(
                    logging.INFO,
                    account_id=account.id,
                    endpoint=MSAUTH_TOKEN_URL,
                    attempt=idx + 1,
                    tag="refresh_succeeded",
                )
                return payload
            # 响应 200 但有 error 字段
            err_msg = payload.get("error_description") or payload.get("error") or ""
            last_error = OAuthServiceError(f"MSAuth error: {err_msg}")
            _oauth_log(
                logging.WARNING,
                account_id=account.id,
                endpoint=MSAUTH_TOKEN_URL,
                attempt=idx + 1,
                tag="provider_error",
            )
            continue

        error_detail = ""
        try:
            error_payload = response.json()
            error_detail = error_payload.get("error_description") or error_payload.get("error") or ""
        except Exception:
            error_detail = response.text[:300]
        last_error = OAuthServiceError(
            f"HTTP {response.status_code}: {error_detail} (endpoint: {MSAUTH_TOKEN_URL})"
        )
        _oauth_log(
            logging.WARNING,
            account_id=account.id,
            endpoint=MSAUTH_TOKEN_URL,
            attempt=idx + 1,
            tag="http_error",
        )

    raise last_error or OAuthServiceError("MSAuth refresh failed (unknown)")


def _store_tokens(account: MailAccount, db: Session, access_token: str, new_refresh_token: str, now: int, expires_in: int | None = None, scope_slot: str = "graph", oauth_mode: str = "") -> None:
    old_refresh_token = _sanitize_token(account.refresh_token)

    if new_refresh_token and new_refresh_token != old_refresh_token:
        _oauth_log(
            logging.INFO,
            account_id=account.id,
            endpoint="token_store",
            attempt=0,
            tag="token_rotated",
        )
        if old_refresh_token:
            db.add(
                MailRefreshTokenHistory(
                    mail_account_id=account.id,
                    old_refresh_token=old_refresh_token,
                    update_time=now,
                )
            )
        account.refresh_token = new_refresh_token

    # 优先使用 OAuth 响应中的 expires_in（实际有效期），留 5 分钟缓冲
    # fallback 到 TOKEN_CACHE_SECONDS（50 分钟）
    if expires_in and expires_in > 300:
        cache_seconds = expires_in - 300  # 留 5 分钟缓冲
    else:
        cache_seconds = TOKEN_CACHE_SECONDS

    # 按 scope 隔离持久化，修复 graph / imap 刷新互相覆盖的历史问题
    if scope_slot == "imap":
        account.cached_access_token_imap = access_token
        account.cached_access_token_imap_expire_time = now + cache_seconds
    else:
        account.cached_access_token_graph = access_token
        account.cached_access_token_graph_expire_time = now + cache_seconds
    account.cached_access_token = access_token  # 旧字段保留以兼容
    account.access_token_expire_time = now + cache_seconds
    # 记录本次成功的取件模式（新GR / 老GR / IMAP），下次刷新优先复用，
    # 避免每次都从头把三种模式挨个试一遍
    if oauth_mode and getattr(account, "oauth_mode", None) != oauth_mode:
        account.oauth_mode = oauth_mode
    db.commit()
    db.refresh(account)


# 按用途隔离的 token 内存缓存：(account_id, scope) -> {"token": str, "expire": float}
# scope 分为 "graph"(需要 Mail.Read) 与 "imap"(需要 wl.imap / IMAP.AccessAsUser.All)。
# 必须隔离：M.C 账号的 Graph 路径若缓存了 Mail.Read token，会污染 IMAP 路径（IMAP 需要 wl.imap）。
_token_cache: dict = {}
_token_cache_lock = threading.Lock()


def _token_cache_get(account_id: int, scope: str) -> str | None:
    key = (account_id, scope)
    with _token_cache_lock:
        c = _token_cache.get(key)
        if c and c["expire"] > time.time():
            return c["token"]
    return None


def _token_cache_set(account_id: int, scope: str, token: str, ttl: int) -> None:
    key = (account_id, scope)
    with _token_cache_lock:
        _token_cache[key] = {"token": token, "expire": time.time() + ttl}


def _seed_token_cache_from_db(account: MailAccount, now: int, scope_slot: str) -> str | None:
    """进程重启后内存缓存为空时，用持久化在 DB 中且仍有效的 access_token 预热内存缓存。

    只读取与 scope 对应的专用列，避免把其它 scope 的 token 误填进缓存 slot
    （否则会因缓存命中而跳过刷新，导致错误 scope 的 token 长期滞留、协议取件失败）。
    若对应 scope 列无值（如历史上只刷新过另一 scope），则不预热，交由网络刷新。
    """
    col = "cached_access_token_graph" if scope_slot == "graph" else "cached_access_token_imap"
    cached = _sanitize_token(getattr(account, col, "") or "")
    if not cached:
        return None
    expire_col = (
        "cached_access_token_graph_expire_time"
        if scope_slot == "graph"
        else "cached_access_token_imap_expire_time"
    )
    expire = getattr(account, expire_col, 0) or 0
    if expire <= now:
        return None
    ttl = expire - now
    with _token_cache_lock:
        _token_cache[(account.id, scope_slot)] = {
            "token": cached,
            "expire": time.time() + ttl,
        }
    return cached


def get_valid_access_token(
    account: MailAccount,
    db: Session,
    required_scope: str = "graph",
    force_refresh: bool = False,
) -> str:
    """获取可用的 access_token。

    required_scope:
    - "graph": Graph API 需要 Mail.Read 权限（标准 OAuth2 端点，严格校验 Mail.Read）。
    - "imap":  IMAP/POP3 XOAUTH2 需要 IMAP 权限：
               * M.C / 个人版 token → 用 MSAuth 端点刷新出 wl.imap token（最可靠）。
               * 组织账号 → 用标准 OAuth2 端点请求 IMAP.AccessAsUser.All 权限。

    按用途隔离缓存，避免不同协议互相污染 token。
    """
    now = int(time.time())
    scope_slot = "imap" if required_scope == "imap" else "graph"

    if not force_refresh:
        cached = _token_cache_get(account.id, scope_slot)
        if cached:
            return cached
        # 进程重启后内存缓存为空：用持久化在 DB 中、且仍有效的对应 scope token 预热，
        # 避免每个账号在重启后首次取件都强制发起一次网络刷新（甚至刷新失败需重新登录）。
        # 只读取与 scope 对应的专用列，避免误把其它 scope 的 token 填入缓存 slot。
        seeded = _seed_token_cache_from_db(account, now, scope_slot)
        if seeded:
            return seeded

    # 从代理池获取代理（自动轮询可用代理）
    proxies = get_session_proxy(db, account)

    last_error: OAuthServiceError | None = None
    is_msauth = _is_msauth_token(account.refresh_token)

    def persist(payload: dict, oauth_mode: str = "") -> str:
        access_token = payload["access_token"]
        new_refresh_token = _sanitize_token(str(payload.get("refresh_token") or ""))
        expires_in = payload.get("expires_in")
        _store_tokens(account, db, access_token, new_refresh_token, now,
                      expires_in=int(expires_in) if expires_in else None,
                      scope_slot=scope_slot, oauth_mode=oauth_mode)
        # 缓存有效期：用 OAuth 响应中的 expires_in（留 5 分钟缓冲），否则 fallback 50 分钟
        if expires_in and expires_in > 300:
            ttl = expires_in - 300
        else:
            ttl = TOKEN_CACHE_SECONDS
        _token_cache_set(account.id, scope_slot, access_token, ttl)
        return access_token

    def try_mode_chain(mode_chain) -> str | None:
        """先走「新GR / 老GR / IMAP」三模式标准链，成功则直接返回 access_token。

        这是与参考实现对齐的首选路径：scope 精确、端点统一（consumers），
        且会把成功的模式记到 account.oauth_mode，下次优先复用。
        M.C / EwA 等 MSAuth 格式 token 走不通 consumers 端点，跳过以免白等。
        """
        nonlocal last_error
        if is_msauth:
            return None
        try:
            mode, payload = auto_get_token(account, proxies, modes=mode_chain)
        except OAuthServiceError as exc:
            last_error = exc
            return None
        logger.info(
            "oauth account_id=%s 取件模式=%s 命中",
            getattr(account, "id", 0), get_oauth_mode_label(mode),
        )
        return persist(payload, oauth_mode=mode)

    if scope_slot == "imap":
        # ── IMAP / POP3 XOAUTH2 ──
        # 首选：IMAP 专用 scope（参考实现的第三种模式）
        token = try_mode_chain(IMAP_MODE_CHAIN)
        if token:
            return token
        if is_msauth:
            # M.C 个人版 token：优先 MSAuth 端点刷新出 wl.imap token（IMAP 唯一可靠路径）
            _oauth_log(
                logging.INFO,
                account_id=account.id,
                endpoint=MSAUTH_TOKEN_URL,
                attempt=1,
                tag="msauth_imap_refresh",
            )
            try:
                payload = _try_msauth_refresh(account, proxies)
                return persist(payload)
            except OAuthServiceError as exc:
                last_error = exc
                _oauth_log(
                    logging.WARNING,
                    account_id=account.id,
                    endpoint=MSAUTH_TOKEN_URL,
                    attempt=1,
                    tag="refresh_failed",
                )
            # MSAuth 失败 → 兜底尝试标准端点（部分 MSA 应用 client_id 可刷出含 IMAP 的 token）
            for attempt_idx, token_url in enumerate((TOKEN_URL_CONSUMER, TOKEN_URL_COMMON), start=1):
                try:
                    payload = _try_oauth2_refresh(
                        token_url, account, proxies,
                        relax_scope_check=True,
                        candidate_scopes=[IMAP_SCOPES, IMAP_SCOPES_RELAXED, None],
                    )
                    return persist(payload)
                except OAuthServiceError as exc:
                    last_error = exc
                    _oauth_log(
                        logging.WARNING,
                        account_id=account.id,
                        endpoint=token_url,
                        attempt=attempt_idx,
                        tag="refresh_failed",
                    )
        else:
            # 组织账号：标准 OAuth2 端点请求 IMAP 权限
            for attempt_idx, token_url in enumerate((TOKEN_URL_COMMON, TOKEN_URL_CONSUMER), start=1):
                try:
                    payload = _try_oauth2_refresh(
                        token_url, account, proxies,
                        relax_scope_check=False,
                        candidate_scopes=[IMAP_SCOPES, IMAP_SCOPES_RELAXED, None],
                        require_scope_keyword="imap",
                    )
                    return persist(payload)
                except OAuthServiceError as exc:
                    last_error = exc
                    _oauth_log(
                        logging.WARNING,
                        account_id=account.id,
                        endpoint=token_url,
                        attempt=attempt_idx,
                        tag="refresh_failed",
                    )
    else:
        # ── Graph API（需要 Mail.Read）──
        # 首选：新GR → 老GR 两种标准模式（参考实现的前两种）
        token = try_mode_chain(GRAPH_MODE_CHAIN)
        if token:
            return token
        # 兜底：历史多 scope 探测逻辑（覆盖非标准 client_id）
        for attempt_idx, token_url in enumerate((TOKEN_URL_CONSUMER, TOKEN_URL_COMMON), start=1):
            try:
                payload = _try_oauth2_refresh(token_url, account, proxies, relax_scope_check=False)
                return persist(payload)
            except OAuthServiceError as exc:
                last_error = exc
                _oauth_log(
                    logging.WARNING,
                    account_id=account.id,
                    endpoint=token_url,
                    attempt=attempt_idx,
                    tag="refresh_failed",
                )
        if is_msauth:
            # M.C 个人版 token 通常没有 Mail.Read，Graph 基本不可用，这里仅作兜底
            try:
                payload = _try_msauth_refresh(account, proxies)
                return persist(payload)
            except OAuthServiceError as exc:
                last_error = exc
                _oauth_log(
                    logging.WARNING,
                    account_id=account.id,
                    endpoint=MSAUTH_TOKEN_URL,
                    attempt=1,
                    tag="refresh_failed",
                )

    # 所有刷新失败 → 兜底复用“同 scope”的已持久化 token（仅当仍有效）。
    # 注意：不要回退到旧的 cached_access_token（可能是另一 scope 的 token），
    # 否则 Graph 请求拿到 IMAP token 会 401，被误报为 token_invalid；也不要返回已过期 token。
    col = "cached_access_token_graph" if scope_slot == "graph" else "cached_access_token_imap"
    same_scope = _sanitize_token(getattr(account, col, "") or "")
    expire_col = (
        "cached_access_token_graph_expire_time"
        if scope_slot == "graph"
        else "cached_access_token_imap_expire_time"
    )
    if same_scope and (getattr(account, expire_col, 0) or 0) > now + 30:
        return same_scope
    # 没有可用的同 scope token：如实抛出刷新错误，让协议链正确回退 / 报出真实原因，
    # 而不是返回一个过期或错 scope 的 token 导致 401 -> 误导性的 token_invalid。
    _oauth_log(
        logging.ERROR,
        account_id=account.id,
        endpoint="all",
        attempt=0,
        tag="all_endpoints_failed",
    )
    raise last_error or OAuthServiceError("unknown token refresh error")
