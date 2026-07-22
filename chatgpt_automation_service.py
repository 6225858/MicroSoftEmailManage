import secrets
import time
from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import ChatgptEmailClaim, MailAccount


ACTIVE_LEASE_SECONDS = 15 * 60
CODE_FOUND_LEASE_SECONDS = 24 * 60 * 60
COMPLETED_RECEIPT_SECONDS = 24 * 60 * 60
REGISTERED_TAG = "已注册chatgpt"


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
