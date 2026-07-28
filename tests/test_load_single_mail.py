"""验证 load_single_mail_with_protocol 的协议分发（修复 auto 模式错误走 Graph）。"""
import unittest
from unittest import mock

import mail_service


class _FakeAccount:
    def __init__(self, protocol, last_used=None):
        self.protocol = protocol
        self.last_used_protocol = last_used
        self.email = "a@hotmail.com"
        self.refresh_token = "r"
        self.client_id = "c"
        self.mail_server = "outlook.office365.com"
        self.mail_port = 993


class TestLoadSingleMailWithProtocol(unittest.TestCase):
    def test_auto_last_used_imap_uses_imap_not_graph(self):
        acct = _FakeAccount("auto", last_used="imap")
        with mock.patch.object(mail_service, "load_mail_messages"), \
             mock.patch.object(mail_service, "load_imap_messages") as im, \
             mock.patch.object(mail_service, "load_pop3_messages"), \
             mock.patch.object(mail_service, "load_account_mails"), \
             mock.patch.object(mail_service, "_load_single_imap_mail") as sim:
            sim.return_value = {"id": "m1"}
            res = mail_service.load_single_mail_with_protocol(acct, None, "m1", "inbox")
            self.assertIsNotNone(res)
            # IMAP 单封取件应直接按 Message-ID 定位，而非重新拉取整个列表
            mail_service._load_single_imap_mail.assert_called_once()
            mail_service.load_imap_messages.assert_not_called()
            mail_service.load_mail_messages.assert_not_called()
            mail_service.load_pop3_messages.assert_not_called()
            mail_service.load_account_mails.assert_not_called()

    def test_auto_last_used_pop3_uses_pop3(self):
        acct = _FakeAccount("auto", last_used="pop3")
        with mock.patch.object(mail_service, "load_mail_messages"), \
             mock.patch.object(mail_service, "load_imap_messages"), \
             mock.patch.object(mail_service, "load_pop3_messages") as po, \
             mock.patch.object(mail_service, "load_account_mails"):
            po.return_value = [{"id": "m1"}]
            res = mail_service.load_single_mail_with_protocol(acct, None, "m1", "inbox")
            self.assertIsNotNone(res)
            mail_service.load_pop3_messages.assert_called_once()
            mail_service.load_mail_messages.assert_not_called()
            mail_service.load_imap_messages.assert_not_called()
            mail_service.load_account_mails.assert_not_called()

    def test_explicit_graph_uses_graph(self):
        acct = _FakeAccount("graph", last_used="imap")
        with mock.patch.object(mail_service, "load_single_mail") as g, \
             mock.patch.object(mail_service, "load_imap_messages"), \
             mock.patch.object(mail_service, "load_pop3_messages"), \
             mock.patch.object(mail_service, "load_account_mails"):
            g.return_value = {"id": "m1"}
            res = mail_service.load_single_mail_with_protocol(acct, None, "m1", "inbox")
            self.assertIsNotNone(res)
            mail_service.load_single_mail.assert_called_once()
            mail_service.load_imap_messages.assert_not_called()
            mail_service.load_pop3_messages.assert_not_called()
            mail_service.load_account_mails.assert_not_called()

    def test_auto_no_last_used_falls_to_auto_select(self):
        acct = _FakeAccount("auto", last_used=None)
        with mock.patch.object(mail_service, "load_mail_messages"), \
             mock.patch.object(mail_service, "load_imap_messages"), \
             mock.patch.object(mail_service, "load_pop3_messages"), \
             mock.patch.object(mail_service, "load_account_mails") as au:
            au.return_value = [{"id": "m1"}]
            res = mail_service.load_single_mail_with_protocol(acct, None, "m1", "inbox")
            self.assertIsNotNone(res)
            mail_service.load_account_mails.assert_called_once()
            mail_service.load_mail_messages.assert_not_called()
            mail_service.load_imap_messages.assert_not_called()
            mail_service.load_pop3_messages.assert_not_called()


if __name__ == "__main__":
    unittest.main()
