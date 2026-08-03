from types import SimpleNamespace
from unittest import mock

import icutool_mail
import mail_cache_service
import mail_service


def _account(**overrides):
    values = {
        "id": 1,
        "email": "a@hotmail.com",
        "protocol": "graph",
        "last_used_protocol": "graph",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _db_returning(account):
    db = mock.MagicMock()
    db.query.return_value.filter.return_value.first.return_value = account
    return db


def test_fresh_empty_cache_does_not_trigger_network_refresh():
    account = _account()
    cached = {"items": [], "updated_at": 100, "is_fresh": True}
    with mock.patch.object(icutool_mail, "get_mail_cache", return_value=cached), \
         mock.patch.object(icutool_mail, "refresh_mail_cache_async") as refresh:
        result = icutool_mail.get_account_mails(1, folder="inbox", db=_db_returning(account))

    assert result["cached"] is True
    assert result["items"] == []
    refresh.assert_not_called()


def test_mail_detail_distinguishes_confirmed_empty_from_missing():
    account = _account()
    db = _db_returning(account)
    with mock.patch.object(icutool_mail, "get_mail_cache", return_value=None), \
         mock.patch.object(icutool_mail, "load_single_mail_with_protocol") as load:
        load.return_value = {"id": "m1", "body": "<p>No content</p>"}
        empty = icutool_mail.get_account_mail_detail(1, "m1", db=db)
        load.return_value = {"id": "m2", "body": ""}
        missing = icutool_mail.get_account_mail_detail(1, "m2", db=db)

    assert empty["body_status"] == "empty"
    assert missing["body_status"] == "missing"


def test_uid_upgrade_preserves_body_from_legacy_message_id_cache():
    old = [{"id": "<legacy@example.com>", "body": "cached body"}]
    new = [{
        "id": "imap:123:42",
        "message_id": "<legacy@example.com>",
        "subject": "Current metadata",
        "body": "",
    }]
    merged = mail_cache_service.merge_preserve_bodies(new, old)
    assert merged[0]["id"] == "imap:123:42"
    assert merged[0]["body"] == "cached body"


def test_graph_list_maps_read_status():
    payload = {"value": [{
        "id": "g1",
        "subject": "Read mail",
        "receivedDateTime": "2026-01-01T00:00:00Z",
        "isRead": True,
    }]}
    with mock.patch.object(mail_service, "_graph_request", return_value=payload):
        items = mail_service.load_mail_messages(_account(), None)
    assert items[0]["is_read"] is True


def test_pop3_unseen_filter_is_rejected_as_inaccurate():
    account = _account(protocol="pop3", last_used_protocol="pop3")
    body = SimpleNamespace()
    try:
        icutool_mail._fetch_mails_by_email(
            _db_returning(account), account.email, "inbox", 20, False, 1,
            unseen_only=True,
        )
    except icutool_mail.HTTPException as exc:
        assert exc.status_code == 400
        assert exc.detail["code"] == "unseen_unsupported"
    else:
        raise AssertionError("POP3 unseen_only should not claim an accurate result")


def test_pop3_uidl_keeps_messages_unique_without_message_id():
    class FakePOP:
        def user(self, _value):
            return b"+OK"

        def pass_(self, _value):
            return b"+OK"

        def stat(self):
            return 2, 100

        def uidl(self):
            return b"+OK", [b"1 stable-a", b"2 stable-b"], 20

        def retr(self, index):
            raw = (
                f"From: sender{index}@example.com\r\n"
                f"Subject: Message {index}\r\n"
                "Date: Mon, 01 Jan 2024 00:00:00 +0000\r\n"
                "\r\nBody"
            ).encode()
            return b"+OK", raw.split(b"\r\n"), len(raw)

        def quit(self):
            return b"+OK"

    account = SimpleNamespace(
        id=2,
        email="pop@example.com",
        password="password",
        refresh_token="",
        client_id="",
        mail_server="pop.example.com",
        mail_port=995,
        mail_use_ssl=1,
    )
    with mock.patch.object(mail_service.poplib, "POP3_SSL", return_value=FakePOP()), \
         mock.patch.object(mail_service, "get_proxied_socket_factory", return_value=None):
        items = mail_service.load_pop3_messages(account, mock.MagicMock(), limit=2)

    assert [item["id"] for item in items] == ["pop3:stable-b", "pop3:stable-a"]
    assert all(item["message_id"] == "" for item in items)
