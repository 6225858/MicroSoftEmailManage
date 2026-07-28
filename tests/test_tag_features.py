"""标签功能增强测试：批量标签操作 + 标签统计概览。

直接调用视图函数（batch_update_tags / tag_stats）走真实代码路径，
避免依赖 TestClient 与鉴权，测试聚焦业务逻辑。
"""
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import icutool_mail
from database import Base
from icutool_mail import BatchTagsBody, batch_update_tags, tag_stats
from models import MailAccount


class TagFeatureTest(unittest.TestCase):
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

    def add_account(self, email, tags=""):
        account = MailAccount(email=email, created_at=1, valid_status=1, tags=tags)
        self.db.add(account)
        self.db.commit()
        return account

    def test_batch_tags_add_dedup_keeps_order(self):
        a = self.add_account("a@x.com", "old")
        b = self.add_account("b@x.com", "")
        res = batch_update_tags(BatchTagsBody(ids=[a.id, b.id], tags="new, old", mode="add"), self.db)
        self.assertEqual(res["updated"], 2)
        self.assertEqual(res["not_found"], 0)
        self.assertEqual(res["tags"][a.id], "old, new")  # 保留原顺序并去重
        self.assertEqual(res["tags"][b.id], "new, old")  # 空账号添加两个标签

    def test_batch_tags_remove(self):
        a = self.add_account("a@x.com", "x, y, z")
        res = batch_update_tags(BatchTagsBody(ids=[a.id], tags="y", mode="remove"), self.db)
        self.assertEqual(res["tags"][a.id], "x, z")

    def test_batch_tags_set_overwrites(self):
        a = self.add_account("a@x.com", "old1, old2")
        res = batch_update_tags(BatchTagsBody(ids=[a.id], tags="only", mode="set"), self.db)
        self.assertEqual(res["tags"][a.id], "only")

    def test_batch_tags_not_found_counted(self):
        self.add_account("a@x.com", "")
        res = batch_update_tags(BatchTagsBody(ids=[999, 1000], tags="t", mode="add"), self.db)
        self.assertEqual(res["updated"], 0)
        self.assertEqual(res["not_found"], 2)

    def test_tag_stats_counts(self):
        self.add_account("a@x.com", "red, blue")
        self.add_account("b@x.com", "blue")
        self.add_account("c@x.com", "")
        stats = tag_stats(self.db)
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["no_tag_count"], 1)
        tag_map = {item["tag"]: item["count"] for item in stats["tags"]}
        self.assertEqual(tag_map["blue"], 2)
        self.assertEqual(tag_map["red"], 1)
        # 按数量降序、标签名升序排列
        self.assertEqual(stats["tags"][0]["tag"], "blue")


if __name__ == "__main__":
    unittest.main()
