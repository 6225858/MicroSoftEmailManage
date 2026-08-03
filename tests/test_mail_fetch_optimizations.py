"""取件功能优化点的单元测试：

1. Graph 列表去掉 body 字段
2. IMAP 列表批量拉取邮件头（BODY.PEEK[HEADER]），不拉全文
3. IMAP 单封按 Message-ID 精准定位，不再重载整个列表
4. 成功取件后清空全部瞬态错误标签
5. 已知 Graph 401 的账号，自动取件跳过 Graph 首试
"""
import unittest
from unittest import mock

import mail_service


class _FakeAccount:
    def __init__(self, protocol="auto", last_used=None, tags="", email="a@hotmail.com",
                 refresh_token="r", client_id="c", password="", account_id=1):
        self.protocol = protocol
        self.last_used_protocol = last_used
        self.tags = tags
        self.email = email
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.password = password
        self.mail_server = "outlook.office365.com"
        self.mail_port = 993
        self.id = account_id


class _FakeListIMAP:
    def __init__(self):
        self.fetch_args = None

    def select(self, *a, **k):
        return ("OK", [b"1"])

    def response(self, name):
        return ("UIDVALIDITY", [b"123"])

    def uid(self, command, *a):
        if command == "search":
            return ("OK", [b"1 2 3"])
        self.fetch_args = a
        ids = a[0].split(",")
        chunks = []
        for i, uid in enumerate(ids, start=1):
            header = (
                b"From: a@example.com\r\n"
                b"Subject: Subj " + str(i).encode() + b"\r\n"
                b"Message-ID: <m" + str(i).encode() + b"@e.com>\r\n"
                b"Date: Mon, 01 Jan 2024 00:00:00 +0000\r\n"
            )
            chunks.append((f"{i} FETCH (UID {uid} FLAGS (\\Seen) BODY[HEADER] {{0}}".encode(), header))
        return ("OK", chunks)

    def logout(self, *a, **k):
        return ("OK", [b""])


class _FakeSingleIMAP:
    def select(self, *a, **k):
        return ("OK", [b"1"])

    def uid(self, cmd, *args):
        if cmd == "search":
            return ("OK", [b"2"])
        if cmd == "fetch":
            raw = (
                b"From: a@example.com\r\n"
                b"Subject: Single\r\n"
                b"Message-ID: <m2@e.com>\r\n"
                b"Date: Mon, 01 Jan 2024 00:00:00 +0000\r\n"
                b"\r\n"
                b"Hello body"
            )
            return ("OK", [(b"2 FETCH (RFC822 {0})", raw), b")"])
        return ("OK", [])

    def logout(self, *a, **k):
        return ("OK", [b""])


class TestMailFetchOptimizations(unittest.TestCase):
    def setUp(self):
        self._saved = set(mail_service._graph_401_accounts)
        mail_service._graph_401_accounts.clear()

    def tearDown(self):
        mail_service._graph_401_accounts.clear()
        mail_service._graph_401_accounts.update(self._saved)

    def test_list_select_excludes_body(self):
        self.assertNotIn("body", mail_service.LIST_SELECT.split(","))

    def test_imap_list_fetches_headers_only(self):
        fake = _FakeListIMAP()
        acct = _FakeAccount()
        with mock.patch.object(mail_service, "get_imap_conn", return_value=fake), \
             mock.patch.object(mail_service, "_select_imap_folder", return_value="inbox"):
            items = mail_service.load_imap_messages(acct, None, folder="inbox", limit=20)

        # 仅一次批量 fetch，且只拉邮件头，不拉 RFC822
        self.assertIsNotNone(fake.fetch_args)
        self.assertEqual(fake.fetch_args[1], "(BODY.PEEK[HEADER] FLAGS)")
        self.assertEqual(len(items), 3)
        for item in items:
            # 列表不返回正文，减小体积
            self.assertEqual(item["body"], "")
            self.assertTrue(item["id"].startswith("imap:123:"))
            self.assertTrue(item["is_read"])

    def test_imap_single_fetch_does_not_reload_list(self):
        fake = _FakeSingleIMAP()
        acct = _FakeAccount(protocol="imap", account_id=7)
        with mock.patch.object(mail_service, "get_imap_conn", return_value=fake), \
             mock.patch.object(mail_service, "_select_imap_folder", return_value="inbox"), \
             mock.patch.object(mail_service, "load_imap_messages") as reload_mock:
            res = mail_service.load_single_mail_with_protocol(
                acct, None, "<m2@e.com>", "inbox"
            )

        self.assertIsNotNone(res)
        self.assertEqual(res["id"], "<m2@e.com>")
        self.assertIn("Hello body", res["body"])
        # 不应为取一封邮件而重新拉取整个列表
        reload_mock.assert_not_called()

    def test_transient_error_tags_cleared_on_success(self):
        # 标签清理只在 auto 分支、且成功协议 != last_used 时触发
        acct = _FakeAccount(
            protocol="auto", last_used="imap",
            tags="token_invalid,imap_auth_failed,keep-tag", account_id=3,
        )
        db = mock.MagicMock()
        imap_err = mail_service.MailServiceError("imap fail", tag="imap_auth_failed")
        with mock.patch.object(mail_service, "load_imap_messages", side_effect=imap_err), \
             mock.patch.object(mail_service, "load_mail_messages", return_value=[{"id": "x"}]) as g, \
             mock.patch.object(mail_service, "load_pop3_messages"):
            items = mail_service._load_with_protocol_selection(acct, db, folder="inbox", limit=20)

        self.assertEqual(items, [{"id": "x"}])
        # 瞬态错误标签全部清空，保留业务标签
        self.assertEqual(acct.tags, "keep-tag")
        self.assertEqual(acct.last_used_protocol, "graph")
        g.assert_called_once()
        db.commit.assert_called()

    def test_graph_401_skips_first_attempt(self):
        mail_service._graph_401_accounts.add(1)
        acct = _FakeAccount(protocol="auto", last_used=None, account_id=1)
        db = mock.MagicMock()
        with mock.patch.object(mail_service, "load_mail_messages") as g, \
             mock.patch.object(mail_service, "load_imap_messages", return_value=[{"id": "x"}]) as im, \
             mock.patch.object(mail_service, "load_pop3_messages") as po:
            items = mail_service._load_with_protocol_selection(acct, db, folder="inbox", limit=20)

        self.assertEqual(items, [{"id": "x"}])
        # IMAP 首试成功，Graph 不应被调用
        im.assert_called_once()
        g.assert_not_called()
        po.assert_not_called()

    def test_merge_incremental_reuses_cached_bodies(self):
        # 增量刷新：旧邮件复用正文，新邮件保持元数据并按需加载正文
        acct = _FakeAccount(protocol="imap", account_id=9)
        existing = [
            {"id": "<old@e.com>", "subject": "Old", "body": "cached body",
             "html": "<p>cached</p>"},
        ]
        refs = [
            {"id": "<new@e.com>", "subject": "New"},
            {"id": "<old@e.com>", "subject": "Old"},
        ]
        with mock.patch.object(mail_service, "load_account_mails", return_value=refs), \
             mock.patch.object(
                 mail_service, "_load_single_imap_mail",
                 return_value={"id": "<new@e.com>", "subject": "New", "body": "new body"},
             ) as fm:
            merged, new_count = mail_service.merge_incremental_mails(
                acct, None, "inbox", 20, existing
            )

        self.assertEqual(new_count, 1)
        self.assertEqual(len(merged), 2)
        fm.assert_not_called()
        # 顺序保持服务端最新在前
        self.assertEqual(merged[0]["id"], "<new@e.com>")
        self.assertNotIn("body", merged[0])
        # 旧邮件复用缓存中的正文，不再重新下载
        self.assertEqual(merged[1]["id"], "<old@e.com>")
        self.assertEqual(merged[1]["body"], "cached body")

    def test_merge_incremental_graph_fetches_new_only(self):
        acct = _FakeAccount(protocol="graph", account_id=10)
        existing = [{"id": "g-old", "subject": "Old", "body": "cached"}]
        refs = [{"id": "g-new", "subject": "New"}, {"id": "g-old", "subject": "Old"}]
        with mock.patch.object(mail_service, "load_account_mails", return_value=refs), \
             mock.patch.object(
                 mail_service, "load_single_mail",
                 return_value={"id": "g-new", "subject": "New", "body": "fresh"},
             ) as fm:
            merged, new_count = mail_service.merge_incremental_mails(
                acct, None, "inbox", 20, existing
            )
        self.assertEqual(new_count, 1)
        fm.assert_not_called()
        self.assertNotIn("body", merged[0])
        self.assertEqual(merged[1]["body"], "cached")

    def test_merge_incremental_pop3_reuses_ref_body(self):
        # POP3 列表本身含正文，fetch_mail_full 直接复用 ref，无额外请求
        acct = _FakeAccount(protocol="pop3", account_id=11)
        existing = []
        refs = [{"id": "<p1@e.com>", "subject": "M1", "body": "pop body"}]
        with mock.patch.object(mail_service, "load_account_mails", return_value=refs), \
             mock.patch.object(mail_service, "_load_single_imap_mail") as im, \
             mock.patch.object(mail_service, "load_single_mail") as gm:
            merged, new_count = mail_service.merge_incremental_mails(
                acct, None, "inbox", 20, existing
            )
        self.assertEqual(new_count, 1)
        self.assertEqual(merged[0]["body"], "pop body")
        im.assert_not_called()
        gm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
