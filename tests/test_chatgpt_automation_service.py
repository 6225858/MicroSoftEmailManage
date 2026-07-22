import tempfile
import threading
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models import ChatgptEmailClaim, MailAccount
from chatgpt_automation_service import (
    AutomationError,
    append_exact_tag,
    claim_email,
    complete_claim,
    release_claim,
    renew_claim,
    resolve_active_claim,
)


class ClaimServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "test.db"
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False, "timeout": 5},
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()
        self.temp_dir.cleanup()

    def add_account(self, email, tags="", valid_status=1):
        with self.Session() as db:
            account = MailAccount(
                email=email,
                tags=tags,
                valid_status=valid_status,
                created_at=1,
            )
            db.add(account)
            db.commit()

    def test_claim_excludes_exact_registered_tag_and_invalid_accounts(self):
        self.add_account("registered@example.com", "vip,已注册chatgpt")
        self.add_account("similar@example.com", "未已注册chatgpt测试")
        self.add_account("invalid@example.com", valid_status=0)
        with self.Session() as db:
            result = claim_email(db, now=1000, token_factory=lambda: "claim-a")
        self.assertEqual(result["email"], "similar@example.com")
        self.assertEqual(result["claim_token"], "claim-a")
        self.assertEqual(result["expires_at"], 1900)
        self.assertEqual(set(result), {"email", "claim_token", "expires_at"})

    def test_complete_is_idempotent_and_preserves_tags(self):
        self.add_account("user@example.com", "vip,测试")
        with self.Session() as db:
            claim_email(db, now=1000, token_factory=lambda: "claim-b")
            first = complete_claim(db, "claim-b", now=1100)
            second = complete_claim(db, "claim-b", now=1101)
            account = db.query(MailAccount).filter_by(email="user@example.com").one()
        self.assertEqual(first, {"ok": True, "status": "completed"})
        self.assertEqual(second, first)
        self.assertEqual(account.tags, "vip,测试,已注册chatgpt")

    def test_release_is_idempotent_but_cannot_release_completed_claim(self):
        self.add_account("user@example.com")
        with self.Session() as db:
            claim_email(db, now=1000, token_factory=lambda: "claim-c")
            self.assertTrue(release_claim(db, "claim-c"))
            self.assertFalse(release_claim(db, "claim-c"))
            claim_email(db, now=1001, token_factory=lambda: "claim-d")
            complete_claim(db, "claim-d", now=1002)
            with self.assertRaises(AutomationError) as caught:
                release_claim(db, "claim-d")
        self.assertEqual(caught.exception.code, "claim_completed")
        self.assertEqual(caught.exception.status_code, 409)

    def test_expired_fifteen_minute_lease_is_reclaimed(self):
        self.add_account("user@example.com")
        with self.Session() as db:
            claim_email(db, now=1000, token_factory=lambda: "claim-e")
            result = claim_email(db, now=1901, token_factory=lambda: "claim-f")
        self.assertEqual(result["email"], "user@example.com")
        self.assertEqual(result["claim_token"], "claim-f")

    def test_completed_receipt_is_cleaned_up_after_twenty_four_hours(self):
        self.add_account("user@example.com")
        with self.Session() as db:
            claim_email(db, now=1000, token_factory=lambda: "claim-g")
            complete_claim(db, "claim-g", now=1100)
            with self.assertRaises(AutomationError) as caught:
                claim_email(db, now=87501, token_factory=lambda: "claim-h")
            remaining = db.query(ChatgptEmailClaim).count()
        self.assertEqual(caught.exception.code, "no_available_email")
        self.assertEqual(remaining, 0)

    def test_code_found_renewal_extends_active_lease_for_twenty_four_hours(self):
        self.add_account("user@example.com")
        with self.Session() as db:
            claim_email(db, now=1000, token_factory=lambda: "claim-i")
            claim, account = resolve_active_claim(db, "claim-i", now=1010)
            expires_at = renew_claim(db, claim, now=1020, code_found=True)
            db.refresh(claim)
            account_email = account.email
        self.assertEqual(account_email, "user@example.com")
        self.assertEqual(expires_at, 87420)
        self.assertEqual(claim.expires_at, 87420)

    def test_no_available_email_has_stable_error_code(self):
        self.add_account("registered@example.com", "已注册chatgpt")
        with self.Session() as db:
            with self.assertRaises(AutomationError) as caught:
                claim_email(db, now=1000, token_factory=lambda: "claim-j")
        self.assertEqual(caught.exception.code, "no_available_email")
        self.assertEqual(caught.exception.status_code, 409)

    def test_two_threads_claim_two_distinct_accounts(self):
        self.add_account("first@example.com")
        self.add_account("second@example.com")
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def claim_in_thread(index):
            try:
                with self.Session() as db:
                    barrier.wait()
                    results.append(
                        claim_email(
                            db,
                            now=1000,
                            token_factory=lambda: f"claim-thread-{index}",
                        )
                    )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=claim_in_thread, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len({result["email"] for result in results}), 2)


class TagHelpersTest(unittest.TestCase):
    def test_append_exact_tag_normalizes_delimiters_and_deduplicates(self):
        self.assertEqual(
            append_exact_tag(" vip，测试, vip ", "已注册chatgpt"),
            "vip,测试,已注册chatgpt",
        )
