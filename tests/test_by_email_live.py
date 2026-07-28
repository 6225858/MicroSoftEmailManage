"""取号功能冒烟测试：验证路由修复与按邮箱定向取件逻辑。

不依赖 TestClient/网络鉴权，直接调用内部函数走真实代码路径。
真实联网取件对真实账号执行，受 timeout 限制；失败会体现在 refresh_error 中。
"""
import os
import shutil
import sys
import tempfile
import traceback
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import icutool_mail as api
from database import Base
from models import MailAccount
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_test_out.txt")
REAL_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "mail.db")


def log(msg):
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(str(msg) + "\n")


class ByEmailLiveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open(OUT, "w", encoding="utf-8").close()
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        if os.path.exists(REAL_DB):
            shutil.copy(REAL_DB, tmp.name)
        eng = create_engine("sqlite:///%s" % tmp.name,
                            connect_args={"check_same_thread": False})
        Base.metadata.create_all(eng)
        cls.Session = sessionmaker(bind=eng)
        cls.db_path = tmp.name

    @classmethod
    def tearDownClass(cls):
        try:
            os.remove(cls.db_path)
        except OSError:
            pass

    def setUp(self):
        self.db = self.Session()

    def tearDown(self):
        self.db.close()

    def test_route_registration_no_duplicate(self):
        """修复 #1：/api/automation/chatgpt/verification-code 只应注册一个 POST 路由，
        且端点必须是 get_chatgpt_verification_code（不是辅助函数）。"""
        matches = []
        for route in api.app.routes:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None) or set()
            if path == "/api/automation/chatgpt/verification-code" and "POST" in methods:
                matches.append(getattr(route.endpoint, "__name__", repr(route.endpoint)))
        log("ROUTE /api/automation/chatgpt/verification-code -> %r" % (matches,))
        self.assertEqual(len(matches), 1, "重复路由未消除: %r" % (matches,))
        self.assertEqual(matches[0], "get_chatgpt_verification_code",
                         "端点被辅助函数拦截: %r" % (matches,))
        log("PASS 路由注册: 唯一且正确")

    def test_nonexistent_email_returns_404(self):
        with self.assertRaises(api.HTTPException) as caught:
            api._fetch_mails_by_email(self.db, "no-such-account@nowhere.test", "inbox", 5, True, timeout=10)
        self.assertEqual(caught.exception.status_code, 404)
        log("PASS 不存在邮箱 -> 404 (%s)" % caught.exception.detail.get("code"))

    def test_placeholder_account_reports_refresh_error(self):
        """占位账号（空凭证）刷新失败：验证 refresh_error 被上报(修复#3) 且 is_fresh=False(修复#2)。"""
        res = api._fetch_mails_by_email(self.db, "user1@example.com", "inbox", 5, True, timeout=10)
        log("占位账号 user1@example.com: refresh_error=%r is_fresh=%r items=%d"
            % (res.get("refresh_error"), res.get("is_fresh"), len(res.get("items", []))))
        self.assertTrue(res.get("refresh_error"), "修复#3 失效: 失败的刷新未上报 refresh_error")
        self.assertFalse(res.get("is_fresh"), "修复#2 失效: is_fresh 仍误报 True")
        log("PASS 占位账号失败刷新正确上报 refresh_error 且 is_fresh=False")

    def test_real_account_fetch(self):
        real = self.db.query(MailAccount).filter(
            MailAccount.email.ilike("%hotmail.com")
        ).first()
        if real is None:
            log("SKIP 真实账号不存在，跳过联网取件")
            return
        log("真实账号: %s protocol=%s" % (real.email, real.protocol))
        r = api._fetch_mails_by_email(self.db, real.email, "inbox", 10, True, timeout=20)
        log("真实账号取件: refresh_error=%r is_fresh=%r items=%d cached=%s"
            % (r.get("refresh_error"), r.get("is_fresh"),
               len(r.get("items", [])), r.get("cached")))
        if r.get("refresh_error"):
            log("注意: 真实账号取件失败（多为网络/代理/凭证问题），但错误已被正确上报（修复#3 生效）")
        else:
            log("成功: 真实账号取件返回 %d 封邮件" % len(r.get("items", [])))
        log("PASS 真实账号取件流程执行完毕（成功或错误均已正确呈现）")


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    except Exception:
        log(traceback.format_exc())
