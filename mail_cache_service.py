"""
邮件缓存服务：缓存邮件列表实现秒出，并支持等待后台刷新完成。
"""
import json
import logging
import threading
import time
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False

# 缓存有效期（秒），超过此时间才重新拉取
CACHE_TTL = 300  # 5 分钟

# 后台刷新任务跟踪：(account_id, folder) -> RefreshTask
# RefreshTask 包含一个 threading.Event，调用方可以 .wait() 等待刷新完成
_refresh_tasks: dict[tuple[int, str], "RefreshTask"] = {}
_refresh_lock = threading.Lock()

# 最近一次刷新的结果（含 error）。worker 完成后会从 _refresh_tasks 出队，
# 但异步任务轮询仍需要读到结果（尤其失败时的 error），因此单独持久保存。
# 键为 (account_id, folder)，天然按账号+文件夹去重，数量有限，无需额外清理。
_refresh_last_result: dict[tuple[int, str], dict] = {}
_refresh_generation: dict[tuple[int, str], int] = {}
_cache_locks_guard = threading.Lock()
_cache_locks: dict[tuple[int, str], threading.RLock] = {}


def _cache_lock(account_id: int, folder: str) -> threading.RLock:
    key = (account_id, folder)
    with _cache_locks_guard:
        return _cache_locks.setdefault(key, threading.RLock())

# 后台刷新看门狗：若单次刷新超过该秒数仍未完成（如网络卡死），强制将刷新任务标记为结束，
# 避免 is_refreshing 永远为 True 导致前端一直显示"正在拉取最新邮件"。
REFRESH_WATCHDOG_SECONDS = 90

# 未配置任何可用代理时，刷新失败附加的明确提示（大陆网络直连 Microsoft 通常不通）
NO_PROXY_REFRESH_HINT = "（当前未配置任何可用代理，大陆网络可能无法直连 Microsoft 服务，请到「设置-代理」配置代理后重试）"


class RefreshTask:
    """表示一次后台刷新任务，可被等待。"""

    __slots__ = ("event", "started_at", "error", "item_count")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.started_at = time.time()
        self.error: Optional[str] = None
        self.item_count: int = -1

    def done(self, error: Optional[str] = None, item_count: int = -1) -> None:
        self.error = error
        self.item_count = item_count
        self.event.set()


def merge_preserve_bodies(new_items: list, old_items: list | None) -> list:
    """用新列表覆盖缓存前，把旧缓存里「已经取到的正文」保留下来。

    背景：列表取件为省流量不带正文，正文是用户点开邮件时由详情接口单独拉取
    并写回缓存的。若后台刷新直接用新列表整体覆盖，这些已拉取的正文就被抹掉了，
    表现为「刚看过有内容的邮件，一刷新又变成空白」。
    这里按 id 对齐：新项正文缺失、而旧缓存有正文时，沿用旧正文。
    """
    from mail_service import is_body_missing

    if not old_items:
        return new_items

    old_map = {}
    for item in old_items:
        if isinstance(item, dict) and item.get("id"):
            old_map[item["id"]] = item
            if item.get("message_id"):
                old_map[item["message_id"]] = item

    for item in new_items or []:
        if not isinstance(item, dict):
            continue
        if not is_body_missing(item.get("body")):
            continue  # 新项自带正文，无需回填
        old = old_map.get(item.get("id")) or old_map.get(item.get("message_id"))
        if old is None:
            continue
        if not is_body_missing(old.get("body")):
            item["body"] = old.get("body")
            for extra in ("html", "attachments"):
                if old.get(extra) is not None and item.get(extra) is None:
                    item[extra] = old.get(extra)
    return new_items


def save_mail_cache(
    db: Session,
    account_id: int,
    folder: str,
    mails: list,
    touch_timestamp: bool = True,
) -> None:
    """保存邮件到缓存表。

    touch_timestamp=False 时不更新 updated_at：用于「详情接口写回单封正文」
    这类局部更新，避免把列表缓存的 TTL 续命，导致 is_fresh 误判为新鲜、
    前端不再触发真正的列表刷新。
    """
    from models import MailCache

    with _cache_lock(account_id, folder):
        now = int(time.time())
        mails_json = json.dumps(mails, ensure_ascii=False, default=str)
        existing = (
            db.query(MailCache)
            .filter(MailCache.account_id == account_id, MailCache.folder == folder)
            .first()
        )
        if existing:
            existing.mails_json = mails_json
            existing.mail_count = len(mails)
            if touch_timestamp:
                existing.updated_at = now
        else:
            db.add(MailCache(
                account_id=account_id,
                folder=folder,
                mails_json=mails_json,
                mail_count=len(mails),
                updated_at=now,
            ))
        db.commit()


def get_mail_cache(db: Session, account_id: int, folder: str) -> dict | None:
    """
    获取缓存的邮件。
    返回 {"items": [...], "updated_at": timestamp, "is_fresh": bool}
    如果没有缓存返回 None。
    """
    from models import MailCache

    cache = (
        db.query(MailCache)
        .filter(MailCache.account_id == account_id, MailCache.folder == folder)
        .first()
    )

    if not cache:
        return None

    now = int(time.time())
    is_fresh = (now - cache.updated_at) < CACHE_TTL

    try:
        mails = json.loads(cache.mails_json or "[]")
    except json.JSONDecodeError:
        mails = []

    return {
        "items": mails,
        "updated_at": cache.updated_at,
        "is_fresh": is_fresh,
        "count": cache.mail_count,
    }


def update_mail_body(
    db: Session,
    account_id: int,
    folder: str,
    mail_id: str,
    detail: dict,
) -> bool:
    """把详情接口刚拉到的单封正文写回缓存（局部更新，不动列表 TTL）。

    写回前重新读一次缓存，尽量缩小与后台刷新线程之间的读改写竞态窗口
    （MailCache 是整个文件夹一个 JSON 大字段，只能整体覆盖）。
    返回是否命中并更新了该封邮件。
    """
    if not mail_id or not isinstance(detail, dict):
        return False

    with _cache_lock(account_id, folder):
        db.expire_all()
        cached = get_mail_cache(db, account_id, folder)
        items = (cached or {}).get("items") or []
        if not items:
            return False
        updated = False
        for item in items:
            if not isinstance(item, dict) or item.get("id") != mail_id:
                continue
            body = detail.get("body")
            if body is not None:
                item["body"] = body
            for extra in ("html", "attachments"):
                if detail.get(extra) is not None:
                    item[extra] = detail[extra]
            updated = True
            break
        if updated:
            save_mail_cache(db, account_id, folder, items, touch_timestamp=False)
        return updated


def is_refreshing(account_id: int, folder: str) -> bool:
    """检查指定账号+文件夹是否有后台刷新在进行。"""
    key = (account_id, folder)
    with _refresh_lock:
        return key in _refresh_tasks


def cancel_refresh_for_account(account_id: int) -> int:
    """取消指定账号的所有后台刷新任务（所有文件夹）。

    返回取消的任务数量。
    注意：正在运行的 worker 线程无法真正中断（IMAP/POP3 连接在阻塞中），
    但会立即从 _refresh_tasks 中移除，使 is_refreshing 返回 False，
    且等待者会立即收到 "cancelled" 错误。
    worker 线程完成后会发现账号已删除，跳过 save_mail_cache。
    """
    cancelled = 0
    with _refresh_lock:
        keys_to_remove = [k for k in _refresh_tasks if k[0] == account_id]
        for key in keys_to_remove:
            task = _refresh_tasks.pop(key)
            task.done(error="cancelled")
            cancelled += 1
    if cancelled:
        logger.info("已取消账号 %d 的 %d 个后台刷新任务", account_id, cancelled)
    return cancelled


def get_active_refresh(account_id: int, folder: str) -> Optional["RefreshTask"]:
    """获取正在进行的刷新任务，没有则返回 None。"""
    key = (account_id, folder)
    with _refresh_lock:
        return _refresh_tasks.get(key)


def get_last_refresh_result(account_id: int, folder: str) -> Optional[dict]:
    """
    获取 (account_id, folder) 最近一次后台刷新的结果（含 error）。
    与 get_active_refresh 不同：任务已完成后会从 _refresh_tasks 出队，
    此函数仍能读到该次刷新的结果，用于异步任务轮询上报错误。
    """
    return _refresh_last_result.get((account_id, folder))


def wait_for_refresh(account_id: int, folder: str, timeout: float = 30.0) -> Optional["RefreshTask"]:
    """
    等待 (account_id, folder) 的后台刷新完成。
    返回 RefreshTask（包含 error 和 item_count）；若没有刷新在进行，返回 None。
    """
    key = (account_id, folder)
    with _refresh_lock:
        task = _refresh_tasks.get(key)
    if task is None:
        return None
    task.event.wait(timeout=timeout)
    return task


def refresh_mail_cache_async(
    account_id: int,
    folder: str,
    limit: int = 20,
    force: bool = False,
    incremental: bool = True,
) -> "RefreshTask":
    """
    后台异步刷新邮件缓存（不阻塞用户请求）。
    同一 (account_id, folder) 的并发刷新会去重：复用同一个 RefreshTask。
    force=True 时强制启动新任务（取消旧的）。
    incremental=True 且已有缓存时，只对新邮件取正文、复用已缓存邮件，刷新更快。
    返回 RefreshTask，调用方可 .event.wait(timeout=N) 等待完成。
    """
    from database import SessionLocal
    from mail_service import (
        load_account_mails,
        merge_incremental_mails,
        MailServiceError,
        safe_mail_error_tag,
    )
    from models import MailAccount

    key = (account_id, folder)

    with _refresh_lock:
        existing = _refresh_tasks.get(key)
        if existing is not None and not force:
            return existing
        task = RefreshTask()
        generation = _refresh_generation.get(key, 0) + 1
        _refresh_generation[key] = generation
        if existing is not None:
            existing.done(error="已被新的强制刷新取代", item_count=-1)
        _refresh_tasks[key] = task

    def _watchdog() -> None:
        # 看门狗：若刷新线程卡死超过阈值仍未结束，强制标记完成，
        # 防止 is_refreshing 永远为 True 导致前端一直显示"正在拉取最新邮件"。
        if not task.event.is_set():
            task.done(error="刷新超时(看门狗)", item_count=-1)
            with _refresh_lock:
                if _refresh_tasks.get(key) is task:
                    _refresh_generation[key] = generation + 1
                    _refresh_tasks.pop(key, None)
                    _refresh_last_result[key] = {
                        "error": task.error,
                        "item_count": -1,
                        "ts": time.time(),
                    }

    watchdog = threading.Timer(REFRESH_WATCHDOG_SECONDS, _watchdog)
    watchdog.daemon = True
    watchdog.start()

    def worker() -> None:
        error_msg: Optional[str] = None
        item_count = -1
        no_proxy = False
        try:
            with SessionLocal() as db:
                from proxy_service import has_available_proxy
                no_proxy = not has_available_proxy(db)
                account = db.query(MailAccount).filter(MailAccount.id == account_id).first()
                if not account:
                    error_msg = "account not found"
                else:
                    # 已有缓存则走增量刷新（仅对新邮件取正文，复用已缓存邮件），否则全量取件
                    items = None
                    if incremental:
                        try:
                            existing = get_mail_cache(db, account_id, folder)
                        except Exception as cache_exc:  # noqa: BLE001
                            # 读取缓存异常不应中断刷新，回退全量取件
                            logger.warning(
                                "读取邮件缓存失败,回退全量取件 account=%d: %s",
                                account_id, cache_exc,
                            )
                            existing = None
                        if existing and existing.get("items"):
                            try:
                                items, new_count = merge_incremental_mails(
                                    account, db, folder, limit, existing["items"]
                                )
                                logger.info(
                                    "增量刷新邮件缓存: account=%d folder=%s 新增 %d 封 / 共 %d 封",
                                    account_id, folder, new_count, len(items),
                                )
                            except Exception as inc_exc:  # noqa: BLE001
                                logger.warning(
                                    "增量刷新失败,回退全量取件 account=%d: %s",
                                    account_id, inc_exc,
                                )
                                items = None
                    if items is None:
                        items = load_account_mails(account, db, folder=folder, limit=limit)
                    # 检查账号是否在取件过程中被删除
                    db.expire_all()
                    if not db.query(MailAccount).filter(MailAccount.id == account_id).first():
                        logger.info("账号 %d 在刷新过程中被删除，跳过保存缓存", account_id)
                        error_msg = "account deleted during refresh"
                    else:
                        # 覆盖前重新读一次缓存并回填正文：
                        # 取件期间用户可能刚点开过某封邮件，详情接口已把正文写回缓存，
                        # 若直接用不含正文的新列表整体覆盖，那份正文就丢了。
                        with _refresh_lock:
                            still_current = _refresh_generation.get(key) == generation
                        if not still_current:
                            error_msg = "刷新结果已过期，跳过保存"
                        else:
                            with _cache_lock(account_id, folder):
                                db.expire_all()
                                latest = get_mail_cache(db, account_id, folder)
                                items = merge_preserve_bodies(
                                    items, (latest or {}).get("items")
                                )
                                save_mail_cache(db, account_id, folder, items)
                            item_count = len(items)
                        logger.info(
                            "后台刷新邮件缓存完成: account=%d folder=%s (%d封)",
                            account_id, folder, item_count,
                        )
        except MailServiceError as exc:
            error_msg = safe_mail_error_tag(exc)
            if no_proxy:
                error_msg += NO_PROXY_REFRESH_HINT
            logger.warning(
                "后台刷新邮件缓存失败 account=%d folder=%s error=%s",
                account_id, folder, error_msg,
            )
        except Exception as exc:  # noqa: BLE001
            # 记录完整堆栈，便于定位真正的失败原因；错误文案带上异常类型与信息，
            # 不再是无意义的 "unexpected_error"，方便前端展示与排查。
            error_msg = f"unexpected_error: {type(exc).__name__}: {str(exc)[:200]}"
            if no_proxy:
                error_msg += NO_PROXY_REFRESH_HINT
            logger.exception(
                "后台刷新邮件缓存异常 account=%d folder=%s",
                account_id, folder,
            )
        finally:
            watchdog.cancel()
            task.done(error=error_msg, item_count=item_count)
            with _refresh_lock:
                if _refresh_generation.get(key) == generation:
                    _refresh_last_result[key] = {
                        "error": error_msg,
                        "item_count": item_count,
                        "ts": time.time(),
                    }
                if _refresh_tasks.get(key) is task:
                    _refresh_tasks.pop(key, None)

    threading.Thread(
        target=worker,
        name=f"mail-cache-refresh-{account_id}-{folder}",
        daemon=True,
    ).start()
    return task


def refresh_mail_cache_sync(
    account_id: int,
    folder: str,
    limit: int = 20,
    timeout: float = 60.0,
    db: Optional[Session] = None,
) -> tuple[bool, Optional[str]]:
    """
    同步刷新邮件缓存（阻塞当前请求直到完成）。
    用于"强制刷新"接口。

    重要：如果传入 db（FastAPI 路由的 session），就在当前 session 中直接执行，
    避免子线程写入但主线程看不到的 SQLite 事务隔离问题。

    如果不传 db，则启动子线程并等待（保持向后兼容）。
    """
    if db is not None:
        # 在调用方的 session 中同步执行，确保写入对调用方立即可见
        from mail_service import load_account_mails, MailServiceError
        from models import MailAccount

        try:
            account = db.query(MailAccount).filter(MailAccount.id == account_id).first()
            if not account:
                return False, "account not found"
            items = load_account_mails(account, db, folder=folder, limit=limit)
            key = (account_id, folder)
            with _refresh_lock:
                _refresh_generation[key] = _refresh_generation.get(key, 0) + 1
            with _cache_lock(account_id, folder):
                db.expire_all()
                previous = get_mail_cache(db, account_id, folder)
                items = merge_preserve_bodies(items, (previous or {}).get("items"))
                save_mail_cache(db, account_id, folder, items)
            logger.info(
                "同步刷新邮件缓存完成: account=%d folder=%s (%d封)",
                account_id, folder, len(items),
            )
            return True, None
        except MailServiceError as exc:
            return False, exc.message
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)[:200]

    # 没传 db：启动子线程并等待（保留给异步场景使用）
    task = refresh_mail_cache_async(account_id, folder, limit, force=True)
    task.event.wait(timeout=timeout)
    if task.error:
        return False, task.error
    return True, None
