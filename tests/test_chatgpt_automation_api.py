import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import icutool_mail as automation_api
from chatgpt_automation_service import claim_email
from database import Base
from icutool_mail import (
    ChatgptClaimTokenBody,
    ChatgptVerificationCodeBody,
    claim_chatgpt_email,
    complete_chatgpt_claim,
    get_chatgpt_verification_code,
    release_chatgpt_claim,
    require_automation_api_key,
)
from models import ApiKey, ChatgptEmailClaim, MailAccount


class FakeRefreshTask:
    def __init__(self, error=None):
        self.error = error
        self.event = threading.Event()
        self.event.set()


def matching_mail():
    return {
        "subject": "Your temporary ChatGPT verification code",
        "mail_from": "ChatGPT (noreply@tm.openai.com)",
        "mail_to": "user@outlook.com (user@outlook.com)",
        "mail_dt": "2026-07-22 13:48:45",
        "body": "Enter this temporary verification code to continue: 919020",
    }


class AutomationApiTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "test.db"
        self.engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def add_account(self, email):
        account = MailAccount(email=email, created_at=1, valid_status=1)
        self.db.add(account)
        self.db.commit()
        return account

    def test_strict_auth_rejects_missing_and_invalid_key(self):
        for value in (None, "invalid"):
            with self.subTest(value=value), self.assertRaises(HTTPException) as caught:
                require_automation_api_key(value, self.db)
            self.assertEqual(caught.exception.status_code, 401)
            self.assertEqual(caught.exception.detail["code"], "invalid_api_key")

    def test_strict_auth_accepts_valid_key(self):
        record = ApiKey(name="automation", key="valid-key", created_at=1)
        self.db.add(record)
        self.db.commit()
        self.assertEqual(require_automation_api_key(" valid-key ", self.db).id, record.id)

    def test_claim_route_returns_minimal_response_and_translates_unavailable(self):
        with self.assertRaises(HTTPException) as caught:
            claim_chatgpt_email(db=self.db)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail["code"], "no_available_email")

        self.add_account("user@outlook.com")
        response = claim_chatgpt_email(db=self.db)
        self.assertEqual(set(response), {"email", "claim_token", "expires_at"})
        self.assertEqual(response["email"], "user@outlook.com")

    @patch("icutool_mail.refresh_mail_cache_async")
    @patch("icutool_mail.get_mail_cache")
    def test_verification_route_returns_minimal_response(self, get_cache, refresh):
        account = self.add_account("user@outlook.com")
        claim_email(self.db, now=int(time.time()), token_factory=lambda: "claim-a")
        get_cache.side_effect = [
            {"items": [matching_mail()], "updated_at": 1, "is_fresh": True},
            None,
        ]
        response = get_chatgpt_verification_code(
            ChatgptVerificationCodeBody(claim_token="claim-a", not_before=1784699250000),
            db=self.db,
        )
        self.assertEqual(set(response), {"code", "received_at", "folder"})
        self.assertEqual(response["code"], "919020")
        self.assertEqual(
            [call.args[:2] for call in refresh.call_args_list],
            [(account.id, "inbox"), (account.id, "junk")],
        )

    @patch("icutool_mail.refresh_mail_cache_async")
    @patch("icutool_mail.get_mail_cache")
    def test_verification_reuses_fresh_empty_caches_without_refresh(self, get_cache, refresh):
        now = int(time.time())
        self.add_account("user@outlook.com")
        claim_email(self.db, now=now, token_factory=lambda: "claim-fresh-empty")
        get_cache.side_effect = [
            {"items": [], "updated_at": now - 9, "is_fresh": True},
            {"items": [], "updated_at": now - 9, "is_fresh": True},
        ]

        with patch("icutool_mail.time.time", return_value=now):
            response = get_chatgpt_verification_code(
                ChatgptVerificationCodeBody(
                    claim_token="claim-fresh-empty", not_before=1784699250000
                ),
                db=self.db,
            )

        self.assertEqual(response, {"code": "", "received_at": "", "folder": ""})
        refresh.assert_not_called()

    @patch("icutool_mail.refresh_mail_cache_async")
    @patch("icutool_mail.get_mail_cache")
    def test_verification_refreshes_empty_caches_at_ten_second_boundary(self, get_cache, refresh):
        now = int(time.time())
        account = self.add_account("user@outlook.com")
        claim_email(self.db, now=now, token_factory=lambda: "claim-cooldown")
        expired_empty = {"items": [], "updated_at": now - 10, "is_fresh": True}
        refreshed_empty = {"items": [], "updated_at": now, "is_fresh": True}
        get_cache.side_effect = [expired_empty, expired_empty, refreshed_empty, refreshed_empty]
        refresh.side_effect = [FakeRefreshTask(), FakeRefreshTask()]

        with patch("icutool_mail.time.time", return_value=now):
            response = get_chatgpt_verification_code(
                ChatgptVerificationCodeBody(
                    claim_token="claim-cooldown", not_before=1784699250000
                ),
                db=self.db,
            )

        self.assertEqual(response, {"code": "", "received_at": "", "folder": ""})
        self.assertEqual(
            [call.args[:2] for call in refresh.call_args_list],
            [(account.id, "inbox"), (account.id, "junk")],
        )
        self.assertEqual(get_cache.call_count, 4)

    @patch("icutool_mail.refresh_mail_cache_async")
    @patch("icutool_mail.get_mail_cache")
    def test_verification_waits_once_and_reloads_after_missing_cache(self, get_cache, refresh):
        account = self.add_account("user@outlook.com")
        claim_email(self.db, now=int(time.time()), token_factory=lambda: "claim-wait")
        fresh_empty = {"items": [], "updated_at": int(time.time()), "is_fresh": True}
        get_cache.side_effect = [None, fresh_empty, {"items": [matching_mail()], "is_fresh": True}, fresh_empty]
        task = FakeRefreshTask()
        refresh.return_value = task

        response = get_chatgpt_verification_code(
            ChatgptVerificationCodeBody(claim_token="claim-wait", not_before=1784699250000),
            db=self.db,
        )

        self.assertEqual(response["code"], "919020")
        self.assertEqual(get_cache.call_count, 4)
        self.assertEqual(len(refresh.call_args_list), 1)
        self.assertEqual(refresh.call_args_list[0].args[:2], (account.id, "inbox"))

    @patch("icutool_mail.refresh_mail_cache_async")
    @patch("icutool_mail.get_mail_cache")
    def test_verification_tolerates_one_folder_refresh_failure(self, get_cache, refresh):
        self.add_account("user@outlook.com")
        claim_email(self.db, now=int(time.time()), token_factory=lambda: "claim-partial")
        fresh_empty = {"items": [], "updated_at": int(time.time()), "is_fresh": True}
        get_cache.side_effect = [None, None, None, fresh_empty]
        refresh.side_effect = [FakeRefreshTask("inbox failed"), FakeRefreshTask()]

        response = get_chatgpt_verification_code(
            ChatgptVerificationCodeBody(
                claim_token="claim-partial", not_before=1784699250000
            ),
            db=self.db,
        )

        self.assertEqual(response, {"code": "", "received_at": "", "folder": ""})

    @patch("icutool_mail.refresh_mail_cache_async")
    @patch("icutool_mail.get_mail_cache")
    def test_verification_returns_502_when_all_uncached_refreshes_fail(self, get_cache, refresh):
        self.add_account("user@outlook.com")
        claim_email(self.db, now=int(time.time()), token_factory=lambda: "claim-failed")
        get_cache.side_effect = [None, None, None, None]
        refresh.side_effect = [FakeRefreshTask("inbox failed"), FakeRefreshTask("junk failed")]

        with self.assertRaises(HTTPException) as caught:
            get_chatgpt_verification_code(
                ChatgptVerificationCodeBody(
                    claim_token="claim-failed", not_before=1784699250000
                ),
                db=self.db,
            )

        self.assertEqual(caught.exception.status_code, 502)
        self.assertEqual(caught.exception.detail["code"], "mail_fetch_failed")

    def test_expired_claim_maps_to_410(self):
        self.add_account("user@outlook.com")
        claim_email(self.db, now=1, token_factory=lambda: "expired")
        with self.assertRaises(HTTPException) as caught:
            get_chatgpt_verification_code(
                ChatgptVerificationCodeBody(claim_token="expired", not_before=1), db=self.db
            )
        self.assertEqual(caught.exception.status_code, 410)
        self.assertEqual(caught.exception.detail["code"], "claim_expired")

    def test_complete_and_release_responses_match_contract(self):
        self.add_account("user@outlook.com")
        claim_email(self.db, token_factory=lambda: "complete")
        self.assertEqual(
            complete_chatgpt_claim(ChatgptClaimTokenBody(claim_token="complete"), db=self.db),
            {"ok": True, "status": "completed"},
        )

        self.add_account("release@outlook.com")
        claim_email(self.db, token_factory=lambda: "release")
        self.assertEqual(
            release_chatgpt_claim(ChatgptClaimTokenBody(claim_token="release"), db=self.db),
            {"ok": True, "released": True},
        )
        self.assertEqual(
            release_chatgpt_claim(ChatgptClaimTokenBody(claim_token="release"), db=self.db),
            {"ok": True, "released": False},
        )

    def test_reconcile_route_quarantines_registration_with_minimal_receipt(self):
        self.assertTrue(
            hasattr(automation_api, "ChatgptReconcileBody")
            and hasattr(automation_api, "reconcile_chatgpt_claim"),
            "authenticated reconciliation API is missing",
        )
        account = self.add_account("user@outlook.com")
        claim_email(self.db, now=1000, token_factory=lambda: "lost-claim")
        self.db.query(ChatgptEmailClaim).delete()
        self.db.commit()

        response = automation_api.reconcile_chatgpt_claim(
            automation_api.ChatgptReconcileBody(
                claim_token="lost-claim",
                email="user@outlook.com",
            ),
            db=self.db,
        )

        self.db.refresh(account)
        self.assertEqual(response, {"ok": True, "status": "reconciled"})
        self.assertIn("已注册chatgpt", account.tags)
