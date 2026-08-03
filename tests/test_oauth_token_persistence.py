"""登录态持久化测试：按 scope 隔离的 token 存储与重启后从 DB 安全预热。"""
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from oauth_service import (
    AUTO_MODE_CHAIN,
    IMAP_OAUTH_SCOPE,
    NEW_GR_SCOPE,
    OAUTH_MODE_IMAP,
    OAUTH_MODE_NEW_GR,
    OAUTH_MODE_OLD_GR,
    OLD_GR_SCOPE,
    _seed_token_cache_from_db,
    _store_tokens,
    _token_cache,
    auto_get_token,
    get_valid_access_token,
    request_token_by_mode,
)


def _make_account(**overrides) -> SimpleNamespace:
    base = dict(
        id=1,
        refresh_token="dummy-refresh",
        cached_access_token="",
        cached_access_token_graph="",
        cached_access_token_imap="",
        access_token_expire_time=0,
        cached_access_token_graph_expire_time=0,
        cached_access_token_imap_expire_time=0,
        client_id="client-id",
        oauth_mode="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeDB:
    def commit(self):
        pass

    def refresh(self, obj):
        pass


def _clear_cache():
    _token_cache.clear()


def test_store_tokens_isolates_scope():
    """graph 与 imap 刷新写入各自的专用列，不再互相覆盖。"""
    _clear_cache()
    account = _make_account()
    db = _FakeDB()
    now = int(time.time())

    _store_tokens(account, db, "graph-tok", "", now, expires_in=3600, scope_slot="graph")
    assert account.cached_access_token_graph == "graph-tok"
    assert account.cached_access_token_imap == ""
    assert account.cached_access_token == "graph-tok"  # 旧字段仍兼容保留
    graph_expire = account.cached_access_token_graph_expire_time

    _store_tokens(account, db, "imap-tok", "", now, expires_in=3600, scope_slot="imap")
    assert account.cached_access_token_imap == "imap-tok"
    assert account.cached_access_token_graph == "graph-tok"  # 不被 imap 覆盖
    assert account.cached_access_token_graph_expire_time == graph_expire
    assert account.cached_access_token_imap_expire_time == now + 3300
    assert account.cached_access_token == "imap-tok"


def test_seed_from_db_uses_correct_scope():
    """重启后只预热对应 scope 的 DB 列；缺失或过期则不预热。"""
    _clear_cache()
    now = int(time.time())
    account = _make_account(
        cached_access_token_graph="gtok",
        cached_access_token_imap="",
        cached_access_token_graph_expire_time=now + 1000,
    )

    seeded = _seed_token_cache_from_db(account, now, "graph")
    assert seeded == "gtok"
    assert _token_cache.get((account.id, "graph")) is not None
    # imap 列无值，不应预热，也不应误用 graph 列
    assert _seed_token_cache_from_db(account, now, "imap") is None
    assert _token_cache.get((account.id, "imap")) is None


def test_seed_from_db_expired_is_none():
    _clear_cache()
    now = int(time.time())
    account = _make_account(
        cached_access_token_graph="gtok",
        cached_access_token_graph_expire_time=now - 10,  # 已过期
    )
    assert _seed_token_cache_from_db(account, now, "graph") is None


def test_get_valid_access_token_reuses_db_token_after_restart():
    """内存缓存为空（模拟重启）时，get_valid_access_token 直接复用 DB 中仍有效的 token，
    不再强制发起网络刷新。"""
    _clear_cache()
    now = int(time.time())
    account = _make_account(
        cached_access_token_graph="gtok",
        cached_access_token_graph_expire_time=now + 1000,
    )
    db = _FakeDB()
    # force_refresh=False，且内存缓存为空 -> 应命中 DB 预热并返回，不触发任何网络刷新
    token = get_valid_access_token(account, db, required_scope="graph", force_refresh=False)
    assert token == "gtok"
    # 内存缓存已被预热，后续命中
    assert get_valid_access_token(account, db, required_scope="graph", force_refresh=False) == "gtok"


@pytest.mark.parametrize(
    ("mode", "expected_scope"),
    [
        (OAUTH_MODE_NEW_GR, NEW_GR_SCOPE),
        (OAUTH_MODE_OLD_GR, OLD_GR_SCOPE),
        (OAUTH_MODE_IMAP, IMAP_OAUTH_SCOPE),
    ],
)
def test_request_token_by_mode_uses_reference_scope(mode, expected_scope):
    account = _make_account()
    response = mock.Mock(ok=True)
    response.json.return_value = {"access_token": "token"}
    with mock.patch("oauth_service._post_with_retry", return_value=response) as post:
        assert request_token_by_mode(account, mode)["access_token"] == "token"

    _, kwargs = post.call_args
    assert kwargs["data"]["scope"] == expected_scope
    assert kwargs["data"]["grant_type"] == "refresh_token"
    assert kwargs["data"]["refresh_token"] == "dummy-refresh"


def test_auto_get_token_falls_back_new_gr_old_gr_imap_in_order():
    account = _make_account()
    calls = []

    def request(_account, mode, _proxies):
        calls.append(mode)
        if mode != OAUTH_MODE_IMAP:
            raise RuntimeError("not used")
        return {
            "access_token": "imap-token",
            "scope": "https://outlook.office.com/IMAP.AccessAsUser.All",
        }

    # auto_get_token only catches its public service error; use the real type here.
    from oauth_service import OAuthServiceError

    def request_with_service_error(_account, mode, _proxies):
        try:
            return request(_account, mode, _proxies)
        except RuntimeError as exc:
            raise OAuthServiceError(str(exc)) from exc

    with mock.patch("oauth_service.request_token_by_mode", side_effect=request_with_service_error):
        mode, payload = auto_get_token(account)

    assert calls == list(AUTO_MODE_CHAIN)
    assert mode == OAUTH_MODE_IMAP
    assert payload["access_token"] == "imap-token"


def test_auto_get_token_prioritizes_remembered_mode():
    account = _make_account(oauth_mode=OAUTH_MODE_OLD_GR)
    payload = {"access_token": "old-token", "scope": "Mail.Read"}
    with mock.patch("oauth_service.request_token_by_mode", return_value=payload) as request:
        mode, _ = auto_get_token(account)

    assert mode == OAUTH_MODE_OLD_GR
    assert request.call_args.args[1] == OAUTH_MODE_OLD_GR


def test_store_tokens_persists_oauth_mode():
    account = _make_account()
    db = _FakeDB()
    _store_tokens(
        account,
        db,
        "graph-token",
        "",
        int(time.time()),
        scope_slot="graph",
        oauth_mode=OAUTH_MODE_NEW_GR,
    )
    assert account.oauth_mode == OAUTH_MODE_NEW_GR
