import secrets
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses
from html import unescape
from html.parser import HTMLParser
import re

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import ChatgptEmailClaim, MailAccount


ACTIVE_LEASE_SECONDS = 15 * 60
CODE_FOUND_LEASE_SECONDS = 24 * 60 * 60
COMPLETED_RECEIPT_SECONDS = 24 * 60 * 60
REGISTERED_TAG = "已注册chatgpt"
EXPECTED_SENDER = "noreply@tm.openai.com"
EXPECTED_SUBJECT = "Your temporary ChatGPT verification code"
BODY_CODE_RE = re.compile(
    r"Enter\s+this\s+temporary\s+verification\s+code\s+to\s+continue:\s*(?<!\d)(\d{6})(?!\d)",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
PROJECT_ACTUAL_ADDRESS_RE = re.compile(
    rf"(?P<separator>^|,)\s*[^,]*?\(\s*(?P<address>{EMAIL_RE.pattern})\s*\)\s*(?=,|$)",
    re.IGNORECASE,
)
SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in {"script", "style"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def _visible_text(value: str) -> str:
    parser = _VisibleTextParser()
    parser.feed(str(value or ""))
    parser.close()
    return " ".join(unescape(" ".join(parser.parts)).split())


def extract_chatgpt_code(body: str) -> str:
    match = BODY_CODE_RE.search(_visible_text(body))
    return match.group(1) if match else ""


def _parse_received_datetime(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value):
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI_TZ)
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _addresses(value) -> set[str]:
    normalized = PROJECT_ACTUAL_ADDRESS_RE.sub(
        lambda match: f"{match.group('separator')} <{match.group('address')}>",
        str(value or ""),
    )
    return {
        address.strip().casefold()
        for _display_name, address in getaddresses([normalized])
        if EMAIL_RE.fullmatch(address.strip())
    }


def find_latest_chatgpt_code(
    folder_mails: dict[str, list[dict]], email: str, not_before_ms: int
) -> dict | None:
    try:
        earliest_ms = int(not_before_ms) - 120000
    except (TypeError, ValueError):
        return None

    expected_recipient = str(email or "").strip().casefold()
    if not expected_recipient:
        return None

    latest: tuple[datetime, str, str] | None = None
    for folder, mails in folder_mails.items():
        for mail in mails or []:
            if not isinstance(mail, dict):
                continue
            if _addresses(mail.get("mail_from")) != {EXPECTED_SENDER}:
                continue
            if str(mail.get("subject") or "").strip().casefold() != EXPECTED_SUBJECT.casefold():
                continue
            if expected_recipient not in _addresses(mail.get("mail_to")):
                continue
            received_at = _parse_received_datetime(mail.get("mail_dt"))
            if received_at is None or int(received_at.timestamp() * 1000) < earliest_ms:
                continue
            code = extract_chatgpt_code(str(mail.get("body") or ""))
            if not code:
                continue
            candidate = (received_at, str(folder), code)
            if latest is None or candidate[0] > latest[0]:
                latest = candidate

    if latest is None:
        return None
    received_at, folder, code = latest
    return {"code": code, "received_at": received_at.isoformat(), "folder": folder}


class AutomationError(Exception):
    def __init__(self, code: str, status_code: int, message: str):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.message = message


def parse_tags(value: str) -> list[str]:
    result = []
    for raw in str(value or "").replace("，", ",").split(","):
        tag = raw.strip()
        if tag and tag not in result:
            result.append(tag)
    return result


def has_exact_tag(value: str, tag: str) -> bool:
    return tag in parse_tags(value)


def append_exact_tag(value: str, tag: str) -> str:
    tags = parse_tags(value)
    if tag not in tags:
        tags.append(tag)
    return ",".join(tags)


def _now(now: int | None) -> int:
    return int(time.time()) if now is None else int(now)


def _claim_error(code: str, status_code: int, message: str) -> AutomationError:
    return AutomationError(code, status_code, message)


def claim_email(
    db: Session,
    now: int | None = None,
    token_factory: Callable[[], str] | None = None,
) -> dict:
    claimed_at = _now(now)
    make_token = token_factory or (lambda: secrets.token_urlsafe(32))

    for _ in range(3):
        try:
            if db.in_transaction():
                db.rollback()
            db.execute(text("BEGIN IMMEDIATE"))
            db.query(ChatgptEmailClaim).filter(
                ChatgptEmailClaim.status == "active",
                ChatgptEmailClaim.expires_at <= claimed_at,
            ).delete(synchronize_session=False)
            db.query(ChatgptEmailClaim).filter(
                ChatgptEmailClaim.status == "completed",
                ChatgptEmailClaim.completed_at <= claimed_at - COMPLETED_RECEIPT_SECONDS,
            ).delete(synchronize_session=False)

            claimed_account_ids = {
                account_id
                for (account_id,) in db.query(ChatgptEmailClaim.mail_account_id).all()
            }
            accounts = (
                db.query(MailAccount)
                .filter(MailAccount.valid_status == 1)
                .order_by(MailAccount.id.asc())
                .all()
            )
            account = next(
                (
                    candidate
                    for candidate in accounts
                    if candidate.id not in claimed_account_ids
                    and not has_exact_tag(candidate.tags, REGISTERED_TAG)
                ),
                None,
            )
            if account is None:
                db.commit()
                raise _claim_error("no_available_email", 409, "没有可用邮箱")

            claim_token = make_token()
            expires_at = claimed_at + ACTIVE_LEASE_SECONDS
            db.add(
                ChatgptEmailClaim(
                    mail_account_id=account.id,
                    claim_token=claim_token,
                    status="active",
                    claimed_at=claimed_at,
                    expires_at=expires_at,
                    completed_at=0,
                )
            )
            db.commit()
            return {
                "email": account.email,
                "claim_token": claim_token,
                "expires_at": expires_at,
            }
        except IntegrityError:
            db.rollback()

    raise _claim_error("claim_conflict", 409, "领取冲突，请重试")


def resolve_active_claim(
    db: Session,
    claim_token: str,
    now: int | None = None,
) -> tuple[ChatgptEmailClaim, MailAccount]:
    claim = db.query(ChatgptEmailClaim).filter_by(claim_token=claim_token).one_or_none()
    if claim is None:
        raise _claim_error("claim_not_found", 404, "领取不存在")
    if claim.status == "completed":
        raise _claim_error("claim_completed", 409, "领取已完成")
    if claim.expires_at <= _now(now):
        raise _claim_error("claim_expired", 410, "领取已过期")

    account = db.query(MailAccount).filter_by(id=claim.mail_account_id).one_or_none()
    if account is None:
        raise _claim_error("claim_not_found", 404, "领取不存在")
    return claim, account


def renew_claim(
    db: Session,
    claim: ChatgptEmailClaim,
    now: int | None = None,
    code_found: bool = False,
) -> int:
    current_time = _now(now)
    if claim.status == "completed":
        raise _claim_error("claim_completed", 409, "领取已完成")
    if claim.expires_at <= current_time:
        raise _claim_error("claim_expired", 410, "领取已过期")

    lease_seconds = CODE_FOUND_LEASE_SECONDS if code_found else ACTIVE_LEASE_SECONDS
    claim.expires_at = current_time + lease_seconds
    db.commit()
    return claim.expires_at


def complete_claim(db: Session, claim_token: str, now: int | None = None) -> dict:
    completed_at = _now(now)
    if db.in_transaction():
        db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    claim = db.query(ChatgptEmailClaim).filter_by(claim_token=claim_token).one_or_none()
    if claim is None:
        db.rollback()
        raise _claim_error("claim_not_found", 404, "领取不存在")
    if claim.status == "completed":
        db.commit()
        return {"ok": True, "status": "completed"}
    if claim.expires_at <= completed_at:
        db.rollback()
        raise _claim_error("claim_expired", 410, "领取已过期")

    account = db.query(MailAccount).filter_by(id=claim.mail_account_id).one_or_none()
    if account is None:
        db.rollback()
        raise _claim_error("claim_not_found", 404, "领取不存在")
    account.tags = append_exact_tag(account.tags, REGISTERED_TAG)
    claim.status = "completed"
    claim.completed_at = completed_at
    db.commit()
    return {"ok": True, "status": "completed"}


def release_claim(db: Session, claim_token: str) -> bool:
    claim = db.query(ChatgptEmailClaim).filter_by(claim_token=claim_token).one_or_none()
    if claim is None:
        db.commit()
        return False
    if claim.status == "completed":
        db.commit()
        raise _claim_error("claim_completed", 409, "领取已完成")

    deleted = (
        db.query(ChatgptEmailClaim)
        .filter(
            ChatgptEmailClaim.id == claim.id,
            ChatgptEmailClaim.status == "active",
        )
        .delete(synchronize_session=False)
    )
    db.commit()
    if deleted:
        return True

    claim = db.query(ChatgptEmailClaim).filter_by(claim_token=claim_token).one_or_none()
    if claim is not None and claim.status == "completed":
        db.commit()
        raise _claim_error("claim_completed", 409, "领取已完成")
    db.commit()
    return False
