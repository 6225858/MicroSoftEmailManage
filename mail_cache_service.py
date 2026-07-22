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


def save_mail_cache(db: Session, account_id: int, folder: str, mails: list) -> None:
    """保存邮件到缓存表"""
    from models import MailCache

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
        existing.updated_at = now
    else:
        cache = MailCache(
            account_id=account_id,
            folder=folder,
            mails_json=mails_json,
            mail_count=len(mails),
            updated_at=now,
        )
        db.add(cache)

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
) -> "RefreshTask":
    """
    后台异步刷新邮件缓存（不阻塞用户请求）。
    同一 (account_id, folder) 的并发刷新会去重：复用同一个 RefreshTask。
    force=True 时强制启动新任务（取消旧的）。
    返回 RefreshTask，调用方可 .event.wait(timeout=N) 等待完成。
    """
    from database import SessionLocal
    from mail_service import load_account_mails, MailServiceError, safe_mail_error_tag
    from models import MailAccount

    key = (account_id, folder)

    # 已有刷新在进行？复用同一任务
    if not force:
        with _refresh_lock:
            existing = _refresh_tasks.get(key)
            if existing is not None:
                return existing

    # 注册新任务
    task = RefreshTask()
    with _refresh_lock:
        if force and key in _refresh_tasks:
            # 让等待旧任务的人立刻返回（避免悬空等待）
            _refresh_tasks[key].event.set()
        _refresh_tasks[key] = task

    def worker() -> None:
        error_msg: Optional[str] = None
        item_count = -1
        try:
            with SessionLocal() as db:
                account = db.query(MailAccount).filter(MailAccount.id == account_id).first()
                if not account:
                    error_msg = "account not found"
                else:
                    items = load_account_mails(account, db, folder=folder, limit=limit)
                    # 检查账号是否在取件过程中被删除
                    db.expire_all()
                    if not db.query(MailAccount).filter(MailAccount.id == account_id).first():
                        logger.info("账号 %d 在刷新过程中被删除，跳过保存缓存", account_id)
                        error_msg = "account deleted during refresh"
                    else:
                        save_mail_cache(db, account_id, folder, items)
                        item_count = len(items)
                        logger.info(
                            "后台刷新邮件缓存完成: account=%d folder=%s (%d封)",
                            account_id, folder, item_count,
                        )
        except MailServiceError as exc:
            error_msg = safe_mail_error_tag(exc)
            logger.warning(
                "后台刷新邮件缓存失败 account=%d folder=%s error=%s",
                account_id, folder, error_msg,
            )
        except Exception:  # noqa: BLE001
            error_msg = "unexpected_error"
            logger.warning(
                "后台刷新邮件缓存异常 account=%d folder=%s error=%s",
                account_id, folder, error_msg,
            )
        finally:
            task.done(error=error_msg, item_count=item_count)
            with _refresh_lock:
                # 只有当前注册的还是我们的 task 时才清理
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
