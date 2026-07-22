# ChatGPT Email Automation Final Remediation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development and superpowers:systematic-debugging. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every Critical, Important, and Minor final-review finding without weakening the existing mailbox lease or plugin lifecycle ordering.

**Architecture:** Replace ambient `chrome.storage` task access with a background-owned capability protocol bound to a dedicated ChatGPT tab, a random task nonce, and a random claim identity. Keep claim and completion transitions in the existing serialized background queue, classify mailbox failures at the runtime boundary, and coordinate backend refreshes from cache metadata before starting Microsoft work.

**Tech Stack:** Python 3, FastAPI, SQLAlchemy, `unittest`, Chrome Manifest V3, JavaScript, `node:test`, Playwright.

## Global Constraints

- Backend work is limited to `D:\projects\对话\MicroSoftEmailManage\.worktrees\chatgpt-email-automation`.
- Plugin work is limited to `D:\projects\codextask\pix-automation-2.4.3\pix-automation`; do not initialize Git there.
- Do not access real Microsoft mail or submit a real ChatGPT registration.
- Do not touch the normal backend checkout, `_import_result.txt`, `_test_result.txt`, PID 36040, or port 10019.
- Status, logs, and errors must never contain email, verification code, claim token, API key, credentials, or Session Token.
- Every production behavior change requires an observed failing regression test first.

---

### Task 1: Bound-tab mailbox ownership

**Files:**
- Modify: `D:\projects\codextask\pix-automation-2.4.3\pix-automation\tests\mail-code.test.js`
- Modify: `D:\projects\codextask\pix-automation-2.4.3\pix-automation\background.js`

**Interfaces:**
- Produces stored `taskNonce`, `claimId`, and `boundTabId` fields.
- Produces `AUTO_LOGIN_GET_BOUND_TASK`, `AUTO_LOGIN_FETCH_CODE`, and `AUTO_LOGIN_SESSION_READY` validation using sender tab plus both identities.
- Produces conservative tab removal/replacement cleanup.

- [ ] Add tests proving an authenticated dedicated tab cannot claim a mailbox.
- [ ] Add tests proving generated task nonces contain at least 128 random bits and are not caller-controlled.
- [ ] Add tests proving wrong tab, wrong nonce, wrong claim identity, removed tab, and replacement tab cannot read, fetch, or complete.
- [ ] Run `node --test .\tests\mail-code.test.js` and record the expected failures.
- [ ] Implement the smallest background protocol and tab lifecycle handlers that pass the tests.
- [ ] Re-run the focused test and `node --check .\background.js`.

### Task 2: Runtime error policy and completion recovery

**Files:**
- Modify: `D:\projects\codextask\pix-automation-2.4.3\pix-automation\tests\mail-code.test.js`
- Modify: `D:\projects\codextask\pix-automation-2.4.3\pix-automation\tests\login-progress-smoke.js`
- Modify: `D:\projects\codextask\pix-automation-2.4.3\pix-automation\tests\ui-smoke.js`
- Modify: `D:\projects\codextask\pix-automation-2.4.3\pix-automation\background.js`
- Modify: `D:\projects\codextask\pix-automation-2.4.3\pix-automation\auto-login.js`
- Modify: `D:\projects\codextask\pix-automation-2.4.3\pix-automation\sidepanel.js`

**Interfaces:**
- Produces safe runtime errors `{ok:false,status,code,error}`.
- Produces exact completion receipt validation for `{ok:true,status:"completed"}`.
- Produces `RETRY_PENDING_COMPLETION` after corrected mailbox settings are saved.

- [ ] Add tests for terminal 401/404/410 polling, retryable empty/502/network/5xx polling, and sensitive-data redaction.
- [ ] Add a malformed-2xx completion receipt test that retains `completionPending` and schedules retry.
- [ ] Add a settings-save recovery test for a pending 401 completion.
- [ ] Run the focused Node test and browser smokes and record the expected failures.
- [ ] Preserve status/code through the listener and stop only terminal polling failures.
- [ ] Validate the completion receipt before storage deletion and retry a pending completion on settings save.
- [ ] Re-run focused tests and syntax checks.

### Task 3: Content-script capability handoff and sidepanel ordering

**Files:**
- Modify: `D:\projects\codextask\pix-automation-2.4.3\pix-automation\tests\login-progress-smoke.js`
- Modify: `D:\projects\codextask\pix-automation-2.4.3\pix-automation\tests\profile-auto-smoke.js`
- Modify: `D:\projects\codextask\pix-automation-2.4.3\pix-automation\tests\ui-smoke.js`
- Modify: `D:\projects\codextask\pix-automation-2.4.3\pix-automation\auto-login.js`
- Modify: `D:\projects\codextask\pix-automation-2.4.3\pix-automation\sidepanel.js`

**Interfaces:**
- Consumes the bound task protocol from Task 1.
- Produces `AUTO_LOGIN_WAKE` carrying only `taskNonce` and `claimId` to the dedicated tab.
- Produces a timeout-bounded login operation queue.

- [ ] Add smokes proving content scripts never read or write the global task storage key.
- [ ] Add smokes proving start creates a dedicated tab before claiming and manual Session completion sends an explicit bound tab ID.
- [ ] Add a short timeout test for a hung login operation without changing queue order.
- [ ] Record RED, implement minimal handoff and timeout behavior, then record GREEN.

### Task 4: Backend cache coordination and address parsing

**Files:**
- Modify: `D:\projects\对话\MicroSoftEmailManage\.worktrees\chatgpt-email-automation\tests\test_chatgpt_automation_api.py`
- Modify: `D:\projects\对话\MicroSoftEmailManage\.worktrees\chatgpt-email-automation\tests\test_chatgpt_automation_service.py`
- Modify: `D:\projects\对话\MicroSoftEmailManage\.worktrees\chatgpt-email-automation\icutool_mail.py`
- Modify: `D:\projects\对话\MicroSoftEmailManage\.worktrees\chatgpt-email-automation\chatgpt_automation_service.py`

**Interfaces:**
- Reuses `get_mail_cache()` metadata, including fresh empty entries.
- Starts refresh tasks only for missing or stale folders, waits once when needed, reloads caches, tolerates partial success, and returns 502 only when every uncached source fails.

- [ ] Add direct API tests for fresh-empty reuse, wait/reload, partial failure, and total 502 failure.
- [ ] Add parsing tests for final parenthesized addresses after display-name parentheses and invalid/aware timestamps.
- [ ] Run focused Python tests and record RED.
- [ ] Implement cache-first coordination and the minimal address parser correction.
- [ ] Re-run focused Python tests and compilation.

### Task 5: Registration semantics, documentation, and release verification

**Files:**
- Modify: `D:\projects\codextask\pix-automation-2.4.3\pix-automation\tests\mail-code.test.js`
- Modify: `D:\projects\codextask\pix-automation-2.4.3\pix-automation\README.md`
- Create: `D:\projects\对话\MicroSoftEmailManage\.worktrees\chatgpt-email-automation\.superpowers\sdd\final-fix-report.md`

**Interfaces:**
- Produces directly testable runtime, alarm, tab removal, and tab replacement listener registration.
- Documents dedicated-tab startup and concrete Save-and-Retry recovery.

- [ ] Test actual `onMessage`, `onAlarm`, `onRemoved`, and `onReplaced` registration and dispatch.
- [ ] Correct the README workflow and 401 recovery instructions.
- [ ] Run the full backend suite, compilation, and `git diff --check`.
- [ ] Run all plugin unit tests, syntax checks, and the login/profile/UI Playwright smokes with each browser run under 60 seconds.
- [ ] Audit changed text for sensitive values and inspect the full diff against every finding.
- [ ] Commit tracked backend code, tests, and docs in cohesive commits; leave plugin non-Git.
- [ ] Write the final report with RED/GREEN evidence, commit hashes, file inventory, audit, and concerns.
