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

    def test_auto_no_last_used_discovers_protocol_and_fetches_body(self):
        # P0-A 修复：auto 模式 + 空 last_used 时，必须先跑自动协议选择确定协议，
        # 再按该协议真正补取单封正文，而不能直接把无正文的「列表项」当详情返回。
        acct = _FakeAccount("auto", last_used=None)
        with mock.patch.object(mail_service, "load_account_mails") as au, \
             mock.patch.object(mail_service, "_resolve_effective_protocol") as rp, \
             mock.patch.object(mail_service, "_load_single_by_protocol") as lsb, \
             mock.patch.object(mail_service, "load_mail_messages"), \
             mock.patch.object(mail_service, "load_imap_messages"), \
             mock.patch.object(mail_service, "load_pop3_messages"):
            # 列表取件只返回元数据（graph/imap 列表本就不含正文）
            au.return_value = [{"id": "m1", "subject": "列表项无正文"}]
            rp.return_value = "imap"  # 自动选择探测出 imap 可用
            # 按探测协议补取的单封带回正文
            lsb.return_value = {"id": "m1", "subject": "完整邮件", "body": "<p>你好</p>"}
            res = mail_service.load_single_mail_with_protocol(acct, None, "m1", "inbox")
            self.assertIsNotNone(res)
            # 自动协议选择必须发生
            au.assert_called_once()
            # 关键：必须按探测出的协议补取一次单封正文
            lsb.assert_called_once_with("imap", acct, None, "m1", "inbox")
            # 返回的是带正文的结果，而非无正文的列表项
            self.assertEqual(res.get("body"), "<p>你好</p>")
            # 列表取件函数本身不应被直接用于取单封
            mail_service.load_mail_messages.assert_not_called()
            mail_service.load_imap_messages.assert_not_called()
            mail_service.load_pop3_messages.assert_not_called()

    def test_auto_no_last_used_no_body_falls_back_to_list_meta(self):
        # 探测协议后仍然取不到正文（真实服务器就是没有正文）时，
        # 至少要把列表项的元数据返回，而不是返回 None。
        acct = _FakeAccount("auto", last_used=None)
        with mock.patch.object(mail_service, "load_account_mails") as au, \
             mock.patch.object(mail_service, "_resolve_effective_protocol") as rp, \
             mock.patch.object(mail_service, "_load_single_by_protocol") as lsb, \
             mock.patch.object(mail_service, "load_mail_messages"), \
             mock.patch.object(mail_service, "load_imap_messages"), \
             mock.patch.object(mail_service, "load_pop3_messages"):
            au.return_value = [{"id": "m1", "subject": "列表项无正文"}]
            rp.return_value = "imap"
            lsb.return_value = None  # 单封补取也失败
            res = mail_service.load_single_mail_with_protocol(acct, None, "m1", "inbox")
            self.assertIsNotNone(res)
            self.assertEqual(res.get("id"), "m1")


if __name__ == "__main__":
    unittest.main()
