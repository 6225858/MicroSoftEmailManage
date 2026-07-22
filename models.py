from sqlalchemy import Column, Integer, Text

from database import Base


class MailAccount(Base):
    __tablename__ = "mail_account"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(Text, unique=True, index=True, nullable=False)
    password = Column(Text, default="", nullable=False)
    client_id = Column(Text, default="", nullable=False)
    refresh_token = Column(Text, default="", nullable=False)
    cached_access_token = Column(Text, default="", nullable=False)
    access_token_expire_time = Column(Integer, default=0, nullable=False)
    tags = Column(Text, default="", nullable=False)
    remark = Column(Text, default="", nullable=False)
    valid_status = Column(Integer, default=1, nullable=False)
    # 取件协议：auto（自动选择）/ graph / imap / pop3，默认 auto
    protocol = Column(Text, default="auto", nullable=False)
    # 上次成功使用的协议（自动选择模式下的优化提示，避免每次都从头尝试）
    last_used_protocol = Column(Text, default="", nullable=False)
    # IMAP/POP3 服务器地址，留空则使用微软默认值
    mail_server = Column(Text, default="", nullable=False)
    # IMAP/POP3 服务器端口，0 表示按协议默认值
    mail_port = Column(Integer, default=0, nullable=False)
    # 是否启用 SSL，1=启用 0=不启用，默认 1
    mail_use_ssl = Column(Integer, default=1, nullable=False)
    created_at = Column(Integer, nullable=False)


class ChatgptEmailClaim(Base):
    __tablename__ = "chatgpt_email_claim"

    id = Column(Integer, primary_key=True, index=True)
    mail_account_id = Column(Integer, unique=True, index=True, nullable=False)
    claim_token = Column(Text, unique=True, index=True, nullable=False)
    status = Column(Text, default="active", nullable=False)
    claimed_at = Column(Integer, nullable=False)
    expires_at = Column(Integer, index=True, nullable=False)
    completed_at = Column(Integer, default=0, nullable=False)


class TokenRefreshLog(Base):
    __tablename__ = "token_refresh_log"

    id = Column(Integer, primary_key=True, index=True)
    trigger_type = Column(Text, default="manual", nullable=False)
    total_count = Column(Integer, default=0, nullable=False)
    success_count = Column(Integer, default=0, nullable=False)
    failed_count = Column(Integer, default=0, nullable=False)
    failure_details = Column(Text, default="[]", nullable=False)
    html_content = Column(Text, default="", nullable=False)
    started_at = Column(Integer, nullable=False)
    finished_at = Column(Integer, nullable=False)
    duration_seconds = Column(Integer, default=0, nullable=False)
    created_at = Column(Integer, nullable=False)


class MailRefreshTokenHistory(Base):
    __tablename__ = "mail_refresh_token_history"

    id = Column(Integer, primary_key=True, index=True)
    mail_account_id = Column(Integer, index=True, nullable=False)
    old_refresh_token = Column(Text, default="", nullable=False)
    update_time = Column(Integer, nullable=False)


class ApiKey(Base):
    __tablename__ = "api_key"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(Text, default="", nullable=False)
    key = Column(Text, unique=True, index=True, nullable=False)
    created_at = Column(Integer, nullable=False)
    last_used_at = Column(Integer, default=0, nullable=False)


class Proxy(Base):
    __tablename__ = "proxy"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(Text, default="", nullable=False)
    proxy_type = Column(Text, default="http", nullable=False)   # http / socks5
    host = Column(Text, nullable=False)
    port = Column(Integer, nullable=False)
    username = Column(Text, default="", nullable=False)
    password = Column(Text, default="", nullable=False)
    status = Column(Integer, default=1, nullable=False)         # 1=正常 0=失效
    use_count = Column(Integer, default=0, nullable=False)
    latency_ms = Column(Integer, default=0, nullable=False)     # 延迟（毫秒）
    exit_ip = Column(Text, default="", nullable=False)           # 出口 IP
    purity_info = Column(Text, default="", nullable=False)       # 纯净度 JSON
    last_used_at = Column(Integer, default=0, nullable=False)
    last_checked_at = Column(Integer, default=0, nullable=False)
    created_at = Column(Integer, nullable=False)


class MailCache(Base):
    """邮件缓存表：存储每个账号每个文件夹的邮件列表"""
    __tablename__ = "mail_cache"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, index=True, nullable=False)
    folder = Column(Text, default="inbox", nullable=False)         # inbox / junk
    mails_json = Column(Text, default="[]", nullable=False)        # 邮件列表 JSON
    mail_count = Column(Integer, default=0, nullable=False)
    updated_at = Column(Integer, default=0, nullable=False)
