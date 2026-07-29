"""登录态持久化测试：按 scope 隔离的 token 存储与重启后从 DB 安全预热。"""
import time
from types import SimpleNamespace

import pytest

from oauth_service import (
    _seed_token_cache_from_db,
    _store_tokens,
    _token_cache,
    get_valid_access_token,
)


def _make_account(**overrides) -> SimpleNamespace:
    base = dict(
        id=1,
        refresh_token="dummy-refresh",
        cached_access_token="",
        cached_access_token_graph="",
        cached_access_token_imap="",
        access_token_expire_time=0,
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

    _store_tokens(account, db, "imap-tok", "", now, expires_in=3600, scope_slot="imap")
    assert account.cached_access_token_imap == "imap-tok"
    assert account.cached_access_token_graph == "graph-tok"  # 不被 imap 覆盖
    assert account.cached_access_token == "imap-tok"


def test_seed_from_db_uses_correct_scope():
    """重启后只预热对应 scope 的 DB 列；缺失或过期则不预热。"""
    _clear_cache()
    now = int(time.time())
    account = _make_account(
        cached_access_token_graph="gtok",
        cached_access_token_imap="",
        access_token_expire_time=now + 1000,
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
        access_token_expire_time=now - 10,  # 已过期
    )
    assert _seed_token_cache_from_db(account, now, "graph") is None


def test_get_valid_access_token_reuses_db_token_after_restart():
    """内存缓存为空（模拟重启）时，get_valid_access_token 直接复用 DB 中仍有效的 token，
    不再强制发起网络刷新。"""
    _clear_cache()
    now = int(time.time())
    account = _make_account(
        cached_access_token_graph="gtok",
        access_token_expire_time=now + 1000,
    )
    db = _FakeDB()
    # force_refresh=False，且内存缓存为空 -> 应命中 DB 预热并返回，不触发任何网络刷新
    token = get_valid_access_token(account, db, required_scope="graph", force_refresh=False)
    assert token == "gtok"
    # 内存缓存已被预热，后续命中
    assert get_valid_access_token(account, db, required_scope="graph", force_refresh=False) == "gtok"
