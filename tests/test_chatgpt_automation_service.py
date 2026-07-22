import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from database import Base
from models import ChatgptEmailClaim, MailAccount
import chatgpt_automation_service as automation_service
import icutool_mail
import mail_cache_service
import mail_service
import oauth_service
from oauth_service import OAuthServiceError
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

    def test_reconcile_registration_invalidates_newer_claim_and_is_idempotent(self):
        self.assertTrue(
            hasattr(automation_service, "reconcile_claim_registration"),
            "registration reconciliation service is missing",
        )
        self.add_account("user@example.com", "vip")
        with self.Session() as db:
            claim_email(db, now=1000, token_factory=lambda: "old-token")
            release_claim(db, "old-token")
            claim_email(db, now=1001, token_factory=lambda: "new-token")

            first = automation_service.reconcile_claim_registration(
                db,
                "old-token",
                "user@example.com",
                now=1100,
            )
            second = automation_service.reconcile_claim_registration(
                db,
                "old-token",
                "user@example.com",
                now=1101,
            )
            claims = [
                (claim.claim_token, claim.status)
                for claim in db.query(ChatgptEmailClaim).all()
            ]
            with self.assertRaises(AutomationError) as caught:
                claim_email(db, now=90000, token_factory=lambda: "reused-token")
            account_tags = db.query(MailAccount).filter_by(email="user@example.com").one().tags

        self.assertEqual(first, {"ok": True, "status": "reconciled"})
        self.assertEqual(second, first)
        self.assertEqual(account_tags, "vip,已注册chatgpt")
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0], ("old-token", "completed"))
        self.assertEqual(caught.exception.code, "no_available_email")

    def test_reconcile_rejects_token_bound_to_another_mailbox(self):
        self.assertTrue(
            hasattr(automation_service, "reconcile_claim_registration"),
            "registration reconciliation service is missing",
        )
        self.add_account("first@example.com")
        self.add_account("second@example.com")
        with self.Session() as db:
            claim_email(db, now=1000, token_factory=lambda: "first-token")
            with self.assertRaises(AutomationError) as caught:
                automation_service.reconcile_claim_registration(
                    db,
                    "first-token",
                    "second@example.com",
                    now=1001,
                )
            first = db.query(MailAccount).filter_by(email="first@example.com").one()
            second = db.query(MailAccount).filter_by(email="second@example.com").one()

        self.assertEqual(caught.exception.code, "reconcile_account_mismatch")
        self.assertEqual(first.tags, "")
        self.assertEqual(second.tags, "")

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


class SensitiveLogTest(unittest.TestCase):
    oauth_log_tags = {
        "all_endpoints_failed",
        "fallback_to_msauth",
        "http_error",
        "missing_mail_read",
        "network_retry",
        "provider_error",
        "refresh_attempt",
        "refresh_failed",
        "refresh_succeeded",
        "token_rotated",
    }

    def assert_oauth_log_contract(self, records, account_id):
        self.assertTrue(records, "expected at least one OAuth log record")
        endpoint_pattern = "consumer|common|msauth|token_store|all"
        tag_pattern = "|".join(sorted(self.oauth_log_tags))
        for record in records:
            message = record.getMessage()
            self.assertRegex(
                message,
                rf"^oauth account_id={account_id} endpoint=({endpoint_pattern}) "
                rf"attempt=\d+ tag=({tag_pattern})$",
            )

    def test_oauth_network_retry_log_uses_only_fixed_public_fields(self):
        response = Mock()
        provider_error = oauth_service.requests.exceptions.ConnectionError(
            "private@example.com token=refresh-secret code=919020 password=hunter2"
        )

        with (
            patch.object(oauth_service.requests, "post", side_effect=[provider_error, response]),
            patch.object(oauth_service.time, "sleep"),
            self.assertLogs(oauth_service.logger, level="INFO") as captured,
        ):
            try:
                result = oauth_service._post_with_retry(
                    oauth_service.TOKEN_URL_CONSUMER,
                    data={},
                    timeout=1,
                    retries=1,
                    account_id=17,
                )
            except TypeError as exc:
                self.fail(f"OAuth retry helper must accept an account ID: {exc}")

        self.assertIs(result, response)
        self.assert_oauth_log_contract(captured.records, 17)
        logged = " ".join(record.getMessage() for record in captured.records)
        self.assertNotRegex(
            logged,
            r"private@example\.com|refresh-secret|919020|hunter2",
        )

    def test_standard_oauth_failure_logs_no_account_or_provider_secrets(self):
        account = SimpleNamespace(
            id=18,
            email="standard-private@example.com",
            refresh_token="standard-refresh-secret",
            client_id="standard-client-secret",
            password="shortpass",
            cached_access_token="",
            access_token_expire_time=0,
        )
        response = Mock(ok=False, status_code=400, text="response-body-secret")
        response.json.return_value = {
            "error": "invalid_grant",
            "error_description": (
                "standard-private@example.com token=standard-refresh-secret "
                "code=818181 password=shortpass"
            ),
        }

        with (
            patch.object(oauth_service, "get_session_proxy", return_value=None),
            patch.object(oauth_service, "_post_with_retry", return_value=response),
            self.assertLogs(oauth_service.logger, level="DEBUG") as captured,
            self.assertRaises(OAuthServiceError),
        ):
            oauth_service.get_valid_access_token(account, Mock())

        self.assert_oauth_log_contract(captured.records, 18)
        logged = " ".join(record.getMessage() for record in captured.records)
        self.assertNotRegex(
            logged,
            r"standard-private@example\.com|standard-refresh-secret|standard-client-secret|"
            r"response-body-secret|818181|shortpass|invalid_grant",
        )

    def test_msauth_failure_logs_no_account_or_provider_secrets(self):
        account = SimpleNamespace(
            id=19,
            email="msauth-private@example.com",
            refresh_token="M.C-msauth-refresh-secret",
            client_id="msauth-client-secret",
            password="tiny-pass",
            cached_access_token="",
            access_token_expire_time=0,
        )
        response = Mock(ok=False, status_code=401, text="msauth-response-secret")
        response.json.return_value = {
            "error": "access_denied",
            "error_description": (
                "msauth-private@example.com token=M.C-msauth-refresh-secret "
                "code=717171 password=tiny-pass"
            ),
        }

        with (
            patch.object(oauth_service, "get_session_proxy", return_value=None),
            patch.object(oauth_service, "_post_with_retry", return_value=response),
            self.assertLogs(oauth_service.logger, level="DEBUG") as captured,
            self.assertRaises(OAuthServiceError),
        ):
            oauth_service.get_valid_access_token(account, Mock())

        self.assert_oauth_log_contract(captured.records, 19)
        logged = " ".join(record.getMessage() for record in captured.records)
        self.assertNotRegex(
            logged,
            r"msauth-private@example\.com|M\.C-msauth-refresh-secret|msauth-client-secret|"
            r"msauth-response-secret|717171|tiny-pass|access_denied",
        )

    def test_account_preheat_logs_neither_email_nor_oauth_error(self):
        account = SimpleNamespace(
            id=9,
            email="private@example.com",
            refresh_token="refresh-secret",
        )
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.query.return_value.filter.return_value.first.return_value = account

        with (
            patch.object(icutool_mail, "SessionLocal", return_value=session),
            patch.object(
                icutool_mail,
                "get_valid_access_token",
                side_effect=OAuthServiceError("token=refresh-secret password=hunter2"),
            ),
            patch.object(icutool_mail.logger, "warning") as warning,
        ):
            icutool_mail.preheat_accounts_async([9])
            for _ in range(100):
                if warning.called:
                    break
                threading.Event().wait(0.01)

        logged = repr(warning.call_args_list)
        self.assertIn("oauth_token_failed", logged)
        self.assertNotIn("private@example.com", logged)
        self.assertNotIn("refresh-secret", logged)
        self.assertNotIn("hunter2", logged)

    def test_protocol_fallback_logs_only_account_id_and_stable_error_tag(self):
        account = SimpleNamespace(
            id=42,
            email="private@example.com",
            protocol="auto",
            last_used_protocol="",
            refresh_token="refresh-secret",
            client_id="client-secret",
            password="password-secret",
        )
        db = Mock()
        provider_error = mail_service.MailServiceError(
            "provider rejected password=password-secret token=refresh-secret",
            tag="imap_auth_failed",
        )

        with (
            patch.object(mail_service, "_can_use_protocol", return_value=True),
            patch.object(mail_service, "_load_by_protocol_name", side_effect=provider_error),
            patch.object(mail_service.logger, "info") as info,
            patch.object(mail_service.logger, "warning") as warning,
            self.assertRaises(mail_service.MailServiceError),
        ):
            mail_service._load_with_protocol_selection(account, db)

        logged = repr(info.call_args_list + warning.call_args_list)
        self.assertIn("42", logged)
        self.assertIn("imap_auth_failed", logged)
        self.assertNotIn("private@example.com", logged)
        self.assertNotIn("password-secret", logged)
        self.assertNotIn("refresh-secret", logged)

    def test_background_refresh_logs_neither_email_nor_provider_error(self):
        account = SimpleNamespace(id=7, email="private@example.com")
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.query.return_value.filter.return_value.first.return_value = account
        provider_error = mail_service.MailServiceError(
            "provider returned verification=919020 token=refresh-secret",
            tag="token_invalid",
        )

        with (
            patch("database.SessionLocal", return_value=session),
            patch("mail_service.load_account_mails", side_effect=provider_error),
            patch.object(mail_cache_service.logger, "warning") as warning,
        ):
            task = mail_cache_service.refresh_mail_cache_async(7, "inbox", force=True)
            self.assertTrue(task.event.wait(timeout=5))

        logged = repr(warning.call_args_list)
        self.assertIn("token_invalid", logged)
        self.assertNotIn("private@example.com", logged)
        self.assertNotIn("919020", logged)
        self.assertNotIn("refresh-secret", logged)


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

    def test_accepts_final_parenthesized_address_after_display_name_parentheses(self):
        item = matching_mail()
        item["mail_from"] = "ChatGPT (Transactional) (noreply@tm.openai.com)"
        item["mail_to"] = "Primary account (Registration) (user@outlook.com)"

        result = find_latest_chatgpt_code(
            {"inbox": [item]}, "user@outlook.com", self.not_before_ms
        )

        self.assertEqual(result["code"], "919020")

    def test_rejects_invalid_timestamp_and_accepts_timezone_aware_timestamp(self):
        invalid = matching_mail(received="not-a-timestamp")
        self.assertIsNone(find_latest_chatgpt_code(
            {"inbox": [invalid]}, "user@outlook.com", self.not_before_ms
        ))

        aware = matching_mail(received="2026-07-22T05:48:45+00:00")
        result = find_latest_chatgpt_code(
            {"inbox": [aware]}, "user@outlook.com", self.not_before_ms
        )
        self.assertEqual(result["code"], "919020")
