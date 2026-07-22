import tempfile
import threading
import unittest
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from database import Base
from models import ChatgptEmailClaim, MailAccount
from chatgpt_automation_service import (
    AutomationError,
    append_exact_tag,
    claim_email,
    complete_claim,
    find_latest_chatgpt_code,
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

    def test_release_cannot_delete_a_claim_completed_after_its_initial_read(self):
        self.add_account("user@example.com")
        with self.Session() as db:
            claim_email(db, now=1000, token_factory=lambda: "claim-race")

        completion_errors = []
        delete_observed = []

        def complete_in_parallel():
            try:
                with self.Session() as db:
                    complete_claim(db, "claim-race", now=1001)
            except Exception as exc:
                completion_errors.append(exc)

        def complete_before_delete(_conn, _cursor, statement, _parameters, _context, _executemany):
            if not statement.lstrip().upper().startswith("DELETE FROM CHATGPT_EMAIL_CLAIM"):
                return
            if delete_observed:
                return
            delete_observed.append(True)
            worker = threading.Thread(target=complete_in_parallel)
            worker.start()
            worker.join()

        event.listen(self.engine, "before_cursor_execute", complete_before_delete)
        try:
            with self.Session() as db:
                with self.assertRaises(AutomationError) as caught:
                    release_claim(db, "claim-race")
                remaining = db.query(ChatgptEmailClaim).filter_by(claim_token="claim-race").one()
                account = db.query(MailAccount).filter_by(email="user@example.com").one()
        finally:
            event.remove(self.engine, "before_cursor_execute", complete_before_delete)

        self.assertEqual(completion_errors, [])
        self.assertEqual(delete_observed, [True])
        self.assertEqual(caught.exception.code, "claim_completed")
        self.assertEqual(remaining.status, "completed")
        self.assertEqual(account.tags, "已注册chatgpt")

    def test_completion_serializes_with_release_before_tagging_account(self):
        self.add_account("user@example.com")
        with self.Session() as db:
            claim_email(db, now=1000, token_factory=lambda: "claim-reverse-race")

        release_finished = threading.Event()
        release_errors = []
        release_results = []
        account_query_observed = []

        def release_in_parallel():
            try:
                with self.Session() as db:
                    release_results.append(release_claim(db, "claim-reverse-race"))
            except Exception as exc:
                release_errors.append(exc)
            finally:
                release_finished.set()

        def attempt_release_before_completion_writes(
            _conn, _cursor, statement, _parameters, _context, _executemany
        ):
            normalized = statement.lstrip().upper()
            if "FROM MAIL_ACCOUNT" not in normalized or account_query_observed:
                return
            account_query_observed.append(True)
            worker = threading.Thread(target=release_in_parallel)
            worker.start()
            release_finished.wait(timeout=0.2)

        event.listen(self.engine, "before_cursor_execute", attempt_release_before_completion_writes)
        try:
            with self.Session() as db:
                completed = complete_claim(db, "claim-reverse-race", now=1001)
        finally:
            event.remove(self.engine, "before_cursor_execute", attempt_release_before_completion_writes)

        release_finished.wait(timeout=5)
        with self.Session() as db:
            claim = db.query(ChatgptEmailClaim).filter_by(claim_token="claim-reverse-race").one()
            account = db.query(MailAccount).filter_by(email="user@example.com").one()

        self.assertEqual(account_query_observed, [True])
        self.assertEqual(completed, {"ok": True, "status": "completed"})
        self.assertEqual(release_results, [])
        self.assertEqual(len(release_errors), 1)
        self.assertEqual(release_errors[0].code, "claim_completed")
        self.assertEqual(claim.status, "completed")
        self.assertEqual(account.tags, "已注册chatgpt")

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


def matching_mail(code="919020", received="2026-07-22 13:48:45"):
    return {
        "subject": "Your temporary ChatGPT verification code",
        "mail_from": "ChatGPT (noreply@tm.openai.com)",
        "mail_to": "user@outlook.com (user@outlook.com)",
        "mail_dt": received,
        "body": (
            "<p>Enter this temporary verification code to continue:</p>"
            f"<p>{code}</p>"
            "<a href='https://u20216706.ct.sendgrid.net/123456789'>ChatGPT</a>"
        ),
    }


class VerificationMatcherTest(unittest.TestCase):
    not_before_ms = 1784699250000

    def test_extracts_only_anchored_six_digit_code(self):
        result = find_latest_chatgpt_code(
            {"inbox": [matching_mail()], "junk": []},
            "user@outlook.com",
            self.not_before_ms,
        )
        self.assertEqual(result["code"], "919020")
        self.assertEqual(result["folder"], "inbox")
        self.assertEqual(result["received_at"], "2026-07-22T13:48:45+08:00")

    def test_rejects_wrong_sender_subject_recipient_stale_and_non_six_digit(self):
        variants = []
        for field, value in [
            ("mail_from", "attacker@example.com"),
            ("subject", "Your verification code"),
            ("mail_to", "other@outlook.com"),
            ("mail_dt", "2026-07-22 12:00:00"),
            ("body", "Enter this temporary verification code to continue: 12345"),
        ]:
            item = matching_mail()
            item[field] = value
            variants.append(item)
        for item in variants:
            with self.subTest(item=item):
                self.assertIsNone(find_latest_chatgpt_code(
                    {"inbox": [item]}, "user@outlook.com", self.not_before_ms
                ))

    def test_latest_matching_mail_wins_across_folders(self):
        result = find_latest_chatgpt_code(
            {
                "inbox": [matching_mail(code="111111", received="2026-07-22 13:47:45")],
                "junk": [matching_mail(code="222222", received="2026-07-22 13:48:45")],
            },
            "user@outlook.com",
            self.not_before_ms,
        )
        self.assertEqual(result, {
            "code": "222222",
            "received_at": "2026-07-22T13:48:45+08:00",
            "folder": "junk",
        })

    def test_ignores_six_digit_sendgrid_url_without_fixed_sentence(self):
        item = matching_mail()
        item["body"] = "<a href='https://u20216706.ct.sendgrid.net/123456789'>ChatGPT</a>"
        self.assertIsNone(find_latest_chatgpt_code(
            {"inbox": [item]}, "user@outlook.com", self.not_before_ms
        ))

    def test_uses_parenthesized_actual_recipient_not_email_like_display_name(self):
        item = matching_mail()
        item["mail_to"] = "user@outlook.com (attacker@example.com)"

        self.assertIsNone(find_latest_chatgpt_code(
            {"inbox": [item]}, "user@outlook.com", self.not_before_ms
        ))

    def test_accepts_project_normalized_and_plain_recipient_addresses(self):
        for mail_to in [
            "user@outlook.com (user@outlook.com)",
            "user@outlook.com",
            "User <user@outlook.com>",
        ]:
            with self.subTest(mail_to=mail_to):
                item = matching_mail()
                item["mail_to"] = mail_to
                result = find_latest_chatgpt_code(
                    {"inbox": [item]}, "user@outlook.com", self.not_before_ms
                )
                self.assertEqual(result["code"], "919020")

    def test_uses_actual_sender_for_project_and_rfc_address_forms(self):
        for mail_from, should_match in [
            ("noreply@tm.openai.com (attacker@example.com)", False),
            ("ChatGPT (noreply@tm.openai.com)", True),
            ("noreply@tm.openai.com", True),
            ("ChatGPT <noreply@tm.openai.com>", True),
        ]:
            with self.subTest(mail_from=mail_from):
                item = matching_mail()
                item["mail_from"] = mail_from
                result = find_latest_chatgpt_code(
                    {"inbox": [item]}, "user@outlook.com", self.not_before_ms
                )
                self.assertEqual(result is not None, should_match)
