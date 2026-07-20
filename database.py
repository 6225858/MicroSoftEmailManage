import os
import logging
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


# 解析并创建数据目录
DATA_DIR = _resolve_data_dir()

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
