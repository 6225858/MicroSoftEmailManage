import json
import logging
import threading
import time
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

from database import SessionLocal
from mail_service import MailServiceError, get_mail_body_render_mode, load_account_mails
from models import MailAccount, TokenRefreshLog


logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False

REFRESH_HOUR = 3
ALLOWED_PAGE_SIZES = {10, 30, 50}

_refresh_job_lock = threading.Lock()
_scheduler_stop_event = threading.Event()
_scheduler_thread = None


class TokenRefreshTaskRunningError(Exception):
    pass


def _next_run_at(now: datetime | None = None) -> datetime:
    current = now or datetime.now()
    target = current.replace(hour=REFRESH_HOUR, minute=0, second=0, microsecond=0)
    if current >= target:
        target += timedelta(days=1)
    return target


def _serialize_failure_details(failures: list[dict[str, str]]) -> str:
    return json.dumps(failures, ensure_ascii=False)


def _render_export_mail_body(mail_item: dict[str, str]) -> str:
    render_mode = get_mail_body_render_mode(mail_item.get("body"))
    content = render_mode["content"]
    if render_mode["type"] == "iframe":
        return (
            '<iframe class="mail-frame" '
            'sandbox="allow-popups allow-popups-to-escape-sandbox" '
            'referrerpolicy="no-referrer" '
            f'srcdoc="{escape(content, quote=True)}"></iframe>'
        )
    return f'<div class="mail-inline-body">{content}</div>'


def _render_export_account_card(item: dict[str, str]) -> str:
    status = item["status"]
    if status == "failed":
        body_html = f'<div class="mail-error">Fetch failed: {escape(item["error"])}</div>'
    elif status == "empty":
        body_html = '<div class="mail-empty">No mail found for this account.</div>'
    else:
        body_html = (
            '<div class="mail-meta">'
            f'<div><strong>Subject:</strong> {escape(item["subject"] or "-")}</div>'
            f'<div><strong>From:</strong> {escape(item["mail_from"] or "-")}</div>'
            f'<div><strong>To:</strong> {escape(item["mail_to"] or "-")}</div>'
            f'<div><strong>Time:</strong> {escape(item["mail_dt"] or "-")}</div>'
            "</div>"
            f"{_render_export_mail_body(item)}"
        )

    status_text_map = {
        "success": "Success",
        "empty": "Empty",
        "failed": "Failed",
    }
    return (
        '<section class="mail-card">'
        '<div class="mail-card-head">'
        f'<h2>{escape(item["email"])}</h2>'
        f'<span class="mail-status status-{status}">{status_text_map[status]}</span>'
        "</div>"
        f"{body_html}"
        "</section>"
    )


def _build_latest_mail_export_html(
    trigger_type: str,
    started_at: int,
    finished_at: int,
    total_count: int,
    success_count: int,
    failed_count: int,
    account_results: list[dict[str, str]],
) -> str:
    generated_at = datetime.fromtimestamp(finished_at).strftime("%Y-%m-%d %H:%M:%S")
    started_at_text = datetime.fromtimestamp(started_at).strftime("%Y-%m-%d %H:%M:%S")
    cards_html = "".join(_render_export_account_card(item) for item in account_results)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Latest Mail Export {generated_at}</title>
    <style>
        :root {{
            color-scheme: light;
            --bg: #f4f7fb;
            --panel: #ffffff;
            --line: #d9e2ec;
            --text: #102a43;
            --muted: #627d98;
            --success-bg: #e3fcec;
            --success-text: #12703d;
            --warning-bg: #fff7d6;
            --warning-text: #8d6b00;
            --danger-bg: #ffe3e3;
            --danger-text: #b42318;
        }}
        * {{
            box-sizing: border-box;
        }}
        body {{
            margin: 0;
            background: linear-gradient(180deg, #edf2f7 0%, var(--bg) 100%);
            color: var(--text);
            font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
        }}
        .page {{
            width: min(1440px, calc(100% - 32px));
            margin: 0 auto;
            padding: 24px 0 40px;
        }}
        .summary {{
            background: var(--panel);
            border: 1px solid var(--line);
            border-radius: 16px;
            padding: 20px 22px;
            box-shadow: 0 12px 32px rgba(15, 23, 42, 0.06);
            margin-bottom: 20px;
        }}
        .summary h1 {{
            margin: 0 0 8px;
            font-size: 28px;
        }}
        .summary p {{
            margin: 6px 0;
            color: var(--muted);
        }}
        .mail-list {{
            display: grid;
            gap: 16px;
        }}
        .mail-card {{
            background: var(--panel);
            border: 1px solid var(--line);
            border-radius: 16px;
            padding: 20px;
            box-shadow: 0 10px 24px rgba(15, 23, 42, 0.05);
        }}
        .mail-card-head {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            margin-bottom: 16px;
        }}
        .mail-card-head h2 {{
            margin: 0;
            font-size: 20px;
            word-break: break-all;
        }}
        .mail-status {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-width: 72px;
            padding: 6px 10px;
            border-radius: 999px;
            font-size: 13px;
            font-weight: 600;
        }}
        .status-success {{
            background: var(--success-bg);
            color: var(--success-text);
        }}
        .status-empty {{
            background: var(--warning-bg);
            color: var(--warning-text);
        }}
        .status-failed {{
            background: var(--danger-bg);
            color: var(--danger-text);
        }}
        .mail-meta {{
            display: grid;
            gap: 8px;
            margin-bottom: 16px;
            color: var(--muted);
            word-break: break-word;
        }}
        .mail-frame {{
            width: 100%;
            min-height: 520px;
            border: 1px solid var(--line);
            border-radius: 12px;
            background: #fff;
        }}
        .mail-inline-body {{
            border: 1px solid var(--line);
            border-radius: 12px;
            background: #fff;
            padding: 16px;
            overflow: auto;
        }}
        .mail-empty, .mail-error {{
            border-radius: 12px;
            padding: 16px;
            font-size: 15px;
        }}
        .mail-empty {{
            background: #fffaf0;
            color: var(--warning-text);
        }}
        .mail-error {{
            background: #fff5f5;
            color: var(--danger-text);
        }}
        @media (max-width: 768px) {{
            .page {{
                width: min(100% - 20px, 100%);
                padding-top: 16px;
            }}
            .mail-card {{
                padding: 16px;
            }}
            .mail-card-head {{
                align-items: flex-start;
                flex-direction: column;
            }}
            .mail-frame {{
                min-height: 420px;
            }}
        }}
    </style>
</head>
<body>
    <main class="page">
        <section class="summary">
            <h1>Latest Mail Export</h1>
            <p>Generated at: {escape(generated_at)}</p>
            <p>Trigger type: {escape(trigger_type)}</p>
            <p>Task window: {escape(started_at_text)} ~ {escape(generated_at)}</p>
            <p>Total accounts: {total_count}, success: {success_count}, failed: {failed_count}</p>
        </section>
        <section class="mail-list">
            {cards_html}
        </section>
    </main>
</body>
</html>
"""


def _write_latest_mail_export_file(
    export_html: str,
    finished_at: int,
) -> Path:
    export_dir = Path(__file__).resolve().parent / "html"
    export_dir.mkdir(parents=True, exist_ok=True)
    export_name = datetime.fromtimestamp(finished_at).strftime("%Y%m%d%H%M%S") + ".html"
    export_path = export_dir / export_name
    export_path.write_text(export_html, encoding="utf-8")
    return export_path


def parse_failure_details(value: str | None) -> list[dict[str, str]]:
    if not value:
        return []

    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return []

    if not isinstance(payload, list):
        return []

    items = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        email = str(item.get("email") or "").strip()
        error = str(item.get("error") or "").strip()
        if not email and not error:
            continue
        items.append({"email": email, "error": error})
    return items


def serialize_token_refresh_log(log: TokenRefreshLog) -> dict:
    return {
        "id": log.id,
        "trigger_type": log.trigger_type,
        "total_count": log.total_count,
        "success_count": log.success_count,
        "failed_count": log.failed_count,
        "failure_items": parse_failure_details(log.failure_details),
        "has_html": bool(log.html_content),
        "started_at": log.started_at,
        "finished_at": log.finished_at,
        "duration_seconds": log.duration_seconds,
        "created_at": log.created_at,
    }


def _acquire_refresh_job_lock() -> None:
    if not _refresh_job_lock.acquire(blocking=False):
        raise TokenRefreshTaskRunningError("Token refresh task is already running, please try again later.")


def _run_token_refresh_job_with_lock(trigger_type: str = "manual") -> TokenRefreshLog:
    trigger_label = "scheduled task" if trigger_type == "scheduled" else "manual trigger"
    logger.info("Starting token refresh job, trigger type: %s", trigger_label)

    with SessionLocal() as db:
        # The refresh job should always scan the full mailbox list without
        # filtering by valid_status, then update that status from the result.
        accounts = db.query(MailAccount).order_by(MailAccount.id.asc()).all()
        started_at = int(time.time())
        failures = []
        success_count = 0
        total_count = len(accounts)
        account_results = []

        for index, account in enumerate(accounts, start=1):
            logger.info("Refreshing account %d/%d: %s", index, total_count, account.email)
            try:
                items = load_account_mails(account, db, folder="inbox", limit=1)
                account.valid_status = 1
                db.commit()
                success_count += 1

                latest_mail = items[0] if items else None
                if latest_mail:
                    account_results.append(
                        {
                            "status": "success",
                            "email": account.email,
                            "subject": latest_mail.get("subject") or "",
                            "mail_from": latest_mail.get("mail_from") or "",
                            "mail_to": latest_mail.get("mail_to") or "",
                            "mail_dt": latest_mail.get("mail_dt") or "",
                            "body": latest_mail.get("body") or "",
                        }
                    )
                else:
                    account_results.append(
                        {
                            "status": "empty",
                            "email": account.email,
                            "subject": "",
                            "mail_from": "",
                            "mail_to": "",
                            "mail_dt": "",
                            "body": "",
                        }
                    )

                logger.info("Refresh succeeded for account %d/%d: %s", index, total_count, account.email)
            except MailServiceError as exc:
                account.valid_status = 0
                db.commit()
                failures.append(
                    {
                        "email": account.email,
                        "error": exc.message,
                    }
                )
                account_results.append(
                    {
                        "status": "failed",
                        "email": account.email,
                        "error": exc.message,
                    }
                )
                logger.error(
                    "Refresh failed for account %d/%d: %s, error: %s",
                    index,
                    total_count,
                    account.email,
                    exc.message,
                )

        finished_at = int(time.time())
        export_html = _build_latest_mail_export_html(
            trigger_type=trigger_type,
            started_at=started_at,
            finished_at=finished_at,
            total_count=total_count,
            success_count=success_count,
            failed_count=len(failures),
            account_results=account_results,
        )
        export_path = _write_latest_mail_export_file(
            export_html=export_html,
            finished_at=finished_at,
        )

        log = TokenRefreshLog(
            trigger_type=trigger_type,
            total_count=total_count,
            success_count=success_count,
            failed_count=len(failures),
            failure_details=_serialize_failure_details(failures),
            html_content=export_html,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=max(finished_at - started_at, 0),
            created_at=finished_at,
        )
        db.add(log)
        db.commit()
        db.refresh(log)

        logger.info(
            "Token refresh job completed, trigger type: %s, success: %d/%d, failed: %d, duration: %ds",
            trigger_label,
            success_count,
            total_count,
            len(failures),
            log.duration_seconds,
        )
        logger.info("Latest mail export file generated: %s", export_path)
        return log


def run_token_refresh_job(trigger_type: str = "manual") -> TokenRefreshLog:
    _acquire_refresh_job_lock()
    try:
        return _run_token_refresh_job_with_lock(trigger_type=trigger_type)
    finally:
        _refresh_job_lock.release()


def start_token_refresh_job_async(trigger_type: str = "manual") -> None:
    _acquire_refresh_job_lock()

    def worker() -> None:
        try:
            _run_token_refresh_job_with_lock(trigger_type=trigger_type)
        except Exception:
            logger.exception("Background token refresh job failed")
        finally:
            _refresh_job_lock.release()

    threading.Thread(
        target=worker,
        name=f"token-refresh-{trigger_type}",
        daemon=True,
    ).start()


def _scheduler_loop() -> None:
    while not _scheduler_stop_event.is_set():
        next_run_at = _next_run_at()
        wait_seconds = max((next_run_at - datetime.now()).total_seconds(), 1)
        if _scheduler_stop_event.wait(wait_seconds):
            break

        try:
            run_token_refresh_job(trigger_type="scheduled")
        except TokenRefreshTaskRunningError:
            logger.info("Skipping scheduled token refresh because another job is already running")
        except Exception:
            logger.exception("Scheduled token refresh job failed")


def start_token_refresh_scheduler() -> None:
    global _scheduler_thread

    if _scheduler_thread and _scheduler_thread.is_alive():
        return

    _scheduler_stop_event.clear()
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop,
        name="token-refresh-scheduler",
        daemon=True,
    )
    _scheduler_thread.start()


def stop_token_refresh_scheduler() -> None:
    global _scheduler_thread

    _scheduler_stop_event.set()
    if _scheduler_thread and _scheduler_thread.is_alive():
        _scheduler_thread.join(timeout=1)
    _scheduler_thread = None
