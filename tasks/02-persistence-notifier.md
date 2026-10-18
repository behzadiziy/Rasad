# Task 2 — Persistence & Notification: SQLite Database and Telegram Notifier

> Use this prompt together with `00-project-contract.md`. Paste the contract above this prompt, or keep it as `CLAUDE.md` in the project root when using Claude Code.

## Goal

Implement the two I/O adapters that do not depend on scraping: the async SQLite database with its status state machine, and the Telegram notifier. Telegram delivery, including the proxy, is verified early here because it is a common blocker on Iranian servers.

## Inputs

- Frozen from Task 1: `src/models.py`, `pyproject.toml`

## Deliverables

1. `src/database.py`
2. `src/notifier/__init__.py`
3. `src/notifier/telegram.py`
4. `tests/test_database.py`

## Specification

### 1. Database (`src/database.py`)

**Connection:** `__aenter__` creates the parent directory if missing, connects with `aiosqlite`, and runs `PRAGMA journal_mode=WAL;` and `PRAGMA busy_timeout=5000;`.

**Table `jobs`:**
- Columns: `id INTEGER PRIMARY KEY`, plus every `JobPost` field, plus `updated_at`.
- `work_mode` and `status` are stored as TEXT.
- `matched_profiles` is stored as a JSON array in TEXT.
- `created_at` and `updated_at` are ISO 8601 UTC TEXT.
- `UNIQUE(platform, external_id)`. This constraint already provides the lookup index, so do not create a duplicate one.
- `CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)`.

**Methods:**
- `init_db()` is idempotent and safe to call on every run.
- `save_job()` uses `INSERT OR IGNORE` and returns `True` only if a row was inserted. It is used for both `NEW` and `IGNORED` jobs.
- `get_pending_notifications()` returns jobs with status `NEW`, ordered by `created_at` then `id`. Rows are converted back to `JobPost`, with `matched_profiles` as a list and `created_at` as a timezone-aware datetime.
- `update_status()` also sets `updated_at`.

### 2. Telegram notifier (`src/notifier/telegram.py`)

**Client:** one `httpx.AsyncClient(proxy=settings.TELEGRAM_PROXY_URL, timeout=httpx.Timeout(30.0))`, owned by the context manager.

**Sending:** `send_job()` calls `sendMessage` with `parse_mode="HTML"` and `link_preview_options={"is_disabled": true}`.

**Message content:** emojis, then:
- title and company
- city
- work-mode badge: `🏠 دورکاری` for REMOTE, `🏢 حضوری` for ONSITE, `🔀 هیبریدی` for HYBRID
- salary, if present
- matched profiles
- source platform

**Safety:**
- Escape every dynamic value with `html.escape`.
- Keep the message within 4096 characters by truncating individual fields before rendering. Never cut the rendered HTML.

**Inline keyboard:**
- One URL button `🔗 مشاهده آگهی در سایت` pointing to `job.url`.
- A clearly commented block showing where the Product Phase 2/3 callback buttons (`✍️ Tailor Resume`, `🚀 Auto-Apply`) will go. The comment must note that `callback_data` is limited to 64 bytes (e.g., `tailor:jobinja:{external_id}`) and that handling callbacks requires a bot listener in Product Phase 2.

**Retries (tenacity):**
- Retry at most 5 attempts in total.
- Network errors (`httpx.TransportError`) and 5xx: exponential backoff with jitter.
- HTTP 429: raise a custom exception carrying `parameters.retry_after` from the response body. Use a custom `wait` callable that returns `retry_after` for that exception, and exponential backoff otherwise.
- Other 4xx (e.g., an invalid chat ID): do not retry; log one ERROR.
- After the final failure, return `False`. Never raise for API or network failures.

**Pacing:** `send_job` enforces a minimum interval of 1.0 s between consecutive sends, tracked internally with `time.monotonic()`, so callers can loop without sleeping.

**Smoke test:** `uv run python -m src.notifier.telegram` does the following:
1. Loads `Settings`.
2. Sends one sample job with:
   - `platform="jobinja"`
   - title `Senior AI & ML Engineer <Test>`, which proves the escaping works
   - city `مشهد`, `REMOTE`, salary `توافقی`
   - URL `https://example.com/job/123`
   - one matched profile
3. Logs the result and exits with code 0 on success, 1 on failure.

### 3. Tests (`tests/test_database.py`)

Write plain sync tests using `tmp_path` and `asyncio.run()` that verify:
- `PRAGMA journal_mode` returns `wal`
- saving a new job returns `True`, and saving a duplicate returns `False`
- an `IGNORED` job is persisted, and `job_exists` returns `True` for it
- `get_pending_notifications` returns only `NEW` jobs, oldest first
- `update_status` removes a job from the pending list and updates `updated_at`
- `matched_profiles` and a timezone-aware `created_at` survive a round trip

## Acceptance Criteria

1. `uv run pytest` passes, including the Task 1 tests.
2. With a filled `config/.env`, `uv run python -m src.notifier.telegram` delivers the test message:
   - the badge is shown
   - `&` and `<Test>` render literally
   - the URL button opens the link
3. With an invalid `TELEGRAM_CHAT_ID`, the same command logs one ERROR, does not retry, and exits with code 1.

## Self-Review Checklist

- WAL and busy_timeout applied on connect; `UNIQUE(platform, external_id)` and the status index exist
- `save_job` uses `INSERT OR IGNORE` and returns an accurate bool; works for `IGNORED`
- Datetimes stored in UTC and returned timezone-aware
- `httpx.AsyncClient(proxy=...)` used; SOCKS works through `httpx[socks]`
- All dynamic values HTML-escaped; 4096-character limit respected without breaking HTML
- 429 honors `retry_after`; other 4xx are not retried; `send_job` never raises
- Internal 1.0 s pacing between sends
- Product Phase 2/3 button extension point commented, including the 64-byte note

