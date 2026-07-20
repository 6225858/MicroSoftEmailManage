import time
import logging
from typing import Optional

import requests
from sqlalchemy.orm import Session

from models import MailAccount, MailRefreshTokenHistory
from proxy_service import get_session_proxy


TOKEN_URL_CONSUMER = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
TOKEN_URL_COMMON = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
MSAUTH_TOKEN_URL = "https://login.live.com/oauth20_token.srf"
TOKEN_CACHE_SECONDS = 30 * 60

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

MSAUTH_TOKEN_PREFIXES = ("M.C", "M.R", "EwA", "EwB")


logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


class OAuthServiceError(Exception):
    pass


# 网络重试:遇到瞬时网络错误(连接重置、超时、代理不可用)自动重试 2 次,
# 间隔递增(1s, 2s)。永久性错误(4xx)由调用方处理。
_NETWORK_RETRYABLE_EXC = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


def _post_with_retry(url: str, *, data, timeout: int, proxies=None, retries: int = 2):
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
                sleep_sec = 1.0 * (attempt + 1)
                logger.info(
                    "OAuth 请求网络错误, %.1fs 后重试 (attempt=%d/%d): %s",
                    sleep_sec, attempt + 1, retries, str(exc)[:120],
                )
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


def _try_oauth2_refresh(token_url: str, account: MailAccount, proxies: dict | None) -> dict:
    """
    标准 OAuth2 端点刷新。不同 client_id 注册时授权的 scope 不同，
    因此按以下顺序尝试，第一个成功【且 token 实际含 Mail.Read 权限】的就返回：

    1. Graph Mail.Read scope（首选，client_id 已授权 Graph 时直接用）
    2. .default + offline_access（用应用注册时的所有 scope）
    3. 不传 scope（用 refresh_token 原始 scope，最通用）

    关键修复：之前会接受任何刷新成功的 token，但有些 scope（如 'openid profile email'）
    拿到的 token 没有 Mail.Read 权限，调 Graph API 会 401，导致 "graph token invalid after refresh"。
    现在通过 OAuth2 响应中的 scope 字段验证 token 是否真的含 Mail.Read。
    """
    cleaned_token = _sanitize_token(account.refresh_token)

    # 多 scope fallback 顺序（删掉了 'openid profile email offline_access'，
    # 因为它拿到的 token 一定不含 Mail.Read）
    candidate_scopes = [
        GRAPH_SCOPE,
        "https://graph.microsoft.com/.default offline_access",
        None,  # 不传 scope 参数，让 Azure AD 用 refresh_token 默认 scope
    ]

    last_error: OAuthServiceError | None = None

    for scope in candidate_scopes:
        request_data = {
            "client_id": account.client_id,
            "grant_type": "refresh_token",
            "refresh_token": cleaned_token,
        }
        if scope:
            request_data["scope"] = scope

        try:
            response = _post_with_retry(token_url, data=request_data, timeout=20, proxies=proxies)
        except requests.RequestException as exc:
            last_error = OAuthServiceError(f"network error: {exc}")
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
            logger.debug(
                "邮箱 %s 端点 %s scope=%r 刷新失败: %s",
                account.email, token_url, scope, error_detail[:120],
            )
            continue

        payload = response.json()
        if payload.get("error"):
            last_error = OAuthServiceError(
                payload.get("error_description") or payload["error"]
            )
            continue

        if not payload.get("access_token"):
            last_error = OAuthServiceError("token response missing access_token")
            continue

        # 关键：验证返回的 token 实际包含 Mail.Read 权限
        # OAuth2 响应的 scope 字段表示实际授予的 scope
        granted_scope = str(payload.get("scope", "")).lower()
        if "mail.read" not in granted_scope and "mail.readwrite" not in granted_scope:
            # token 刷新成功但没有 Mail.Read 权限
            # 这种 token 调 Graph API 会 401，不能用作 Graph 取件
            logger.warning(
                "邮箱 %s 端点 %s scope=%r 刷新成功但 token 不含 Mail.Read（实际 scope: %s），跳过",
                account.email, token_url, scope, granted_scope[:200],
            )
            last_error = OAuthServiceError(
                f"刷新成功但 token 缺少 Mail.Read 权限（实际 scope: {granted_scope[:100]}）"
            )
            continue

        # 成功 + token 有 Mail.Read 权限
        logger.info(
            "邮箱 %s 端点 %s 刷新成功（scope=%r, granted=%s）",
            account.email, token_url, scope, granted_scope[:80],
        )
        return payload

    raise last_error or OAuthServiceError("unknown OAuth2 refresh error")


def _try_msauth_refresh(account: MailAccount, proxies: dict | None) -> dict:
    """
    MSAuth 端点刷新（login.live.com）。
    必须用 wl.* 格式 scope，不能用 Graph scope。

    尝试顺序：
    1. password grant + wl.imap（用账号密码直接拿 token）
    2. refresh_token grant + wl.imap（用 refresh_token 刷新）
    3. refresh_token grant + wl.basic（fallback，scope 更宽松）
    """
    cleaned_token = _sanitize_token(account.refresh_token)

    # 收集所有候选请求
    candidates = []
    if account.password:
        candidates.append({
            "grant_type": "password",
            "username": account.email,
            "password": account.password,
            "scope": MSAUTH_SCOPE,
        })
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

    if not candidates:
        raise OAuthServiceError("MSAuth 刷新失败：没有 password 也没有 refresh_token")

    last_error: OAuthServiceError | None = None
    for idx, req_data in enumerate(candidates):
        req_data_full = {"client_id": account.client_id, **req_data}
        scope_desc = req_data.get("scope", "")
        grant_desc = req_data.get("grant_type", "")
        logger.info(
            "邮箱 %s MSAuth 尝试 %d/%d: grant=%s scope=%s",
            account.email, idx + 1, len(candidates), grant_desc, scope_desc,
        )
        response = _post_with_retry(MSAUTH_TOKEN_URL, data=req_data_full, timeout=20, proxies=proxies)

        if response.ok:
            payload = response.json()
            if not payload.get("error") and payload.get("access_token"):
                logger.info("邮箱 %s MSAuth 刷新成功 (grant=%s scope=%s)",
                            account.email, grant_desc, scope_desc)
                return payload
            # 响应 200 但有 error 字段
            err_msg = payload.get("error_description") or payload.get("error") or ""
            last_error = OAuthServiceError(f"MSAuth error: {err_msg}")
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
        logger.warning(
            "邮箱 %s MSAuth 尝试 %d 失败 (grant=%s scope=%s): %s",
            account.email, idx + 1, grant_desc, scope_desc, error_detail[:120],
        )

    raise last_error or OAuthServiceError("MSAuth refresh failed (unknown)")


def _store_tokens(account: MailAccount, db: Session, access_token: str, new_refresh_token: str, now: int) -> None:
    old_refresh_token = _sanitize_token(account.refresh_token)

    if new_refresh_token and new_refresh_token != old_refresh_token:
        logger.info("邮箱 %s 收到新的 refresh_token", account.email)
        if old_refresh_token:
            db.add(
                MailRefreshTokenHistory(
                    mail_account_id=account.id,
                    old_refresh_token=old_refresh_token,
                    update_time=now,
                )
            )
        account.refresh_token = new_refresh_token

    account.cached_access_token = access_token
    account.access_token_expire_time = now + TOKEN_CACHE_SECONDS
    db.commit()
    db.refresh(account)


def get_valid_access_token(account: MailAccount, db: Session) -> str:
    now = int(time.time())
    if account.cached_access_token and account.access_token_expire_time > now:
        return account.cached_access_token

    # 从代理池获取代理（自动轮询可用代理）
    proxies = get_session_proxy(db, account)

    last_error: OAuthServiceError | None = None
    is_msauth = _is_msauth_token(account.refresh_token)

    # 优先尝试标准 OAuth2 端点（获取含 Mail.Read scope 的 JWT token）
    # 标准 OAuth2 端点返回的 token 同时适用于 Graph API 和 IMAP XOAUTH2，
    # 这样即使没有代理（IMAP 直连被 IP 限制），Graph API 也能正常取件。
    # 即使 refresh_token 是 MSAuth 格式(M.C开头)，标准端点也常常可以接受。
    #
    # 优化：M.C 格式 token 如果第一个标准端点(consumers)失败，
    # 第二个端点(common)大概率也失败，直接 fallback 到 MSAuth 端点，
    # 避免浪费时间在 6 次注定失败的标准 OAuth2 请求上（3 scope × 2 端点）
    standard_endpoints = (TOKEN_URL_CONSUMER, TOKEN_URL_COMMON) if not is_msauth else (TOKEN_URL_CONSUMER,)
    for token_url in standard_endpoints:
        try:
            payload = _try_oauth2_refresh(token_url, account, proxies)
            access_token = payload["access_token"]
            new_refresh_token = _sanitize_token(str(payload.get("refresh_token") or ""))
            _store_tokens(account, db, access_token, new_refresh_token, now)
            return access_token
        except OAuthServiceError as exc:
            last_error = exc
            logger.warning("邮箱 %s 端点 %s 刷新失败: %s", account.email, token_url, str(exc)[:200])

    # 标准 OAuth2 端点全部失败 → 尝试 MSAuth 端点（仅 M.C 格式 token）
    # MSAuth 端点返回的 token 只有 wl.imap scope，不能用于 Graph API，
    # 但可以用于 IMAP XOAUTH2 认证（需要配合代理使用，否则直连可能被 IP 限制）
    if is_msauth:
        logger.info("邮箱 %s 标准 OAuth2 端点失败，尝试 MSAuth 端点刷新", account.email)
        try:
            payload = _try_msauth_refresh(account, proxies)
            access_token = payload["access_token"]
            new_refresh_token = _sanitize_token(str(payload.get("refresh_token") or ""))
            _store_tokens(account, db, access_token, new_refresh_token, now)
            return access_token
        except OAuthServiceError as exc:
            last_error = exc
            logger.warning(
                "邮箱 %s MSAuth 端点刷新也失败: %s",
                account.email, str(exc)[:200],
            )

    logger.error("邮箱 %s OAuth 刷新失败（所有端点均失败）", account.email)
    raise last_error or OAuthServiceError("unknown token refresh error")
