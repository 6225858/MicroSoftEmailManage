"""验证旧版本数据库(./mail.db)升级时自动迁移到新目录，避免邮箱数据清空。"""
import os
import sqlite3
import tempfile
import shutil

import database


def _make_legacy(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE mail_account(id INTEGER PRIMARY KEY, email TEXT)")
    conn.execute("INSERT INTO mail_account(email) VALUES ('keep@hotmail.com')")
    conn.commit()
    conn.close()


def test_legacy_db_migrated_with_data_preserved():
    tmp = tempfile.mkdtemp()
    try:
        legacy_dir = os.path.join(tmp, "legacy")
        new_dir = os.path.join(tmp, "data")
        os.makedirs(legacy_dir)
        os.makedirs(new_dir)

        legacy_db = os.path.join(legacy_dir, "mail.db")
        _make_legacy(legacy_db)
        target_db = os.path.join(new_dir, "mail.db")

        # 注入 legacy_dir 作为“项目根”，cwd 也指向它，完全隔离真实文件
        prev = os.getcwd()
        os.chdir(legacy_dir)
        try:
            database._migrate_legacy_db(new_dir, project_root=legacy_dir)
        finally:
            os.chdir(prev)

        assert os.path.exists(target_db), "目标库未生成"
        assert not os.path.exists(legacy_db), "旧库未被移动"

        conn = sqlite3.connect(target_db)
        rows = conn.execute("SELECT email FROM mail_account").fetchall()
        conn.close()
        assert ("keep@hotmail.com",) in rows, f"数据未保留: {rows}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_legacy_no_action():
    tmp = tempfile.mkdtemp()
    try:
        new_dir = os.path.join(tmp, "data")
        os.makedirs(new_dir)
        database._migrate_legacy_db(new_dir, project_root=tmp)
        assert not os.path.exists(os.path.join(new_dir, "mail.db"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_legacy_db_migrated_with_data_preserved()
    test_no_legacy_no_action()
    print("MIGRATION TESTS OK")
