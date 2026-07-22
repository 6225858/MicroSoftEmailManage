# ChatGPT 邮箱自动领取与验证码接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `pix-automation-2.4.3` 从局域网 `MicroSoftEmailManage` 自动领取未注册 ChatGPT 的邮箱、严格读取验证码，并在取得 Session Token 后可靠标记完成。

**Architecture:** 邮箱服务增加持久化领取租约、严格 ChatGPT 邮件匹配器和四个专用 API；插件后台服务工作线程统一持有 API Key、领取 Token、完成重试和释放逻辑。侧栏只负责配置、展示与启动，内容脚本只负责页面自动化和上报 Session 成功。

**Tech Stack:** Python 3、FastAPI、SQLAlchemy、SQLite、Python `unittest`、Chrome/Edge Manifest V3、JavaScript、Node.js 18+ `node:test`、Playwright。

## Global Constraints

- 邮箱项目根目录：`D:\projects\对话\MicroSoftEmailManage`。
- 插件根目录：`D:\projects\codextask\pix-automation-2.4.3\pix-automation`。
- 默认邮箱服务地址必须是 `http://192.168.1.27:10019/`，同时允许用户配置其他 HTTP/HTTPS 地址。
- 所有 `/api/automation/chatgpt/*` 接口必须要求非空且有效的 `X-Api-Key`；不得改变既有 API 的认证兼容性。
- 插件不得向邮箱服务发送邮箱密码、`client_id`、`refresh_token` 或 Session Token。
- 只有精确发件人 `noreply@tm.openai.com`、精确标题 `Your temporary ChatGPT verification code`、正确收件人和固定正文语句后的独立六位数字可自动填写。
- 普通领取有效期为 15 分钟；找到验证码后延长为 24 小时；完成回执保留 24 小时。
- 完成标签必须是精确标签 `已注册chatgpt`；标签匹配不得使用子串判断。
- API Key、领取 Token、验证码、邮箱明文和邮箱凭据不得进入操作日志。
- 测试不得访问真实 Microsoft 邮箱或提交真实 ChatGPT 注册。
- `MicroSoftEmailManage` 是 Git 仓库，后端任务按任务提交；插件目录不是 Git 仓库，插件任务以测试通过和文件清单作为检查点，不初始化新仓库。

---

## File Structure

### 邮箱服务

- `D:\projects\对话\MicroSoftEmailManage\models.py`：新增领取记录 ORM 模型。
- `D:\projects\对话\MicroSoftEmailManage\chatgpt_automation_service.py`：领取、续租、完成、释放、标签处理、邮件严格匹配的领域逻辑。
- `D:\projects\对话\MicroSoftEmailManage\icutool_mail.py`：表结构迁移、严格 API Key 依赖、Pydantic 请求体和四个 FastAPI 路由；协调现有邮件缓存刷新。
- `D:\projects\对话\MicroSoftEmailManage\tests\test_chatgpt_automation_service.py`：租约、标签、并发和邮件匹配测试。
- `D:\projects\对话\MicroSoftEmailManage\tests\test_chatgpt_automation_api.py`：认证、状态码、缓存协调和路由响应测试。

### 插件

- `D:\projects\codextask\pix-automation-2.4.3\pix-automation\background.js`：邮箱服务 API 客户端、领取任务持久化、验证码请求、完成重试、释放和 alarm 调度。
- `D:\projects\codextask\pix-automation-2.4.3\pix-automation\sidepanel.html`：只读领取邮箱、服务地址和 API Key 控件。
- `D:\projects\codextask\pix-automation-2.4.3\pix-automation\sidepanel.js`：保存配置、启动领取、显示任务、清理分支和手动 Session 完成通知。
- `D:\projects\codextask\pix-automation-2.4.3\pix-automation\sidepanel.css`：只读邮箱和密钥控件的现有设计系统适配。
- `D:\projects\codextask\pix-automation-2.4.3\pix-automation\auto-login.js`：使用领取 Token 查询验证码，并在 Session 成功时交由后台完成，不提前删除任务。
- `D:\projects\codextask\pix-automation-2.4.3\pix-automation\manifest.json`：增加 `alarms` 权限。
- `D:\projects\codextask\pix-automation-2.4.3\pix-automation\tests\mail-code.test.js`：替换旧寻邮协议测试为新邮箱服务客户端和完成重试测试。
- `D:\projects\codextask\pix-automation-2.4.3\pix-automation\tests\login-progress-smoke.js`：更新领取任务和验证码来源断言。
- `D:\projects\codextask\pix-automation-2.4.3\pix-automation\tests\ui-smoke.js`：验证配置、只读邮箱和启动/清理交互。
- 两个项目的 `README.md`：部署、API Key、标签生命周期和使用说明。

---

### Task 1: 持久化邮箱领取与标签生命周期

**Files:**
- Modify: `D:\projects\对话\MicroSoftEmailManage\models.py`
- Create: `D:\projects\对话\MicroSoftEmailManage\chatgpt_automation_service.py`
- Modify: `D:\projects\对话\MicroSoftEmailManage\icutool_mail.py`
- Create: `D:\projects\对话\MicroSoftEmailManage\tests\test_chatgpt_automation_service.py`

**Interfaces:**
- Produces: `ChatgptEmailClaim` ORM model.
- Produces: `AutomationError(code: str, status_code: int, message: str)`.
- Produces: `claim_email(db: Session, now: int | None = None, token_factory: Callable[[], str] | None = None) -> dict`.
- Produces: `resolve_active_claim(db: Session, claim_token: str, now: int | None = None) -> tuple[ChatgptEmailClaim, MailAccount]`.
- Produces: `renew_claim(db: Session, claim: ChatgptEmailClaim, now: int | None = None, code_found: bool = False) -> int`.
- Produces: `complete_claim(db: Session, claim_token: str, now: int | None = None) -> dict`.
- Produces: `release_claim(db: Session, claim_token: str) -> bool`.
- Produces: `parse_tags(value: str) -> list[str]`, `has_exact_tag(value: str, tag: str) -> bool`, `append_exact_tag(value: str, tag: str) -> str`.

- [ ] **Step 1: Write failing lease and tag tests**

Create `tests/test_chatgpt_automation_service.py` with a temporary SQLite database and the core cases:

```python
import tempfile
import threading
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models import ChatgptEmailClaim, MailAccount
from chatgpt_automation_service import (
    AutomationError,
    append_exact_tag,
    claim_email,
    complete_claim,
    release_claim,
)


class ClaimServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "test.db"
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False, "timeout": 5},
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()
        self.temp_dir.cleanup()

    def add_account(self, email, tags="", valid_status=1):
        with self.Session() as db:
            account = MailAccount(
                email=email,
                tags=tags,
                valid_status=valid_status,
                created_at=1,
            )
            db.add(account)
            db.commit()

    def test_claim_excludes_exact_registered_tag_and_invalid_accounts(self):
        self.add_account("registered@example.com", "vip,已注册chatgpt")
        self.add_account("similar@example.com", "未已注册chatgpt测试")
        self.add_account("invalid@example.com", valid_status=0)
        with self.Session() as db:
            result = claim_email(db, now=1000, token_factory=lambda: "claim-a")
        self.assertEqual(result["email"], "similar@example.com")
        self.assertEqual(result["claim_token"], "claim-a")
        self.assertEqual(result["expires_at"], 1900)

    def test_complete_is_idempotent_and_preserves_tags(self):
        self.add_account("user@example.com", "vip,测试")
        with self.Session() as db:
            claim_email(db, now=1000, token_factory=lambda: "claim-b")
            first = complete_claim(db, "claim-b", now=1100)
            second = complete_claim(db, "claim-b", now=1101)
            account = db.query(MailAccount).filter_by(email="user@example.com").one()
        self.assertEqual(first, {"ok": True, "status": "completed"})
        self.assertEqual(second, first)
        self.assertEqual(account.tags, "vip,测试,已注册chatgpt")

    def test_release_is_idempotent_but_cannot_release_completed_claim(self):
        self.add_account("user@example.com")
        with self.Session() as db:
            claim_email(db, now=1000, token_factory=lambda: "claim-c")
            self.assertTrue(release_claim(db, "claim-c"))
            self.assertFalse(release_claim(db, "claim-c"))
```

Add tests in the same file for an expired 15-minute lease, 24-hour completed receipt cleanup, code-found renewal, `no_available_email`, and two threads claiming from a pool of two accounts. The concurrency assertion must compare the two returned email addresses and require two distinct values.

- [ ] **Step 2: Run the new tests and verify the missing symbols fail**

Run from `D:\projects\对话\MicroSoftEmailManage`:

```powershell
python -m unittest tests.test_chatgpt_automation_service -v
```

Expected: FAIL on importing `ChatgptEmailClaim` or `chatgpt_automation_service`.

- [ ] **Step 3: Add the ORM model and startup schema migration**

Append this model to `models.py`:

```python
class ChatgptEmailClaim(Base):
    __tablename__ = "chatgpt_email_claim"

    id = Column(Integer, primary_key=True, index=True)
    mail_account_id = Column(Integer, unique=True, index=True, nullable=False)
    claim_token = Column(Text, unique=True, index=True, nullable=False)
    status = Column(Text, default="active", nullable=False)
    claimed_at = Column(Integer, nullable=False)
    expires_at = Column(Integer, index=True, nullable=False)
    completed_at = Column(Integer, default=0, nullable=False)
```

Add `ensure_chatgpt_email_claim_schema()` next to the existing schema helpers in `icutool_mail.py`. It must create the table and both unique indexes with `CREATE TABLE/INDEX IF NOT EXISTS`, add missing columns for upgrade compatibility, and be called during startup schema initialization.

- [ ] **Step 4: Implement the minimal lease service**

Create `chatgpt_automation_service.py` with constants and exact signatures:

```python
ACTIVE_LEASE_SECONDS = 15 * 60
CODE_FOUND_LEASE_SECONDS = 24 * 60 * 60
COMPLETED_RECEIPT_SECONDS = 24 * 60 * 60
REGISTERED_TAG = "已注册chatgpt"


class AutomationError(Exception):
    def __init__(self, code: str, status_code: int, message: str):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.message = message


def parse_tags(value: str) -> list[str]:
    result = []
    for raw in str(value or "").replace("，", ",").split(","):
        tag = raw.strip()
        if tag and tag not in result:
            result.append(tag)
    return result


def has_exact_tag(value: str, tag: str) -> bool:
    return tag in parse_tags(value)


def append_exact_tag(value: str, tag: str) -> str:
    tags = parse_tags(value)
    if tag not in tags:
        tags.append(tag)
    return ",".join(tags)
```

`claim_email` must issue `BEGIN IMMEDIATE`, delete expired active rows and completed receipts older than 24 hours, load valid accounts, filter tags in Python using `has_exact_tag`, exclude claimed account IDs, then insert one claim. Use `secrets.token_urlsafe(32)` by default and retry an `IntegrityError` caused by a competing claimant. Never return account credentials.

`complete_claim` must atomically append the tag and change the row to `completed`; a repeated completed Token returns the same success. `release_claim` deletes only `active` rows and raises `AutomationError("claim_completed", 409, ...)` for completed rows.

- [ ] **Step 5: Run lease tests until all cases pass**

```powershell
python -m unittest tests.test_chatgpt_automation_service -v
```

Expected: all lease, tag, expiry, idempotency and concurrency tests PASS.

- [ ] **Step 6: Run schema and syntax checks**

```powershell
python -m py_compile .\models.py .\chatgpt_automation_service.py .\icutool_mail.py
python -c "from icutool_mail import app; print(any(r.path == '/api/automation/chatgpt/claims' for r in app.routes))"
```

Expected: compilation succeeds. The route probe prints `False` at this task boundary because routes are added in Task 2.

- [ ] **Step 7: Commit the backend lease deliverable**

```powershell
git add models.py chatgpt_automation_service.py icutool_mail.py tests/test_chatgpt_automation_service.py
git commit -m "feat: add chatgpt email claim lifecycle"
```

---

### Task 2: 严格验证码匹配与自动化 API

**Files:**
- Modify: `D:\projects\对话\MicroSoftEmailManage\chatgpt_automation_service.py`
- Modify: `D:\projects\对话\MicroSoftEmailManage\icutool_mail.py`
- Modify: `D:\projects\对话\MicroSoftEmailManage\tests\test_chatgpt_automation_service.py`
- Create: `D:\projects\对话\MicroSoftEmailManage\tests\test_chatgpt_automation_api.py`

**Interfaces:**
- Consumes: all Task 1 service interfaces.
- Produces: `extract_chatgpt_code(body: str) -> str`.
- Produces: `find_latest_chatgpt_code(folder_mails: dict[str, list[dict]], email: str, not_before_ms: int) -> dict | None`.
- Produces: `require_automation_api_key(x_api_key: str | None, db: Session) -> ApiKey`.
- Produces routes: `POST /api/automation/chatgpt/claims`, `/verification-code`, `/claims/complete`, `/claims/release`.

- [ ] **Step 1: Write failing strict mail matcher tests**

Add the confirmed sample and rejection cases to `test_chatgpt_automation_service.py`:

```python
from chatgpt_automation_service import find_latest_chatgpt_code


def matching_mail(code="919020", received="2026-07-22 13:48:45"):
    return {
        "subject": "Your temporary ChatGPT verification code",
        "mail_from": "ChatGPT (noreply@tm.openai.com)",
        "mail_to": "user@outlook.com (user@outlook.com)",
        "mail_dt": received,
        "body": (
            "<p>Enter this temporary verification code to continue:</p>"
            f"<p>{code}</p>"
            "<a href='https://u20216706.ct.sendgrid.net/123456789'>ChatGPT</a>"
        ),
    }


class VerificationMatcherTest(unittest.TestCase):
    def test_extracts_only_anchored_six_digit_code(self):
        result = find_latest_chatgpt_code(
            {"inbox": [matching_mail()], "junk": []},
            "user@outlook.com",
            1784699200000,
        )
        self.assertEqual(result["code"], "919020")
        self.assertEqual(result["folder"], "inbox")

    def test_rejects_wrong_sender_subject_recipient_stale_and_non_six_digit(self):
        variants = []
        for field, value in [
            ("mail_from", "attacker@example.com"),
            ("subject", "Your verification code"),
            ("mail_to", "other@outlook.com"),
            ("mail_dt", "2026-07-22 12:00:00"),
            ("body", "Enter this temporary verification code to continue: 12345"),
        ]:
            item = matching_mail()
            item[field] = value
            variants.append(item)
        for item in variants:
            with self.subTest(item=item):
                self.assertIsNone(find_latest_chatgpt_code(
                    {"inbox": [item]}, "user@outlook.com", 1784699200000
                ))
```

Use a fixed `not_before_ms` that corresponds to the sample time minus less than 120 seconds. Add a case where Inbox and Junk both match and the later `mail_dt` wins. Add a case proving a six-digit SendGrid URL number without the fixed sentence is ignored.

- [ ] **Step 2: Write failing API/auth tests**

Create `tests/test_chatgpt_automation_api.py`. Use a temporary SQLAlchemy session, `unittest.mock.patch`, and direct route function calls so no extra HTTP test dependency is required:

```python
import unittest
from unittest.mock import patch
from fastapi import HTTPException

from chatgpt_automation_service import claim_email
from icutool_mail import (
    ChatgptClaimTokenBody,
    ChatgptVerificationCodeBody,
    claim_chatgpt_email,
    get_chatgpt_verification_code,
    require_automation_api_key,
)


def matching_mail():
    return {
        "subject": "Your temporary ChatGPT verification code",
        "mail_from": "ChatGPT (noreply@tm.openai.com)",
        "mail_to": "user@outlook.com (user@outlook.com)",
        "mail_dt": "2026-07-22 13:48:45",
        "body": "Enter this temporary verification code to continue: 919020",
    }


class AutomationApiTest(unittest.TestCase):
    def test_strict_auth_rejects_missing_key(self):
        with self.assertRaises(HTTPException) as caught:
            require_automation_api_key(None, self.db)
        self.assertEqual(caught.exception.status_code, 401)

    @patch("icutool_mail.refresh_mail_cache_async")
    @patch("icutool_mail.get_mail_cache")
    def test_verification_route_returns_minimal_response(self, get_cache, refresh):
        self.add_account("user@outlook.com")
        claim_email(self.db, now=1784699200, token_factory=lambda: "claim-a")
        get_cache.side_effect = [
            {"items": [matching_mail()], "updated_at": 1, "is_fresh": True},
            None,
        ]
        response = get_chatgpt_verification_code(
            ChatgptVerificationCodeBody(claim_token="claim-a", not_before=1784699200000),
            db=self.db,
        )
        self.assertEqual(set(response), {"code", "received_at", "folder"})
```

Also assert missing/invalid Key returns `401`, no available account maps to `409` with `detail.code`, expired claim maps to `410`, complete and release responses match the spec, and only `email`, `claim_token`, `expires_at` appear in a claim response.

- [ ] **Step 3: Run both test modules and verify matcher/routes are missing**

```powershell
python -m unittest tests.test_chatgpt_automation_service tests.test_chatgpt_automation_api -v
```

Expected: FAIL on missing matcher, request models, strict auth dependency or route functions.

- [ ] **Step 4: Implement HTML parsing, address checks and time normalization**

In `chatgpt_automation_service.py`, add a small `HTMLParser` subclass that collects visible text, call `html.unescape`, and normalize whitespace. Use these constants and regex:

```python
EXPECTED_SENDER = "noreply@tm.openai.com"
EXPECTED_SUBJECT = "Your temporary ChatGPT verification code"
BODY_CODE_RE = re.compile(
    r"Enter\s+this\s+temporary\s+verification\s+code\s+to\s+continue:\s*(?<!\d)(\d{6})(?!\d)",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
```

`find_latest_chatgpt_code` must parse `mail_dt` as Asia/Shanghai when it is `YYYY-MM-DD HH:MM:SS`, accept timezone-aware ISO values, enforce `not_before_ms - 120000`, reject missing/invalid timestamps, and return only:

```python
{
    "code": code,
    "received_at": received_datetime.isoformat(),
    "folder": folder,
}
```

- [ ] **Step 5: Add strict auth and request models**

In `icutool_mail.py`, define:

```python
class ChatgptClaimTokenBody(BaseModel):
    claim_token: str


class ChatgptVerificationCodeBody(ChatgptClaimTokenBody):
    not_before: int


def require_automation_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key"),
    db: Session = Depends(get_db),
):
    value = (x_api_key or "").strip()
    record = db.query(ApiKey).filter(ApiKey.key == value).first() if value else None
    if record is None:
        raise HTTPException(status_code=401, detail={
            "code": "invalid_api_key",
            "message": "API Key 缺失或无效",
        })
    record.last_used_at = int(time.time())
    db.commit()
    return record
```

Do not modify existing `require_api_key`.

- [ ] **Step 6: Add the four routes and cache coordination**

Add the routes with `dependencies=[Depends(require_automation_api_key)]`. Catch `AutomationError` and translate it to `HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message})`.

For verification, resolve the claim, launch `refresh_mail_cache_async(account.id, folder, limit=10)` for `inbox` and `junk`, inspect `get_mail_cache`, and only wait for launched tasks when neither cache contains items. Use one shared 10-second deadline, reload caches after waiting, and return `502 mail_fetch_failed` only when all folders failed and no cached items exist. Pass `{"inbox": [...], "junk": [...]}` to `find_latest_chatgpt_code`, then call `renew_claim(..., code_found=bool(match))`.

Return the exact response shapes in the design spec; no route may serialize a `MailAccount` object.

- [ ] **Step 7: Run backend tests and inspect OpenAPI paths**

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
python -c "from icutool_mail import app; paths=app.openapi()['paths']; print(sorted(p for p in paths if p.startswith('/api/automation/chatgpt/')))"
```

Expected: all tests PASS and exactly four automation paths are printed.

- [ ] **Step 8: Commit the backend API deliverable**

```powershell
git add chatgpt_automation_service.py icutool_mail.py tests/test_chatgpt_automation_service.py tests/test_chatgpt_automation_api.py
git commit -m "feat: expose chatgpt verification automation api"
```

---

### Task 3: 插件后台邮箱客户端与可靠完成重试

**Files:**
- Modify: `D:\projects\codextask\pix-automation-2.4.3\pix-automation\background.js`
- Modify: `D:\projects\codextask\pix-automation-2.4.3\pix-automation\manifest.json`
- Modify: `D:\projects\codextask\pix-automation-2.4.3\pix-automation\tests\mail-code.test.js`

**Interfaces:**
- Produces constants: `MAIL_SERVICE_ADDRESS_KEY`, `MAIL_SERVICE_API_KEY`, `AUTO_LOGIN_TASK_KEY`, `COMPLETION_RETRY_ALARM`.
- Produces: `normalizeMailServiceBase(value: string) -> string`.
- Produces: `mailServiceRequest(path: string, options: object, fetchImpl?: Function) -> Promise<object>`.
- Produces: `startClaimedLogin(payload: object, chromeApi?: object, fetchImpl?: Function) -> Promise<object>`.
- Produces: `fetchAutoLoginCode(payload: object, chromeApi?: object, fetchImpl?: Function) -> Promise<object>`.
- Produces: `completePendingClaim(chromeApi?: object, fetchImpl?: Function) -> Promise<object>`.
- Produces: `releaseActiveClaim(chromeApi?: object, fetchImpl?: Function) -> Promise<object>`.
- Runtime messages: `START_CHATGPT_LOGIN`, `AUTO_LOGIN_FETCH_CODE`, `AUTO_LOGIN_SESSION_READY`, `CLEAR_CHATGPT_LOGIN_TASK`.

- [ ] **Step 1: Replace old protocol tests with failing new client tests**

Rewrite `tests/mail-code.test.js` around the new exports. The core assertions are:

```javascript
test("claims an email without sending Outlook credentials", async () => {
  const calls = [];
  const chromeApi = fakeChrome({
    mailServiceAddressV1: "http://192.168.1.27:10019/",
    mailServiceApiKeyV1: "ms-secret",
  });
  const result = await startClaimedLogin({
    taskId: "L-1000",
    profileName: "Taylor",
    profileAge: 25,
    profileBirthDate: "2000-01-02",
    startedAt: 1784699200000,
  }, chromeApi, async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse({ email: "user@outlook.com", claim_token: "claim-a", expires_at: 1784700100 });
  });
  assert.equal(result.email, "user@outlook.com");
  assert.equal(calls[0].url, "http://192.168.1.27:10019/api/automation/chatgpt/claims");
  assert.equal(calls[0].options.headers["X-Api-Key"], "ms-secret");
  assert.ok(!String(calls[0].options.body || "").match(/password|client_id|refresh_token/i));
});

test("fetches code by claim token only", async () => {
  // Seed a stored task with claimToken and startedAt.
  // Assert request body is exactly { claim_token: "claim-a", not_before: 1784699200000 }.
  // Assert response { code: "919020", folder: "inbox" } is returned.
});

test("persists pending completion and retries idempotently", async () => {
  // First complete request returns HTTP 503, second returns completed.
  // Assert completionPending remains after the first call, an alarm is created,
  // then the task is removed and the alarm cleared after the second call.
});
```

The local `fakeChrome` must implement `storage.local.get/set/remove`, `alarms.create/clear`, and retain state so assertions inspect it. Add tests for `401` no-retry behavior, release of active claims, refusal to release `completionPending`, and sanitization of server error messages.

- [ ] **Step 2: Run the Node test and verify old exports/new symbols fail**

```powershell
node --test .\tests\mail-code.test.js
```

Expected: FAIL because the new lifecycle functions and storage keys do not exist.

- [ ] **Step 3: Replace the Xunmail client with a minimal mailbox service client**

In `background.js`, remove `buildXunmailEndpoints`, credential payloads, Graph/OAuth fallback and generic mail parsing. Add:

```javascript
const MAIL_SERVICE_ADDRESS_KEY = "mailServiceAddressV1";
const MAIL_SERVICE_API_KEY = "mailServiceApiKeyV1";
const AUTO_LOGIN_TASK_KEY = "chatgptAutoLoginTaskV27";
const COMPLETION_RETRY_ALARM = "chatgptClaimCompletionRetryV1";
const DEFAULT_MAIL_SERVICE_ADDRESS = "http://192.168.1.27:10019/";

function normalizeMailServiceBase(value = DEFAULT_MAIL_SERVICE_ADDRESS) {
  const url = new URL(String(value || DEFAULT_MAIL_SERVICE_ADDRESS).trim());
  if (!/^https?:$/.test(url.protocol)) throw new Error("邮箱服务地址只支持 HTTP 或 HTTPS");
  url.username = "";
  url.password = "";
  url.search = "";
  url.hash = "";
  return `${url.origin}${url.pathname.replace(/\/+$/, "")}`;
}
```

`mailServiceRequest` must load address and API Key from `chrome.storage.local`, reject an empty Key before fetch, set `X-Api-Key`, `Content-Type`, `Accept`, `Cache-Control: no-store`, use a 30-second timeout, parse structured `detail.code/message`, and never include the Key or claim Token in thrown messages.

- [ ] **Step 4: Implement start, code, complete and release state transitions**

Store tasks in this exact shape:

```javascript
{
  taskId,
  email,
  claimToken,
  claimExpiresAt,
  startedAt,
  profileName,
  profileAge,
  profileBirthDate,
  completionPending: false,
}
```

`startClaimedLogin` posts to `/claims`, persists the task immediately, and returns `{ok:true,email,taskId}`. `fetchAutoLoginCode` reads the stored task and posts only `claim_token` and `not_before`.

`completePendingClaim` first persists `completionPending:true`, posts to `/claims/complete`, removes the task and clears the alarm only on success. On network/5xx failure it keeps the task and schedules an alarm every minute. A `401` response remains pending but does not tight-loop; user-visible status identifies configuration failure without exposing Key data.

`releaseActiveClaim` must call `/claims/release` only when `completionPending !== true`; after an idempotent release response it removes the task.

- [ ] **Step 5: Wire runtime and alarm handlers**

Extend the single existing `chrome.runtime.onMessage` listener:

```javascript
if (message?.type === "START_CHATGPT_LOGIN") {
  startClaimedLogin(message.payload).then(sendResponse).catch(toSafeError(sendResponse));
  return true;
}
if (message?.type === "AUTO_LOGIN_SESSION_READY") {
  completePendingClaim().then(sendResponse).catch((error) => {
    sendResponse({ ok: false, pending: true, error: safeMessage(error) });
  });
  return true;
}
if (message?.type === "CLEAR_CHATGPT_LOGIN_TASK") {
  releaseActiveClaim().then(sendResponse).catch(toSafeError(sendResponse));
  return true;
}
```

Register `chrome.alarms.onAlarm` for `COMPLETION_RETRY_ALARM`, call `completePendingClaim` when it fires, and invoke a guarded retry once when the service worker starts. Add `"alarms"` to `manifest.json` permissions.

- [ ] **Step 6: Run client tests and syntax checks**

```powershell
node --test .\tests\mail-code.test.js
node --check .\background.js
node -e "const m=require('./manifest.json'); if(!m.permissions.includes('alarms')) process.exit(1)"
```

Expected: all tests PASS, syntax succeeds, manifest check exits 0.

- [ ] **Step 7: Record the non-Git plugin checkpoint**

Run:

```powershell
Get-Item .\background.js, .\manifest.json, .\tests\mail-code.test.js | Select-Object Name,Length,LastWriteTime
```

Expected: the three files are present. Do not run `git init`; the plugin directory is intentionally delivered as a tested working tree.

---

### Task 4: 侧栏自动领取 UI 与清理流程

**Files:**
- Modify: `D:\projects\codextask\pix-automation-2.4.3\pix-automation\sidepanel.html`
- Modify: `D:\projects\codextask\pix-automation-2.4.3\pix-automation\sidepanel.js`
- Modify: `D:\projects\codextask\pix-automation-2.4.3\pix-automation\sidepanel.css`
- Modify: `D:\projects\codextask\pix-automation-2.4.3\pix-automation\tests\ui-smoke.js`

**Interfaces:**
- Consumes: Task 3 storage keys and runtime messages.
- Produces UI IDs: existing `accountInput` as read-only input, existing `mailApiAddressInput`, new `mailApiKeyInput`.
- Produces: `loadMailServiceSettings()`, `saveMailServiceSettings()`, `startLogin()`, `clearLoginWorkflow()`.

- [ ] **Step 1: Update the UI smoke test first**

In `tests/ui-smoke.js`, seed storage with `mailServiceAddressV1` and `mailServiceApiKeyV1`, mock runtime responses for `START_CHATGPT_LOGIN` and `CLEAR_CHATGPT_LOGIN_TASK`, then assert:

```javascript
await page.click("#startLoginBtn");
await page.waitForFunction(() => document.querySelector("#accountInput").value === "user@outlook.com");
const state = await page.evaluate(() => ({
  readonly: document.querySelector("#accountInput").readOnly,
  address: document.querySelector("#mailApiAddressInput").value,
  keyType: document.querySelector("#mailApiKeyInput").type,
}));
assert.deepEqual(state, {
  readonly: true,
  address: "http://192.168.1.27:10019/",
  keyType: "password",
});
```

Assert the runtime start payload contains profile fields and `startedAt`, but no account credentials or API Key. After clicking clear, assert `CLEAR_CHATGPT_LOGIN_TASK` was sent before the display was emptied.

- [ ] **Step 2: Run the smoke test and verify the new UI contract fails**

```powershell
node .\tests\ui-smoke.js
```

Expected: FAIL because `mailApiKeyInput` and automatic claim behavior do not exist.

- [ ] **Step 3: Replace the account textarea and add API Key input**

Change the registration controls to:

```html
<label class="field">
  <span>当前领取邮箱</span>
  <input id="accountInput" type="email" placeholder="开始任务后自动领取" readonly autocomplete="off">
</label>
<details class="advanced-settings">
  <summary>邮箱服务设置</summary>
  <label class="field">
    <span>邮箱服务地址</span>
    <input id="mailApiAddressInput" type="url" placeholder="http://192.168.1.27:10019/" autocomplete="url" spellcheck="false">
  </label>
  <label class="field">
    <span>API Key</span>
    <input id="mailApiKeyInput" type="password" placeholder="ms_..." autocomplete="off" spellcheck="false">
  </label>
</details>
```

Keep the established compact side-panel styling. Add only focused CSS for readonly contrast and prevent the email value from overflowing its field.

- [ ] **Step 4: Replace manual account parsing with automatic claim startup**

Set the side-panel constants to the Task 3 keys and default address. `loadMailServiceSettings` loads both values. `saveMailServiceSettings` normalizes the address, requires a nonempty Key, saves both, and requests `${new URL(address).origin}/*` Host Permission.

`startLogin` must generate the profile first, send this payload, and only navigate after a successful claim:

```javascript
const result = await chrome.runtime.sendMessage({
  type: "START_CHATGPT_LOGIN",
  payload: {
    taskId: createTaskId("L"),
    startedAt: Date.now(),
    profileName: profile.name,
    profileAge: profile.age,
    profileBirthDate: profile.birthDate,
  },
});
```

Set `accountInput.value` and `currentAccountEmail` from `result.email`. Remove the four-part credential parser and all `manualCodeOnly` branches. Keep `accountInput.value` as the email source required by the downstream payment stage.

- [ ] **Step 5: Implement lifecycle-aware clear and restore**

On `DOMContentLoaded`, restore the active task email and status from `AUTO_LOGIN_TASK_KEY`. The clear button calls `CLEAR_CHATGPT_LOGIN_TASK` before local UI reset. If the response is `{pending:true}` or an error indicates completion is pending, keep the displayed email and explain that the completion mark will retry; never remove the task directly in that branch.

The “清除 ChatGPT 网站数据” action must invoke the same lifecycle-aware clear operation before removing login task storage. It may still clear site cookies afterward, but cannot blindly delete a pending completion task.

- [ ] **Step 6: Run UI smoke and syntax checks**

```powershell
node .\tests\ui-smoke.js
node --check .\sidepanel.js
```

Expected: UI smoke PASS and syntax succeeds.

- [ ] **Step 7: Record the non-Git plugin checkpoint**

```powershell
Get-Item .\sidepanel.html, .\sidepanel.js, .\sidepanel.css, .\tests\ui-smoke.js | Select-Object Name,Length,LastWriteTime
```

Expected: all four changed files are present; do not initialize a repository.

---

### Task 5: 内容脚本验证码和 Session 完成通知

**Files:**
- Modify: `D:\projects\codextask\pix-automation-2.4.3\pix-automation\auto-login.js`
- Modify: `D:\projects\codextask\pix-automation-2.4.3\pix-automation\sidepanel.js`
- Modify: `D:\projects\codextask\pix-automation-2.4.3\pix-automation\tests\login-progress-smoke.js`
- Modify: `D:\projects\codextask\pix-automation-2.4.3\pix-automation\tests\profile-auto-smoke.js`

**Interfaces:**
- Consumes: stored task shape and runtime handlers from Task 3.
- Produces content message payload `{type:"AUTO_LOGIN_FETCH_CODE", payload:{claimToken, notBefore}}`.
- Produces Session message `{type:"AUTO_LOGIN_SESSION_READY", accessToken, taskId}` without deleting pending task storage.

- [ ] **Step 1: Update login smoke fixtures to claimed tasks**

In `tests/login-progress-smoke.js` and `tests/profile-auto-smoke.js`, replace `clientId`/`refreshToken` fixtures with:

```javascript
{
  email: "person@example.com",
  claimToken: "claim-smoke",
  claimExpiresAt: Math.floor(Date.now() / 1000) + 900,
  startedAt: Date.now(),
  taskId: "L-CODE1",
  completionPending: false,
}
```

Mock `AUTO_LOGIN_FETCH_CODE` as `{ok:true,code:"654321",folder:"inbox"}`. Assert the outgoing payload has exactly `claimToken` and `notBefore`, progress text refers to `收件箱`, and neither the code nor claim Token appears in progress logs.

Add a Session-ready smoke assertion that `AUTO_LOGIN_SESSION_READY` is sent and storage `remove(AUTO_LOGIN_TASK_KEY)` is not called by the content script.

- [ ] **Step 2: Run both smoke tests and verify old credential assumptions fail**

```powershell
node .\tests\login-progress-smoke.js
node .\tests\profile-auto-smoke.js
```

Expected: at least the login smoke FAILS because `auto-login.js` still requires Client ID and Refresh Token.

- [ ] **Step 3: Simplify task validation and code polling payload**

Remove `parseAccount` credential parsing from `auto-login.js`. A task is eligible for automatic code polling only when it contains a valid `email`, nonempty `claimToken`, and numeric `startedAt`.

Replace `fetchVerificationCode` with:

```javascript
async function fetchVerificationCode(account) {
  const result = await chrome.runtime.sendMessage({
    type: "AUTO_LOGIN_FETCH_CODE",
    payload: {
      claimToken: account.claimToken,
      notBefore: account.startedAt,
    },
  });
  if (result?.ok === false) throw new Error(result.error || "验证码获取失败");
  return {
    code: String(result?.code || ""),
    mailbox: result?.folder === "junk" ? "垃圾箱" : "收件箱",
  };
}
```

Require exactly six digits before automatic fill. Keep the manual 4–8 digit control as a separate user action.

- [ ] **Step 4: Hand Session completion to the background before task cleanup**

In `notifySessionReady`, remove direct `clearTask()`. After copying the Session Token, send `AUTO_LOGIN_SESSION_READY` and treat `{ok:false,pending:true}` as “登录已完成，邮箱完成标记等待重试”, not as a registration failure. The background owns removal after confirmed completion.

Update `sidepanel.js` manual `readSessionToken` path so a successfully read Session also sends `AUTO_LOGIN_SESSION_READY`; this ensures manual recovery still marks the mailbox.

- [ ] **Step 5: Run smoke, unit and syntax tests**

```powershell
node .\tests\login-progress-smoke.js
node .\tests\profile-auto-smoke.js
node --test .\tests\mail-code.test.js
node --check .\auto-login.js
node --check .\sidepanel.js
```

Expected: all tests PASS and both syntax checks succeed.

- [ ] **Step 6: Record the non-Git plugin checkpoint**

```powershell
Get-Item .\auto-login.js, .\sidepanel.js, .\tests\login-progress-smoke.js, .\tests\profile-auto-smoke.js | Select-Object Name,Length,LastWriteTime
```

Expected: all files are present; do not initialize a repository.

---

### Task 6: 文档、全量回归和部署验证

**Files:**
- Modify: `D:\projects\对话\MicroSoftEmailManage\README.md`
- Modify: `D:\projects\codextask\pix-automation-2.4.3\pix-automation\README.md`

**Interfaces:**
- Consumes: all prior task interfaces.
- Produces: operator instructions for creating an API Key, LAN URL configuration, automatic claim, exact tag lifecycle and recovery.

- [ ] **Step 1: Write documentation acceptance checks**

Run these searches before editing:

```powershell
rg -n "192\.168\.1\.27:10019|X-Api-Key|已注册chatgpt|claim|自动领取" README.md
rg -n "Xunmail|寻邮|client_id|refresh_token|四段" "D:\projects\codextask\pix-automation-2.4.3\pix-automation\README.md"
```

Expected: the mail README lacks the new automation contract and the plugin README still documents the old credential-based flow.

- [ ] **Step 2: Update the mail service README**

Add a section documenting:

```text
POST /api/automation/chatgpt/claims
POST /api/automation/chatgpt/verification-code
POST /api/automation/chatgpt/claims/complete
POST /api/automation/chatgpt/claims/release
X-Api-Key: <从 API 管理页面创建的 Key>
```

Explain exact `已注册chatgpt` eligibility, 15-minute lease, 24-hour code-found/completion protection, strict sender/title/body matching, and that LAN deployments must restrict port `10019` to trusted clients.

- [ ] **Step 3: Update the plugin README**

Replace old Xunmail and four-part credential instructions with: configure service address and API Key, click start to auto-claim, automatic verification, success tagging, release behavior, and pending completion retry. State that the default address is `http://192.168.1.27:10019/` and can be changed.

- [ ] **Step 4: Run complete backend verification**

From `D:\projects\对话\MicroSoftEmailManage`:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
python -m py_compile .\database.py .\models.py .\chatgpt_automation_service.py .\icutool_mail.py
git diff --check
```

Expected: all tests PASS, compilation succeeds and `git diff --check` reports no errors.

- [ ] **Step 5: Run complete plugin verification**

From `D:\projects\codextask\pix-automation-2.4.3\pix-automation`:

```powershell
node --check .\background.js
node --check .\auto-login.js
node --check .\offscreen.js
node --check .\pix-automation.js
node --check .\sidepanel.js
node --test .\tests\clipboard.test.js .\tests\mail-code.test.js .\tests\operation-log.test.js .\tests\pix-automation.test.js .\tests\public-addresses.test.js .\tests\site-data.test.js
node .\tests\login-progress-smoke.js
node .\tests\profile-auto-smoke.js
node .\tests\ui-smoke.js
```

Expected: every syntax check, Node test and smoke test exits 0. If Playwright is unavailable, record that browser smoke tests were not run; do not report them as passing.

- [ ] **Step 6: Run a local API smoke without real mail access**

Start the service in a hidden child process, verify health, and always stop that exact process:

```powershell
$serverProcess = Start-Process -FilePath python -ArgumentList '.\icutool_mail.py' -PassThru -WindowStyle Hidden
try {
    $deadline = (Get-Date).AddSeconds(15)
    do {
        try {
            $health = Invoke-RestMethod -Uri 'http://127.0.0.1:10019/health' -TimeoutSec 2
            break
        } catch {
            if ((Get-Date) -ge $deadline) { throw }
            Start-Sleep -Milliseconds 500
        }
    } while ($true)
    if (-not $health.ok) { throw 'health check returned ok=false' }
    $health
} finally {
    Stop-Process -Id $serverProcess.Id -ErrorAction SilentlyContinue
}
```

Expected: `{ok: true}`. Do not call claim or verification against the user's real `mail.db` during automated validation.

- [ ] **Step 7: Commit backend documentation and any backend-only regression fixes**

```powershell
git add README.md
git commit -m "docs: describe chatgpt email automation"
git status --short
```

Expected: only the user's pre-existing untracked `_import_result.txt` and `_test_result.txt` remain. Do not add them.

- [ ] **Step 8: Record final plugin delivery state**

```powershell
Get-Item .\manifest.json, .\background.js, .\auto-login.js, .\sidepanel.html, .\sidepanel.js, .\sidepanel.css, .\README.md | Select-Object Name,Length,LastWriteTime
```

Expected: every delivery file is present. Report explicitly that this directory is not under Git and therefore has no implementation commit hash.

---

## Final Verification Matrix

| Requirement | Primary proof |
| --- | --- |
| 未注册邮箱自动领取 | Task 1 service tests + Task 4 UI smoke |
| 并发不重复领取 | Task 1 threaded SQLite test |
| 强制 API Key | Task 2 API tests + Task 3 header assertion |
| 不传 Microsoft 凭据 | Task 3 request-body assertion |
| 严格邮件格式 | Task 2 matcher matrix |
| Inbox/Junk 最新邮件 | Task 2 cross-folder test |
| 自动输入六位验证码 | Task 5 login smoke |
| Session 成功后加标签 | Task 1 completion test + Task 3 completion client test |
| 完成通知持久重试 | Task 3 alarm/storage test |
| 未完成任务释放 | Task 1 release test + Task 4 clear smoke |
| 敏感信息不进日志 | Task 3/5 redaction assertions |
| 不影响后续支付邮箱 | Task 4 preserves `currentAccountEmail` and `accountInput.value` |
