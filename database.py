import os
import logging
import shutil
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker


logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


def _resolve_data_dir() -> str:
    """
    解析数据库存储目录：
    1. 优先使用环境变量 DATA_DIR（Docker 部署用）
    2. 否则使用项目根目录下的 data/ 子目录
    3. 自动创建目录，避免数据库文件无法写入
    """
    env_data_dir = os.getenv("DATA_DIR", "").strip()
    if env_data_dir:
        data_dir = env_data_dir
    else:
        # 默认使用项目根目录下的 data/ 子目录
        # __file__ 是 database.py 的路径，data/ 与它同级
        project_root = Path(__file__).resolve().parent
        data_dir = str(project_root / "data")

    # 确保目录存在（即使挂载点不存在也会自动创建）
    try:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("无法创建数据目录 %s: %s，将回退到当前工作目录", data_dir, exc)
        data_dir = "."

    return data_dir


def _migrate_legacy_db(data_dir: str, project_root: str | None = None) -> None:
    """把旧版本数据库迁移到新的存储目录，避免升级后邮箱数据“被清空”。

    旧版本（早期 release）数据库路径为 ./mail.db，即写在项目根目录或
    容器工作目录（Docker 中即 /app/mail.db）；
    新版本统一使用 <DATA_DIR>/mail.db（本地为 data/mail.db，Docker 为 /app/data/mail.db）。

    升级逻辑：
    - 新位置已存在库 → 不迁移（保留新库，避免覆盖）。
    - 新位置不存在库，但旧位置（项目根目录 / 当前工作目录）存在 mail.db →
      连同 WAL 伴随文件(-wal/-shm)一起移动到新位置。
    这样无论是本地升级还是 Docker 重新部署旧镜像，已有邮箱账号都不会丢失。
    """
    target_db = os.path.join(data_dir, "mail.db")
    if os.path.exists(target_db):
        return  # 新位置已有库，无需迁移

    if project_root is None:
        project_root = str(Path(__file__).resolve().parent)
    # 旧版可能的两个位置：项目根目录、进程当前工作目录（如 Docker 的 /app）
    candidates: list[str] = []
    for base in (project_root, os.getcwd()):
        if base and base not in candidates:
            candidates.append(base)

    for base in candidates:
        legacy_db = os.path.join(base, "mail.db")
        if legacy_db == target_db or not os.path.exists(legacy_db):
            continue
        try:
            os.makedirs(data_dir, exist_ok=True)
            # 同时迁移 WAL 伴随文件，确保数据完整（避免只移动主文件导致数据缺失）
            for suffix in ("", "-wal", "-shm"):
                src = legacy_db + suffix
                if os.path.exists(src):
                    shutil.move(src, target_db + suffix)
            logger.info("检测到旧版本数据库 %s，已自动迁移到 %s", legacy_db, target_db)
            return
        except OSError as exc:
            logger.warning("迁移旧版本数据库失败 %s -> %s: %s", legacy_db, target_db, exc)


# 解析并创建数据目录
DATA_DIR = _resolve_data_dir()

# 升级兼容：旧版本数据库写在项目根/工作目录的 mail.db，
# 新版本统一在 <DATA_DIR>/mail.db。若新位置无库但旧位置有，自动迁移，避免邮箱数据被清空。
_migrate_legacy_db(DATA_DIR)

# SQLite 数据库文件路径
DATABASE_PATH = os.path.join(DATA_DIR, "mail.db")
DATABASE_URL = f"sqlite:///{DATABASE_PATH}"

logger.info("数据库存储目录: %s", DATA_DIR)
logger.info("数据库文件路径: %s", DATABASE_PATH)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
    pool_pre_ping=True,
)


# 启用 WAL 模式：读写不互斥，解决后台刷新任务持读锁时删除操作被阻塞的问题
# 非 WAL 模式下：SELECT 持共享锁 → DELETE 的排他锁被阻塞 → 必须等后台任务(15-30s)完成
# WAL 模式下：读不阻塞写，写不阻塞读，只有写阻塞写
@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _conn_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")  # 30 秒等待锁
    cursor.execute("PRAGMA synchronous=NORMAL")  # WAL 模式下 NORMAL 足够安全且更快
    cursor.close()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def _run_schema_migrations() -> None:
    """在 engine 创建后、任何连接被使用前执行幂等 schema 迁移。

    SQLAlchemy 的 create_all 只会创建新表，不会给已存在的表添加新列。
    已有部署的 data/mail.db 需要通过 ALTER TABLE 补齐新增列，否则升级后会报
    'no such column'。放在 database 模块加载时执行，可保证所有连接建立前表结构已最新
    （避免测试/运行时先打开连接缓存旧 schema，再迁移导致列不可见）。
    """
    migrations = [
        ("mail_account", "cached_access_token", "TEXT DEFAULT ''"),
        ("mail_account", "access_token_expire_time", "INTEGER DEFAULT 0"),
        ("mail_account", "cached_access_token_graph", "TEXT DEFAULT ''"),
        ("mail_account", "cached_access_token_imap", "TEXT DEFAULT ''"),
        ("mail_account", "cached_access_token_graph_expire_time", "INTEGER DEFAULT 0"),
        ("mail_account", "cached_access_token_imap_expire_time", "INTEGER DEFAULT 0"),
        ("mail_account", "oauth_mode", "TEXT NOT NULL DEFAULT ''"),
    ]
    try:
        with engine.begin() as conn:
            from sqlalchemy import text

            for table, col, ddl in migrations:
                exists = conn.execute(
                    text(
                        f"SELECT 1 FROM pragma_table_info('{table}') WHERE name='{col}'"
                    )
                ).first()
                if not exists:
                    conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                    )
                    logger.info("schema 迁移: 为 %s 添加列 %s", table, col)
            conn.execute(
                text(
                    "UPDATE mail_account SET "
                    "cached_access_token_graph_expire_time = access_token_expire_time "
                    "WHERE cached_access_token_graph_expire_time = 0 "
                    "AND cached_access_token_graph != ''"
                )
            )
            conn.execute(
                text(
                    "UPDATE mail_account SET "
                    "cached_access_token_imap_expire_time = access_token_expire_time "
                    "WHERE cached_access_token_imap_expire_time = 0 "
                    "AND cached_access_token_imap != ''"
                )
            )
    except Exception as exc:  # 迁移失败不应阻断启动，下次启动重试
        logger.warning("schema 迁移执行失败（可忽略）: %s", exc)


# 在创建 engine 后立刻执行迁移，确保所有连接使用最新表结构
_run_schema_migrations()
