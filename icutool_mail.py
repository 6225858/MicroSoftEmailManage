import secrets
import time
import logging
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional
from urllib.parse import quote

import uvicorn
import requests
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from database import Base, SessionLocal, engine
from mail_service import MailServiceError, load_account_mails, list_account_folders, load_single_mail, list_account_folders_with_protocol, load_single_mail_with_protocol
from mail_cache_service import (
    get_mail_cache,
    save_mail_cache,
    refresh_mail_cache_async,
    refresh_mail_cache_sync,
    wait_for_refresh,
    is_refreshing,
)
from models import ApiKey, MailAccount, MailCache, Proxy, TokenRefreshLog
from oauth_service import OAuthServiceError, get_valid_access_token
from proxy_service import import_proxy_line, test_proxies_status

logger = logging.getLogger("icutool_mail")

# ── 应用版本 & 配置 ──────────────────────────────────────
APP_VERSION = "1.0.0"
DEFAULT_GITHUB_REPO = "6225858/MicroSoftEmailManage"
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")


def load_settings() -> dict:
    """从 settings.json 读取配置,文件不存在或解析失败时返回空字典"""
    if not os.path.exists(SETTINGS_FILE):
        return {}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(settings: dict) -> bool:
    """保存配置到 settings.json"""
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        return True
    except Exception as exc:
        logger.warning("保存 settings.json 失败: %s", exc)
        return False


def compare_versions(v1: str, v2: str) -> int:
    """比较两个 semver 版本号(格式: major.minor.patch)
    返回:  1(v1>v2), 0(相等), -1(v1<v2)"""
    try:
        parts1 = [int(x) for x in v1.strip().lstrip("vV").split(".")]
        parts2 = [int(x) for x in v2.strip().lstrip("vV").split(".")]
    except (ValueError, AttributeError):
        return 0
    while len(parts1) < len(parts2):
        parts1.append(0)
    while len(parts2) < len(parts1):
        parts2.append(0)
    for p1, p2 in zip(parts1, parts2):
        if p1 > p2:
            return 1
        if p1 < p2:
            return -1
    return 0
from token_refresh_service import (
    ALLOWED_PAGE_SIZES,
    TokenRefreshTaskRunningError,
    serialize_token_refresh_log,
    start_token_refresh_job_async,
    start_token_refresh_scheduler,
    stop_token_refresh_scheduler,
)

Base.metadata.create_all(bind=engine)


class ImportBody(BaseModel):
    text: str
    protocol: str = "auto"
    mail_server: str = ""
    mail_port: int = 0
    mail_use_ssl: int = 1


class TagsBody(BaseModel):
    tags: str


class RemarkBody(BaseModel):
    remark: str


class BatchDeleteBody(BaseModel):
    ids: list[int]


class BatchRefreshBody(BaseModel):
    ids: list[int]
    folder: str = "inbox"
    limit: int = 20


class ApiKeyBody(BaseModel):
    name: str


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key"),
    db: Session = Depends(get_db),
):
    """供外部应用接入时校验 API Key；本地 Web 访问无需认证。"""
    if x_api_key:
        api_key_record = db.query(ApiKey).filter(ApiKey.key == x_api_key).first()
        if api_key_record:
            api_key_record.last_used_at = int(time.time())
            db.commit()
            return
        raise HTTPException(status_code=401, detail="invalid api key")


def normalize_tags(tags: str) -> str:
    seen = []
    for item in tags.replace("，", ",").split(","):
        value = item.strip()
        if value and value not in seen:
            seen.append(value)
    return ",".join(seen)


def normalize_remark(remark: str) -> str:
    return (remark or "").strip()


def ensure_mail_account_schema() -> None:
    with engine.begin() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(mail_account)").fetchall()}
        if "remark" not in columns:
            conn.exec_driver_sql("ALTER TABLE mail_account ADD COLUMN remark TEXT NOT NULL DEFAULT ''")
        if "valid_status" not in columns:
            conn.exec_driver_sql("ALTER TABLE mail_account ADD COLUMN valid_status INTEGER NOT NULL DEFAULT 1")
        if "protocol" not in columns:
            conn.exec_driver_sql("ALTER TABLE mail_account ADD COLUMN protocol TEXT NOT NULL DEFAULT 'auto'")
        if "last_used_protocol" not in columns:
            conn.exec_driver_sql("ALTER TABLE mail_account ADD COLUMN last_used_protocol TEXT NOT NULL DEFAULT ''")
        if "mail_server" not in columns:
            conn.exec_driver_sql("ALTER TABLE mail_account ADD COLUMN mail_server TEXT NOT NULL DEFAULT ''")
        if "mail_port" not in columns:
            conn.exec_driver_sql("ALTER TABLE mail_account ADD COLUMN mail_port INTEGER NOT NULL DEFAULT 0")
        if "mail_use_ssl" not in columns:
            conn.exec_driver_sql("ALTER TABLE mail_account ADD COLUMN mail_use_ssl INTEGER NOT NULL DEFAULT 1")
        conn.exec_driver_sql("UPDATE mail_account SET valid_status = 1 WHERE valid_status IS NULL")
        # 把 NULL/空字符串的 protocol 设为 auto
        conn.exec_driver_sql("UPDATE mail_account SET protocol = 'auto' WHERE protocol IS NULL OR protocol = ''")
        # 把自动切换标签清理掉（自动选择模式下不再使用降级标签）
        conn.exec_driver_sql("UPDATE mail_account SET tags = '' WHERE tags IS NULL")


def ensure_token_refresh_log_schema() -> None:
    with engine.begin() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(token_refresh_log)").fetchall()}
        if "html_content" not in columns:
            conn.exec_driver_sql("ALTER TABLE token_refresh_log ADD COLUMN html_content TEXT NOT NULL DEFAULT ''")


def ensure_api_key_schema() -> None:
    with engine.begin() as conn:
        tables = {row[0] for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "api_key" not in tables:
            conn.exec_driver_sql("""
                CREATE TABLE api_key (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL DEFAULT '',
                    key TEXT NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL,
                    last_used_at INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_api_key_key ON api_key(key)")


def ensure_proxy_schema() -> None:
    with engine.begin() as conn:
        tables = {row[0] for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "proxy" not in tables:
            conn.exec_driver_sql("""
                CREATE TABLE proxy (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL DEFAULT '',
                    proxy_type TEXT NOT NULL DEFAULT 'http',
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    password TEXT NOT NULL DEFAULT '',
                    status INTEGER NOT NULL DEFAULT 1,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    latency_ms INTEGER NOT NULL DEFAULT 0,
                    exit_ip TEXT NOT NULL DEFAULT '',
                    purity_info TEXT NOT NULL DEFAULT '',
                    last_used_at INTEGER NOT NULL DEFAULT 0,
                    last_checked_at INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL
                )
            """)
        else:
            # 补齐旧表缺少的列
            cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(proxy)").fetchall()}
            for col, coltype in [("latency_ms", "INTEGER NOT NULL DEFAULT 0"),
                                  ("exit_ip", "TEXT NOT NULL DEFAULT ''"),
                                  ("purity_info", "TEXT NOT NULL DEFAULT ''")]:
                if col not in cols:
                    conn.exec_driver_sql(f"ALTER TABLE proxy ADD COLUMN {col} {coltype}")


ensure_mail_account_schema()
ensure_token_refresh_log_schema()
ensure_api_key_schema()
ensure_proxy_schema()


def ensure_mail_cache_schema() -> None:
    with engine.begin() as conn:
        tables = {row[0] for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "mail_cache" not in tables:
            conn.exec_driver_sql("""
                CREATE TABLE mail_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    folder TEXT NOT NULL DEFAULT 'inbox',
                    mails_json TEXT NOT NULL DEFAULT '[]',
                    mail_count INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_mail_cache_account ON mail_cache(account_id)")


ensure_mail_cache_schema()


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_token_refresh_scheduler()
    try:
        yield
    finally:
        stop_token_refresh_scheduler()


app = FastAPI(title="Hotmail Mail Manager", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
def index_page(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={})


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request):
    return RedirectResponse(url="/", status_code=302)


@app.get("/api/accounts", dependencies=[Depends(require_api_key)])
def list_accounts(
    q: str = Query(default=""),
    tag: str = Query(default=""),
    db: Session = Depends(get_db),
):
    query = db.query(MailAccount)
    if q:
        like_text = f"%{q.strip()}%"
        query = query.filter(MailAccount.email.like(like_text))
    if tag:
        tag_text = tag.strip()
        if tag_text == "__NO_TAG__":
            query = query.filter(or_(MailAccount.tags.is_(None), MailAccount.tags == ""))
        else:
            query = query.filter(MailAccount.tags.like(f"%{tag_text}%"))

    accounts = query.order_by(MailAccount.id.desc()).all()
    return {
        "items": [
            {
                "id": account.id,
                "email": account.email,
                "tags": normalize_tags(account.tags or ""),
                "remark": normalize_remark(account.remark or ""),
                "protocol": (account.protocol or "auto") if hasattr(account, "protocol") else "auto",
                "last_used_protocol": (account.last_used_protocol or "") if hasattr(account, "last_used_protocol") else "",
                "mail_server": (account.mail_server or "") if hasattr(account, "mail_server") else "",
                "mail_port": (account.mail_port or 0) if hasattr(account, "mail_port") else 0,
                "mail_use_ssl": (account.mail_use_ssl if account.mail_use_ssl is not None else 1) if hasattr(account, "mail_use_ssl") else 1,
                "created_at": account.created_at,
            }
            for account in accounts
        ]
    }


@app.delete("/api/accounts/{account_id}", dependencies=[Depends(require_api_key)])
def delete_account(account_id: int, db: Session = Depends(get_db)):
    account = db.query(MailAccount).filter(MailAccount.id == account_id).first()
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")

    # 删除账号前清理其所有文件夹的邮件缓存
    db.query(MailCache).filter(MailCache.account_id == account_id).delete(synchronize_session=False)

    db.delete(account)
    db.commit()
    return {"ok": True, "id": account_id}


@app.post("/api/accounts/batch-delete", dependencies=[Depends(require_api_key)])
def batch_delete_accounts(body: BatchDeleteBody, db: Session = Depends(get_db)):
    """批量删除邮箱账号,同时清理对应的邮件缓存"""
    if not body.ids:
        return {"ok": True, "deleted": 0, "missing": []}

    ids_to_delete = list(dict.fromkeys(body.ids))  # 去重保序
    accounts = (
        db.query(MailAccount)
        .filter(MailAccount.id.in_(ids_to_delete))
        .all()
    )
    found_ids = {account.id for account in accounts}

    # 批量清理这些账号的邮件缓存
    db.query(MailCache).filter(MailCache.account_id.in_(ids_to_delete)).delete(synchronize_session=False)

    for account in accounts:
        db.delete(account)
    db.commit()

    missing_ids = [i for i in ids_to_delete if i not in found_ids]
    return {
        "ok": True,
        "deleted": len(accounts),
        "missing": missing_ids,
    }


def preheat_accounts_async(account_ids: list[int], folder: str = "inbox", limit: int = 20) -> None:
    """导入完成后异步预热账号:先刷新 OAuth token,再触发后台邮件缓存刷新。

    - 单独线程执行,不阻塞导入 API 响应
    - 并发数限制在 3,避免一次性触发大量请求被限流
    - 任一账号失败不影响其他账号
    - 已经在刷新中的账号会自动跳过(refresh_mail_cache_async 内部去重)
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not account_ids:
        return

    folder = folder if folder in ("inbox", "junk") else "inbox"

    def preheat_one(account_id: int) -> bool:
        try:
            with SessionLocal() as db:
                account = db.query(MailAccount).filter(MailAccount.id == account_id).first()
                if not account or not account.refresh_token:
                    return False
                # 1) 先预热 OAuth token:忽略已缓存的,强制刷新一次,提前暴露 token 问题
                try:
                    get_valid_access_token(account, db)
                except OAuthServiceError as exc:
                    logger.warning("账号 %s 预热 token 失败: %s", account.email, str(exc)[:200])
                    return False
                # 2) 触发后台邮件缓存刷新(非阻塞,内部去重)
                refresh_mail_cache_async(account_id, folder, limit=limit, force=False)
                return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("账号 id=%s 预热失败: %s", account_id, str(exc)[:200])
            return False

    def runner() -> None:
        try:
            with ThreadPoolExecutor(max_workers=3, thread_name_prefix="preheat") as pool:
                futures = [pool.submit(preheat_one, account_id) for account_id in account_ids]
                for _ in as_completed(futures, timeout=120):
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("预热账号池异常: %s", str(exc)[:200])

    threading.Thread(target=runner, name="preheat-accounts", daemon=True).start()


@app.post("/api/accounts/import", dependencies=[Depends(require_api_key)])
def import_accounts(body: ImportBody, db: Session = Depends(get_db)):
    # 规范化协议参数
    protocol = (body.protocol or "auto").strip().lower()
    if protocol not in ("auto", "graph", "imap", "pop3"):
        protocol = "auto"

    mail_server = (body.mail_server or "").strip()
    mail_port = int(body.mail_port or 0)
    mail_use_ssl = 1 if body.mail_use_ssl else 0

    lines = body.text.splitlines()
    inserted = 0
    updated = 0
    skipped = 0
    # 识别详情：让用户看到每行被解析成什么字段
    details: list[dict] = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        # 自动检测分隔符：
        # - 优先用 ----（4 个连字符）作为分隔符
        # - 否则用 |（管道符）
        if "----" in line:
            parts = [part.strip() for part in line.split("----")]
            sep = "----"
        else:
            parts = [part.strip() for part in line.split("|")]
            sep = "|"

        if len(parts) < 2:
            skipped += 1
            details.append({
                "line": line[:80],
                "status": "skipped",
                "reason": "字段数不足（至少需要邮箱+密码）",
                "separator": sep,
            })
            continue

        email_value = parts[0]
        password = parts[1] if len(parts) > 1 else ""
        client_id = parts[2] if len(parts) > 2 else ""
        refresh_token = parts[3] if len(parts) > 3 else ""

        # 行内可覆盖协议 / 服务器配置
        line_protocol = protocol
        line_server = mail_server
        line_port = mail_port
        line_ssl = mail_use_ssl

        if len(parts) >= 5 and parts[4]:
            proto_candidate = parts[4].strip().lower()
            if proto_candidate in ("auto", "graph", "imap", "pop3"):
                line_protocol = proto_candidate

        if len(parts) >= 6 and parts[5]:
            server_field = parts[5].strip()
            if ":" in server_field:
                host_part, port_part = server_field.rsplit(":", 1)
                line_server = host_part.strip()
                try:
                    line_port = int(port_part.strip())
                except ValueError:
                    line_port = 0
            else:
                line_server = server_field

        if len(parts) >= 7 and parts[6]:
            line_ssl = 1 if parts[6].strip() in ("1", "ssl", "tls", "true", "yes") else 0

        # 自动检测字段顺序：MSAuth refresh_token 以 M.C/M.R/EwA/EwB 开头
        # 支持两种顺序：
        #   顺序 A: 邮箱|密码|refresh_token|client_id     （MSAuth token 在前）
        #   顺序 B: 邮箱----密码----client_id----refresh_token  （标准顺序）
        MSAUTH_PREFIXES = ("M.C", "M.R", "EwA", "EwB")
        field_swap = False
        if client_id and refresh_token:
            is_field3_msauth = any(client_id.startswith(p) for p in MSAUTH_PREFIXES)
            is_field4_msauth = any(refresh_token.startswith(p) for p in MSAUTH_PREFIXES)
            if is_field3_msauth and not is_field4_msauth:
                # parts[2] 实际是 refresh_token，parts[3] 是 client_id → 交换
                client_id, refresh_token = refresh_token, client_id
                field_swap = True

        account = db.query(MailAccount).filter(MailAccount.email == email_value).first()
        now = int(time.time())

        if account is None:
            account = MailAccount(
                email=email_value,
                password=password,
                client_id=client_id,
                refresh_token=refresh_token,
                cached_access_token="",
                access_token_expire_time=0,
                tags="",
                remark="",
                valid_status=1,
                protocol=line_protocol,
                mail_server=line_server,
                mail_port=line_port,
                mail_use_ssl=line_ssl,
                created_at=now,
            )
            db.add(account)
            inserted += 1
            status = "inserted"
        else:
            account.password = password
            account.client_id = client_id
            # 仅当 refresh_token 实际变化时才清空已缓存的 access_token,
            # 避免已可用的 token 被无谓重置 → 首次取件时还要重新刷新
            if (account.refresh_token or "") != refresh_token:
                account.refresh_token = refresh_token
                account.cached_access_token = ""
                account.access_token_expire_time = 0
            else:
                account.refresh_token = refresh_token
            account.protocol = line_protocol
            account.mail_server = line_server
            account.mail_port = line_port
            account.mail_use_ssl = line_ssl
            updated += 1
            status = "updated"

        # 记录识别详情（不暴露完整 token/密码）
        details.append({
            "line": email_value,
            "status": status,
            "separator": sep,
            "field_count": len(parts),
            "swapped": field_swap,
            "email": email_value,
            "client_id_prefix": (client_id[:8] + "...") if client_id else "",
            "refresh_token_prefix": (refresh_token[:8] + "...") if refresh_token else "",
            "refresh_token_type": "MSAuth" if refresh_token.startswith(MSAUTH_PREFIXES) else (
                "Standard" if refresh_token else "None"
            ),
            "protocol": line_protocol,
        })

    db.commit()

    # 收集本次导入的有 refresh_token 的账号(新增或更新),
    # 异步预热 OAuth token + 邮件缓存,显著加快首次取件速度并降低首次失败率。
    # 用独立 session 是因为预热是异步的,主请求已经返回。
    preheat_ids: list[int] = []
    try:
        with SessionLocal() as preheat_db:
            for detail in details:
                email_addr = detail.get("email")
                if not email_addr:
                    continue
                account = preheat_db.query(MailAccount).filter(MailAccount.email == email_addr).first()
                if account and account.refresh_token:
                    preheat_ids.append(account.id)
    except Exception:
        # 预热失败不影响导入结果
        pass

    if preheat_ids:
        preheat_accounts_async(preheat_ids, folder="inbox", limit=20)

    return {
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "details": details,
    }


@app.get("/api/accounts/export", dependencies=[Depends(require_api_key)])
def export_accounts(db: Session = Depends(get_db)):
    accounts = (
        db.query(MailAccount)
        .filter(MailAccount.valid_status != 0)
        .order_by(MailAccount.id.asc())
        .all()
    )
    lines = []
    for account in accounts:
        parts = [
            (account.email or "").replace("\r", " ").replace("\n", " "),
            (account.password or "").replace("\r", " ").replace("\n", " "),
            (account.client_id or "").replace("\r", " ").replace("\n", " "),
            (account.refresh_token or "").replace("\r", " ").replace("\n", " "),
        ]
        protocol = (account.protocol or "auto") if hasattr(account, "protocol") else "auto"
        if protocol != "auto":
            parts.append(protocol)
            mail_server = (account.mail_server or "") if hasattr(account, "mail_server") else ""
            mail_port = (account.mail_port or 0) if hasattr(account, "mail_port") else 0
            if mail_server:
                parts.append(f"{mail_server}:{mail_port}" if mail_port else mail_server)
        lines.append("----".join(parts))
    content = "\n".join(lines)
    now = datetime.now()
    timestamp = now.strftime("%Y年%m月%d日%H时%M分%S秒")
    filename = f"emailToken{timestamp}.txt"
    fallback_filename = f"emailToken{now.strftime('%Y%m%d%H%M%S')}.txt"
    headers = {
        "Content-Disposition": (
            f'attachment; filename="{fallback_filename}"; '
            f"filename*=UTF-8''{quote(filename)}"
        )
    }
    return Response(content=content, media_type="text/plain; charset=utf-8", headers=headers)


@app.post("/api/accounts/{account_id}/tags", dependencies=[Depends(require_api_key)])
def update_tags(account_id: int, body: TagsBody, db: Session = Depends(get_db)):
    account = db.query(MailAccount).filter(MailAccount.id == account_id).first()
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")

    account.tags = normalize_tags(body.tags)
    db.commit()
    return {"ok": True, "tags": account.tags}


@app.post("/api/accounts/{account_id}/remark", dependencies=[Depends(require_api_key)])
def update_remark(account_id: int, body: RemarkBody, db: Session = Depends(get_db)):
    account = db.query(MailAccount).filter(MailAccount.id == account_id).first()
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")

    account.remark = normalize_remark(body.remark)
    db.commit()
    return {"ok": True, "remark": account.remark}


@app.get("/api/mails/by-tag", dependencies=[Depends(require_api_key)])
def get_mails_by_tag(
    tag: str = Query(),
    folder: str = Query(default="inbox"),
    limit: int = Query(default=5, ge=1, le=50),
    db: Session = Depends(get_db),
):
    """
    按标签批量获取所有匹配账号的最新邮件。
    返回结构: { "tag": "...", "accounts": [{ "email", "account_id", "mails": [...] }] }
    """
    tag_text = tag.strip()
    if not tag_text:
        raise HTTPException(status_code=400, detail="tag 参数不能为空")

    # 查找匹配该标签的所有账号
    accounts = (
        db.query(MailAccount)
        .filter(MailAccount.tags.like(f"%{tag_text}%"))
        .order_by(MailAccount.id.asc())
        .all()
    )

    results = []
    for account in accounts:
        # 优先从缓存取（秒出）
        cached = get_mail_cache(db, account.id, folder)
        if cached and cached["items"]:
            mails = cached["items"][:limit]
        else:
            # 无缓存则跳过（避免阻塞，调用方应先触发刷新）
            mails = []

        results.append({
            "account_id": account.id,
            "email": account.email,
            "tags": account.tags or "",
            "mail_count": len(mails),
            "mails": mails,
        })

    return {
        "tag": tag_text,
        "total_accounts": len(results),
        "accounts": results,
    }


@app.get("/api/accounts/{account_id}/mails", dependencies=[Depends(require_api_key)])
def get_account_mails(
    account_id: int,
    folder: str = Query(default="inbox"),
    limit: int = Query(default=20, ge=1, le=100),
    wait: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    account = db.query(MailAccount).filter(MailAccount.id == account_id).first()
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")

    # 1. 先查缓存
    cached = get_mail_cache(db, account_id, folder)

    # 2. 有缓存 → 立即返回秒出 + 后台异步拉取最新
    if cached and cached["items"]:
        # 触发后台刷新（自动去重：若已有刷新在进行则复用，不会启动新线程）
        refresh_mail_cache_async(account_id, folder, limit)

        # 如果调用方要求等待最新结果，阻塞最多 30 秒等后台刷新完成
        refreshed_at = cached["updated_at"]
        if wait:
            task = wait_for_refresh(account_id, folder, timeout=30)
            # 关键：后台 worker 用的是独立 session 写入数据库
            # 当前 db session 缓存了旧的对象，必须 expire_all 才能重新查询拿到新数据
            db.expire_all()
            # 重新读缓存
            cached_after = get_mail_cache(db, account_id, folder)
            if cached_after:
                cached = cached_after
                refreshed_at = cached_after["updated_at"]
            return {
                "items": cached["items"],
                "cached": True,
                "updated_at": refreshed_at,
                "is_fresh": cached["is_fresh"],
                "refresh_error": task.error if task else None,
            }

        # 不等待，立即返回缓存（与旧行为一致）
        # 后台刷新完成后，调用方可通过下一次请求拿到新数据
        return {
            "items": cached["items"],
            "cached": True,
            "updated_at": cached["updated_at"],
            "is_fresh": cached["is_fresh"],
            "refreshing": is_refreshing(account_id, folder),
        }

    # 3. 无缓存 → 实时拉取（首次会慢一点）
    try:
        items = load_account_mails(account, db, folder=folder, limit=limit)
        save_mail_cache(db, account_id, folder, items)
        return {"items": items, "cached": False, "updated_at": int(time.time())}
    except MailServiceError as exc:
        if cached:
            return {
                "items": cached["items"],
                "cached": True,
                "stale": True,
                "updated_at": cached["updated_at"],
                "refresh_error": exc.message,
            }
        # 无缓存 + 实时拉取失败:如果后台正在刷新,等最多 30 秒,可能等到结果
        if is_refreshing(account_id, folder):
            try:
                wait_for_refresh(account_id, folder, timeout=30)
                db.expire_all()
                cached_after = get_mail_cache(db, account_id, folder)
                if cached_after and cached_after["items"]:
                    return {
                        "items": cached_after["items"],
                        "cached": True,
                        "updated_at": cached_after["updated_at"],
                        "is_fresh": cached_after["is_fresh"],
                    }
            except Exception:  # noqa: BLE001
                pass
        raise HTTPException(status_code=400, detail=exc.message)


@app.post("/api/accounts/{account_id}/mails/refresh", dependencies=[Depends(require_api_key)])
def force_refresh_account_mails(
    account_id: int,
    folder: str = Query(default="inbox"),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """强制同步刷新指定账号+文件夹的邮件缓存，返回最新结果。"""
    account = db.query(MailAccount).filter(MailAccount.id == account_id).first()
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")

    # 把 db 传给 sync 函数，让它就在当前 session 中写缓存
    # 这样写入对当前 session 立即可见，避免子线程写入主线程看不到的 SQLite 隔离问题
    ok, err = refresh_mail_cache_sync(account_id, folder, limit, timeout=60, db=db)

    # 清除 session 缓存强制重新查询数据库（双重保险）
    db.expire_all()

    cached = get_mail_cache(db, account_id, folder)

    if not ok and not cached:
        raise HTTPException(status_code=400, detail=err or "refresh failed")

    return {
        "items": cached["items"] if cached else [],
        "cached": True,
        "updated_at": cached["updated_at"] if cached else int(time.time()),
        "is_fresh": True,
        "forced": True,
        "refresh_error": err,
    }


@app.post("/api/accounts/batch-refresh", dependencies=[Depends(require_api_key)])
def batch_refresh_account_mails(body: BatchRefreshBody, db: Session = Depends(get_db)):
    """批量触发邮件后台异步刷新(非阻塞)。

    - 不阻塞当前请求,立即返回触发结果
    - 每个 (account_id, folder) 自动去重:正在刷新的不会重复触发
    - 返回 { triggered, skipped, missing } 三类信息
    """
    if not body.ids:
        return {"ok": True, "triggered": 0, "skipped": 0, "missing": []}

    folder = (body.folder or "inbox").strip() or "inbox"
    if folder not in ("inbox", "junk"):
        folder = "inbox"

    limit = max(1, min(int(body.limit or 20), 100))

    ids_to_refresh = list(dict.fromkeys(body.ids))  # 去重保序
    accounts = (
        db.query(MailAccount)
        .filter(MailAccount.id.in_(ids_to_refresh))
        .all()
    )
    found_ids = {account.id for account in accounts}

    triggered = 0
    skipped = 0
    for account in accounts:
        # 先判断是否已经在刷新中(去重),便于统计
        if is_refreshing(account.id, folder):
            skipped += 1
            continue
        # 触发后台异步刷新(不阻塞当前请求)
        refresh_mail_cache_async(account.id, folder, limit=limit, force=False)
        triggered += 1

    missing_ids = [i for i in ids_to_refresh if i not in found_ids]
    return {
        "ok": True,
        "triggered": triggered,
        "skipped": skipped,
        "missing": missing_ids,
        "folder": folder,
    }


@app.get("/api/accounts/{account_id}/folders", dependencies=[Depends(require_api_key)])
def get_account_folders(
    account_id: int,
    db: Session = Depends(get_db),
):
    account = db.query(MailAccount).filter(MailAccount.id == account_id).first()
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")

    try:
        folders = list_account_folders_with_protocol(account, db)
        return {"items": folders}
    except MailServiceError as exc:
        raise HTTPException(status_code=400, detail=exc.message)


@app.get("/api/accounts/{account_id}/mails/{mail_id}", dependencies=[Depends(require_api_key)])
def get_account_mail_detail(
    account_id: int,
    mail_id: str,
    folder: str = Query(default="inbox"),
    db: Session = Depends(get_db),
):
    account = db.query(MailAccount).filter(MailAccount.id == account_id).first()
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")

    try:
        mail_detail = load_single_mail_with_protocol(account, db, mail_id=mail_id, folder=folder)
        if mail_detail is None:
            raise HTTPException(status_code=404, detail="mail not found")
        return mail_detail
    except MailServiceError as exc:
        raise HTTPException(status_code=400, detail=exc.message)


@app.get("/api/token-refresh-logs", dependencies=[Depends(require_api_key)])
def list_token_refresh_logs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
):
    if page_size not in ALLOWED_PAGE_SIZES:
        raise HTTPException(status_code=400, detail="每页条数仅支持 10、30、50")

    query = db.query(TokenRefreshLog)
    total = query.count()
    total_pages = max((total + page_size - 1) // page_size, 1)
    items = (
        query.order_by(TokenRefreshLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "items": [serialize_token_refresh_log(item) for item in items],
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        },
    }


@app.get("/token-refresh-logs/{log_id}/preview", response_class=HTMLResponse)
def preview_token_refresh_log_html(
    log_id: int,
    db: Session = Depends(get_db),
):
    log = db.query(TokenRefreshLog).filter(TokenRefreshLog.id == log_id).first()
    if log is None:
        raise HTTPException(status_code=404, detail="log not found")
    if not (log.html_content or "").strip():
        raise HTTPException(status_code=404, detail="html not found")
    return HTMLResponse(content=log.html_content)


@app.post("/api/token-refresh-logs/trigger", dependencies=[Depends(require_api_key)])
def trigger_token_refresh_task():
    try:
        start_token_refresh_job_async(trigger_type="manual")
    except TokenRefreshTaskRunningError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {
        "ok": True,
        "message": "刷新任务已开始，正在后台执行",
    }


# ── API Key 管理 ──

@app.get("/api/keys", dependencies=[Depends(require_api_key)])
def list_api_keys(db: Session = Depends(get_db)):
    keys = db.query(ApiKey).order_by(ApiKey.id.desc()).all()
    return {
        "items": [
            {
                "id": key.id,
                "name": key.name,
                "key": key.key,
                "created_at": key.created_at,
                "last_used_at": key.last_used_at,
            }
            for key in keys
        ]
    }


@app.post("/api/keys", dependencies=[Depends(require_api_key)])
def create_api_key(body: ApiKeyBody, db: Session = Depends(get_db)):
    now = int(time.time())
    api_key = ApiKey(
        name=body.name.strip(),
        key="ms_" + secrets.token_hex(24),  # 48字符 + 前缀
        created_at=now,
    )
    db.add(api_key)
    db.commit()
    db.refresh(api_key)
    return {
        "id": api_key.id,
        "name": api_key.name,
        "key": api_key.key,
        "created_at": api_key.created_at,
    }


@app.delete("/api/keys/{key_id}", dependencies=[Depends(require_api_key)])
def delete_api_key(key_id: int, db: Session = Depends(get_db)):
    api_key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if api_key is None:
        raise HTTPException(status_code=404, detail="api key not found")
    db.delete(api_key)
    db.commit()
    return {"ok": True}


# ── 代理池管理 ──

class ProxyImportBody(BaseModel):
    text: str


@app.get("/api/proxies", dependencies=[Depends(require_api_key)])
def list_proxies(db: Session = Depends(get_db)):
    proxies = db.query(Proxy).order_by(Proxy.id.desc()).all()
    return {
        "items": [
            {
                "id": p.id,
                "name": p.name,
                "proxy_type": p.proxy_type,
                "host": p.host,
                "port": p.port,
                "username": p.username,
                "status": p.status,
                "use_count": p.use_count,
                "latency_ms": p.latency_ms,
                "exit_ip": p.exit_ip,
                "purity_info": p.purity_info,
                "last_used_at": p.last_used_at,
                "last_checked_at": p.last_checked_at,
                "created_at": p.created_at,
            }
            for p in proxies
        ]
    }


@app.post("/api/proxies", dependencies=[Depends(require_api_key)])
def add_proxy(
    host: str = Query(),
    port: int = Query(),
    proxy_type: str = Query(default="http"),
    username: str = Query(default=""),
    password: str = Query(default=""),
    db: Session = Depends(get_db),
):
    now = int(time.time())
    proxy = Proxy(
        name=f"{host}:{port}",
        proxy_type=proxy_type,
        host=host,
        port=port,
        username=username,
        password=password,
        status=1,
        created_at=now,
    )
    db.add(proxy)
    db.commit()
    db.refresh(proxy)
    return {"id": proxy.id, "name": proxy.name, "ok": True}


@app.post("/api/proxies/import", dependencies=[Depends(require_api_key)])
def import_proxies(body: ProxyImportBody, db: Session = Depends(get_db)):
    lines = body.text.splitlines()
    inserted = 0
    updated = 0
    skipped = 0
    now = int(time.time())

    for raw_line in lines:
        parsed = import_proxy_line(raw_line)
        if not parsed:
            skipped += 1
            continue

        existing = (
            db.query(Proxy)
            .filter(Proxy.host == parsed["host"], Proxy.port == parsed["port"])
            .first()
        )
        if existing:
            existing.proxy_type = parsed["proxy_type"]
            existing.username = parsed["username"]
            existing.password = parsed["password"]
            existing.name = parsed["name"]
            existing.status = 1
            updated += 1
            continue

        proxy = Proxy(
            name=parsed["name"],
            proxy_type=parsed["proxy_type"],
            host=parsed["host"],
            port=parsed["port"],
            username=parsed["username"],
            password=parsed["password"],
            status=1,
            created_at=now,
        )
        db.add(proxy)
        inserted += 1

    db.commit()
    return {"inserted": inserted, "updated": updated, "skipped": skipped}


@app.delete("/api/proxies/{proxy_id}", dependencies=[Depends(require_api_key)])
def delete_proxy(proxy_id: int, db: Session = Depends(get_db)):
    proxy = db.query(Proxy).filter(Proxy.id == proxy_id).first()
    if proxy is None:
        raise HTTPException(status_code=404, detail="proxy not found")
    db.delete(proxy)
    db.commit()
    return {"ok": True}


@app.post("/api/proxies/{proxy_id}/status", dependencies=[Depends(require_api_key)])
def toggle_proxy_status(proxy_id: int, status: int = Query(), db: Session = Depends(get_db)):
    proxy = db.query(Proxy).filter(Proxy.id == proxy_id).first()
    if proxy is None:
        raise HTTPException(status_code=404, detail="proxy not found")
    proxy.status = 1 if status else 0
    db.commit()
    return {"ok": True, "status": proxy.status}


@app.post("/api/proxies/{proxy_id}/type", dependencies=[Depends(require_api_key)])
def switch_proxy_type(proxy_id: int, proxy_type: str = Query(), db: Session = Depends(get_db)):
    proxy = db.query(Proxy).filter(Proxy.id == proxy_id).first()
    if proxy is None:
        raise HTTPException(status_code=404, detail="proxy not found")
    if proxy_type not in ("http", "socks5"):
        raise HTTPException(status_code=400, detail="type must be http or socks5")
    proxy.proxy_type = proxy_type
    db.commit()
    return {"ok": True, "proxy_type": proxy.proxy_type}


@app.post("/api/proxies/check", dependencies=[Depends(require_api_key)])
def check_all_proxies(db: Session = Depends(get_db)):
    test_proxies_status(db)
    available = db.query(Proxy).filter(Proxy.status == 1).count()
    total = db.query(Proxy).count()
    return {"available": available, "total": total}


@app.get("/health")
def health():
    return {"ok": True}


# ── 设置 & 版本检查 ─────────────────────────────────────
class SettingsBody(BaseModel):
    github_repo: str = ""


@app.get("/api/version")
def get_version():
    """返回当前应用版本号和已配置的 GitHub 仓库地址"""
    settings = load_settings()
    return {
        "version": APP_VERSION,
        "github_repo": settings.get("github_repo", "") or DEFAULT_GITHUB_REPO,
    }


@app.get("/api/check-update")
def check_update():
    """检查 GitHub releases 是否有新版本

    - 从 settings.json 读取 github_repo(格式: owner/repo)
    - 调用 GitHub API /repos/{owner}/{repo}/releases/latest
    - 比较 tag_name 与当前 APP_VERSION
    - 返回 { has_update, current_version, latest_version, release_url, ... }
    """
    settings = load_settings()
    github_repo = (settings.get("github_repo") or "").strip() or DEFAULT_GITHUB_REPO
    if not github_repo:
        return {
            "has_update": False,
            "error": "未配置 GitHub 仓库地址，请先在设置中填写",
            "current_version": APP_VERSION,
        }

    # 规范化: 去掉可能的 https://github.com/ 前缀
    github_repo = github_repo.replace("https://github.com/", "").replace("http://github.com/", "").strip("/")
    if github_repo.count("/") != 1:
        return {
            "has_update": False,
            "error": "GitHub 仓库地址格式不正确，应为 owner/repo",
            "current_version": APP_VERSION,
        }

    try:
        # requests 会自动读取 HTTP_PROXY/HTTPS_PROXY 环境变量,
        # 如果用户在系统/启动脚本中设置了代理变量即可走代理
        response = requests.get(
            f"https://api.github.com/repos/{github_repo}/releases/latest",
            timeout=15,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "MicroSoftEmailManage-UpdateChecker",
            },
        )
        if response.status_code == 404:
            return {
                "has_update": False,
                "error": "GitHub 上尚未发布任何 release",
                "current_version": APP_VERSION,
            }
        response.raise_for_status()
        data = response.json()

        latest_version = (data.get("tag_name") or "").lstrip("vV")
        release_url = data.get("html_url") or ""
        release_notes = data.get("body") or ""
        release_name = data.get("name") or ""
        published_at = data.get("published_at") or ""

        has_update = compare_versions(latest_version, APP_VERSION) > 0

        return {
            "has_update": has_update,
            "current_version": APP_VERSION,
            "latest_version": latest_version,
            "release_url": release_url,
            "release_notes": release_notes,
            "release_name": release_name,
            "published_at": published_at,
            "error": None,
        }
    except Exception as exc:
        return {
            "has_update": False,
            "error": f"检查更新失败: {exc}",
            "current_version": APP_VERSION,
        }


@app.post("/api/settings", dependencies=[Depends(require_api_key)])
def save_settings_api(body: SettingsBody):
    """保存 GitHub 仓库地址到 settings.json"""
    settings = load_settings()
    github_repo = (body.github_repo or "").strip()
    # 规范化: 去掉可能的 https://github.com/ 前缀
    github_repo = github_repo.replace("https://github.com/", "").replace("http://github.com/", "").strip("/")
    settings["github_repo"] = github_repo
    ok = save_settings(settings)
    if not ok:
        raise HTTPException(status_code=500, detail="保存设置失败")
    return {"ok": True, "github_repo": github_repo}


@app.post("/api/perform-update", dependencies=[Depends(require_api_key)])
def perform_update():
    """流式下载 GitHub Release 源码 zip 并覆盖项目文件,实时返回更新进度(NDJSON)。

    每行一个 JSON: {"stage": "...", "message": "...", "progress": 0-100, ...}
    最终行: {"stage": "done"/"error", ...}
    """
    import tempfile
    import zipfile
    import shutil
    from fastapi.responses import StreamingResponse

    settings = load_settings()
    github_repo = (settings.get("github_repo") or "").strip() or DEFAULT_GITHUB_REPO
    if not github_repo:
        github_repo = DEFAULT_GITHUB_REPO
    github_repo = github_repo.replace("https://github.com/", "").replace("http://github.com/", "").strip("/")

    PRESERVE_FILES = ["settings.json", "mail.db", ".env", ".env.local", ".env.production"]
    PRESERVE_DIRS = ["data", "logs"]
    PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

    def _emit(stage, message, progress=None, **extra):
        payload = {"stage": stage, "message": message}
        if progress is not None:
            payload["progress"] = progress
        payload.update(extra)
        return json.dumps(payload, ensure_ascii=False) + "\n"

    def generate():
        temp_zip_path = None
        extract_dir = None
        backup_dir = None
        try:
            # ── 阶段 1: 获取最新 release 信息 ──
            yield _emit("start", "正在初始化更新流程…", 5)
            if github_repo.count("/") != 1:
                yield _emit("error", "GitHub 仓库地址格式不正确，应为 owner/repo", 0,
                            error_type="config", suggestion="请在设置中填写正确的 GitHub 仓库地址（格式：owner/repo）")
                return

            yield _emit("fetching", "正在获取 GitHub 最新 Release 信息…", 10)
            try:
                release_resp = requests.get(
                    f"https://api.github.com/repos/{github_repo}/releases/latest",
                    timeout=15,
                    headers={"Accept": "application/vnd.github+json", "User-Agent": "MicroSoftEmailManage-UpdateChecker"},
                )
                if release_resp.status_code == 404:
                    yield _emit("error", f"GitHub 仓库 {github_repo} 上尚未发布任何 Release", 10,
                                error_type="no_release", suggestion=f"请在 https://github.com/{github_repo}/releases/new 创建 Release（tag 如 v1.0.0）")
                    return
                release_resp.raise_for_status()
            except requests.RequestException as exc:
                yield _emit("error", f"获取 Release 信息失败: {exc}", 10,
                            error_type="network", suggestion="请检查网络连接。如果在国内，请配置 HTTP_PROXY / HTTPS_PROXY 环境变量后重启服务再试")
                return

            release_data = release_resp.json()
            latest_version = (release_data.get("tag_name") or "").lstrip("vV")
            release_url = release_data.get("html_url", "")

            if not latest_version:
                yield _emit("error", "无法解析最新版本号（Release 的 tag_name 为空）", 15,
                            error_type="parse_error", suggestion="请在 GitHub Release 中设置正确的 tag（如 v1.0.1）")
                return

            if compare_versions(latest_version, APP_VERSION) <= 0:
                yield _emit("error", f"当前版本 {APP_VERSION} 已是最新（最新 Release: {latest_version}）", 15,
                            error_type="up_to_date", suggestion="无需更新")
                return

            yield _emit("version_checked", f"发现新版本 {latest_version}（当前 {APP_VERSION}）", 20,
                        latest_version=latest_version, current_version=APP_VERSION)

            # ── 阶段 2: 下载源码 zip ──
            zipball_url = release_data.get("zipball_url") or f"https://github.com/{github_repo}/archive/refs/tags/{release_data.get('tag_name', '')}.zip"
            yield _emit("downloading", "正在下载源码包…（可能需要 30 秒 - 2 分钟）", 30)

            try:
                zip_resp = requests.get(zipball_url, timeout=120, stream=True,
                                         headers={"User-Agent": "MicroSoftEmailManage-UpdateChecker"})
                zip_resp.raise_for_status()
            except requests.RequestException as exc:
                yield _emit("error", f"下载源码包失败: {exc}", 30,
                            error_type="download_failed", suggestion="请检查网络连接。如果反复失败，请手动从 GitHub 下载源码 zip 并解压覆盖项目文件")
                return

            # 保存到临时文件
            tf = tempfile.NamedTemporaryFile(delete=False, suffix=".zip", dir=PROJECT_ROOT)
            temp_zip_path = tf.name
            try:
                downloaded = 0
                for chunk in zip_resp.iter_content(chunk_size=65536):
                    if chunk:
                        tf.write(chunk)
                        downloaded += len(chunk)
                tf.close()
                yield _emit("downloaded", f"下载完成（{downloaded // 1024} KB）", 50)
            except Exception as exc:
                tf.close()
                yield _emit("error", f"写入临时文件失败: {exc}", 50,
                            error_type="io_error", suggestion="请检查项目目录的磁盘空间和写入权限")
                return

            # ── 阶段 3: 解压 ──
            yield _emit("extracting", "正在解压源码包…", 60)
            extract_dir = tempfile.mkdtemp(prefix="mse_update_", dir=PROJECT_ROOT)
            try:
                with zipfile.ZipFile(temp_zip_path, "r") as zf:
                    zf.extractall(extract_dir)
            except zipfile.BadZipFile as exc:
                yield _emit("error", f"解压失败（zip 文件损坏）: {exc}", 60,
                            error_type="bad_zip", suggestion="下载的文件可能损坏，请重试。如果反复失败，请手动从 GitHub 下载")
                return

            entries = os.listdir(extract_dir)
            if len(entries) == 1 and os.path.isdir(os.path.join(extract_dir, entries[0])):
                source_root = os.path.join(extract_dir, entries[0])
            else:
                source_root = extract_dir

            yield _emit("extracted", "解压完成", 65)

            # ── 阶段 4: 备份本地数据 ──
            yield _emit("backing_up", "正在备份本地数据（settings.json / mail.db / data/）…", 75)
            backups = {}
            backup_dir = tempfile.mkdtemp(prefix="mse_backup_", dir=PROJECT_ROOT)
            for fname in PRESERVE_FILES:
                src = os.path.join(PROJECT_ROOT, fname)
                if os.path.isfile(src):
                    dst = os.path.join(backup_dir, fname)
                    shutil.copy2(src, dst)
                    backups[fname] = dst
            for dname in PRESERVE_DIRS:
                src = os.path.join(PROJECT_ROOT, dname)
                if os.path.isdir(src):
                    dst = os.path.join(backup_dir, dname)
                    shutil.copytree(src, dst)
                    backups[dname] = dst
            yield _emit("backed_up", f"已备份 {len(backups)} 个本地数据项", 80)

            # ── 阶段 5: 覆盖项目文件 ──
            yield _emit("applying", "正在应用更新（覆盖项目文件）…", 85)
            skipped_files = []
            for item in os.listdir(source_root):
                src_path = os.path.join(source_root, item)
                dst_path = os.path.join(PROJECT_ROOT, item)
                if item in PRESERVE_FILES or item in PRESERVE_DIRS:
                    continue
                try:
                    if os.path.isdir(dst_path):
                        shutil.rmtree(dst_path, ignore_errors=True)
                    elif os.path.exists(dst_path):
                        os.remove(dst_path)
                except Exception:
                    pass
                try:
                    if os.path.isdir(src_path):
                        shutil.copytree(src_path, dst_path, dirs_exist_ok=True)
                    else:
                        shutil.copy2(src_path, dst_path)
                except PermissionError:
                    skipped_files.append(item)
                except Exception as exc:
                    skipped_files.append(f"{item} ({exc})")

            # 恢复备份的本地数据
            for rel_path, backup_path in backups.items():
                dst = os.path.join(PROJECT_ROOT, rel_path)
                try:
                    if os.path.isdir(backup_path):
                        if os.path.exists(dst):
                            shutil.rmtree(dst)
                        shutil.copytree(backup_path, dst)
                    else:
                        shutil.copy2(backup_path, dst)
                except Exception:
                    pass

            yield _emit("applied", f"文件覆盖完成（跳过 {len(skipped_files)} 个被占用文件）", 92,
                        skipped_files=skipped_files)

            # ── 阶段 6: 清理 ──
            yield _emit("cleaning", "正在清理临时文件和 __pycache__…", 96)
            for root, dirs, files in os.walk(PROJECT_ROOT, topdown=False):
                for d in dirs:
                    if d == "__pycache__":
                        shutil.rmtree(os.path.join(root, d), ignore_errors=True)

            yield _emit("done", "更新完成，正在准备重启服务…", 100,
                        previous_version=APP_VERSION,
                        latest_version=latest_version,
                        release_url=release_url,
                        skipped_files=skipped_files,
                        suggestion="请重启服务使更新生效（关闭当前命令行窗口，重新执行 python icutool_mail.py）")

            # ── 自动重启 ──
            # 启动守护线程:等待响应发送完毕 → 启动重启脚本 → 退出当前进程
            import sys
            import threading
            import platform

            python_exe = sys.executable
            is_windows = platform.system() == "Windows"

            def _is_running_in_docker():
                """检测是否在 Docker 容器中运行"""
                if os.path.exists("/.dockerenv"):
                    return True
                try:
                    with open("/proc/1/cgroup", "r") as f:
                        content = f.read()
                        if "docker" in content or "containerd" in content:
                            return True
                except Exception:
                    pass
                return False

            def _delayed_restart():
                # 等待 StreamingResponse 发送完毕
                time.sleep(3)
                try:
                    # Docker 环境:直接退出主进程
                    # docker-compose.yml 配置了 restart: unless-stopped
                    # 容器停止后会自动重启,加载已更新的代码
                    if _is_running_in_docker():
                        logger.info("检测到 Docker 环境,直接退出进程,Docker restart 策略会自动重启容器")
                        os._exit(0)
                        return

                    # 非 Docker 环境:创建重启脚本 → 启动 → 退出
                    if is_windows:
                        bat_path = os.path.join(PROJECT_ROOT, "_restart.bat")
                        bat_content = (
                            "@echo off\r\n"
                            "timeout /t 3 /nobreak >nul\r\n"
                            f'cd /d "{PROJECT_ROOT}"\r\n'
                            f'"{python_exe}" icutool_mail.py\r\n'
                            "del _restart.bat\r\n"
                        )
                        with open(bat_path, "w", encoding="utf-8") as f:
                            f.write(bat_content)
                        creation_flags = 0
                        if hasattr(subprocess, "DETACHED_PROCESS"):
                            creation_flags |= subprocess.DETACHED_PROCESS
                        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                            creation_flags |= subprocess.CREATE_NEW_PROCESS_GROUP
                        subprocess.Popen(
                            ["cmd", "/c", bat_path],
                            creationflags=creation_flags,
                            close_fds=True,
                            cwd=PROJECT_ROOT,
                        )
                    else:
                        sh_path = os.path.join(PROJECT_ROOT, "_restart.sh")
                        sh_content = (
                            "#!/bin/bash\n"
                            "sleep 3\n"
                            f'cd "{PROJECT_ROOT}"\n'
                            f'"{python_exe}" icutool_mail.py\n'
                            "rm -f _restart.sh\n"
                        )
                        with open(sh_path, "w", encoding="utf-8") as f:
                            f.write(sh_content)
                        os.chmod(sh_path, 0o755)
                        subprocess.Popen(
                            ["bash", sh_path],
                            start_new_session=True,
                            close_fds=True,
                            cwd=PROJECT_ROOT,
                        )
                    logger.info("已触发自动重启,3 秒后退出当前进程")
                    time.sleep(1)
                    os._exit(0)
                except Exception as exc:
                    logger.warning("自动重启失败: %s,请手动重启服务", exc)

            yield _emit("restarting", "正在重启服务…（服务将短暂不可用，页面会自动刷新）", 100,
                        previous_version=APP_VERSION,
                        latest_version=latest_version)

            threading.Thread(target=_delayed_restart, daemon=True).start()

        except Exception as exc:
            logger.warning("自动更新失败: %s", exc, exc_info=True)
            error_type = type(exc).__name__
            suggestion = "请查看服务端日志获取详细信息。常见原因：\n1) 文件被占用 → 先停止服务再更新\n2) 网络问题 → 配置代理后重试\n3) 权限不足 → 用管理员权限运行"
            yield _emit("error", f"更新过程中发生异常: {exc}（{error_type}）", 0,
                        error_type=error_type, suggestion=suggestion)
        finally:
            # 清理临时文件
            if temp_zip_path:
                try:
                    os.unlink(temp_zip_path)
                except Exception:
                    pass
            if extract_dir:
                shutil.rmtree(extract_dir, ignore_errors=True)
            if backup_dir:
                shutil.rmtree(backup_dir, ignore_errors=True)

    return StreamingResponse(generate(), media_type="application/x-ndjson")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=10019)