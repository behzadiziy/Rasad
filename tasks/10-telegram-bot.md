# Task 10 — Two-Way Telegram Bot: Review and Approval Flow

## Goal

Make Telegram two-way. From a job notification, the user asks for a tailored resume, receives a private review package with the changes highlighted, and approves, regenerates, or rejects it.

## Authorized Changes

- **`src/notifier/telegram.py`:** in `_build_keyboard`, when `settings.allowed_user_ids` is non-empty, add a `✍️ تنظیم رزومه` callback button with `t:{jv|jb}:{external_id}` next to the URL button. Replace the commented Phase 2/3 block with a short comment pointing to `src/bot/`.
- **`main.py`:** run the bot alongside the scheduler in `--daemon`, and add a `--bot` mode that runs only the bot.

## Deliverables

1. `src/bot/__init__.py`
2. `src/bot/callbacks.py`
3. `src/bot/client.py`
4. `src/bot/messages.py`
5. `src/bot/service.py`
6. `src/notifier/telegram.py`
7. `main.py`
8. `tests/test_bot_callbacks.py`
9. `tests/test_bot_messages.py`
10. `tests/test_bot_service.py`

## Specification

### 1. Client (`client.py`)

Use the same approach as the notifier: proxy, `trust_env=False`, retries, HTML escaping, and pacing.

**Methods:**
- `get_updates`, with an HTTP read timeout longer than the poll timeout
- `answer_callback_query`
- `send_message`
- `send_document` (multipart upload)
- `edit_message_reply_markup`

**Errors:** a 409 response from `getUpdates` raises `BotConflictError`, with a message explaining that another poller or a webhook is active.

### 2. Callbacks (`callbacks.py`)

**Format:** `"{action}:{ref}"`.
- `t` (tailor) uses `{jv|jb}:{external_id}` as its ref.
- All other actions use the tailored resume ID.

**Actions:**
- `t` tailor
- `r` regenerate
- `a` approve
- `s` reject
- `y` retry apply (Task 12)
- `c` confirm submit (Task 12)
- `x` cancel submit (Task 12)
- `n` no-op status button

**Limits:**
- `encode_callback` raises `ValueError` above 64 bytes (UTF-8).
- `decode_callback` returns `None` for malformed data.

### 3. Polling and commands

- **Offset:** persist the update offset in `bot.state_path`, written atomically (write a temp file, then rename).
- **Updates:** request `allowed_updates=["callback_query", "message"]`.
- **Private commands:**
  - `/start` and `/id` reply with the sender's numeric ID and whether they are authorized.
  - `/tailor <jv|jb>:<external_id>` (authorized users only) runs the same flow as the `t` button. This is for jobs whose notifications were sent before this task.

### 4. Authorization and idempotency

- Answer callbacks from unauthorized users with «⛔️ مجاز نیستید», take no action, and log a WARNING with their ID.
- Answer every callback within a few seconds. Run the long work as a background task, with at most one tailoring running at a time. While it runs, other taps get the toast «⏳ در صف».
- Every status change goes through `transition_status`.
- Stale buttons get the toast «این دکمه دیگر معتبر نیست».

### 5. Handlers (`service.py`)

**`t` (tailor):**
1. Require `NOTIFIED`. If the job is already `TAILORED`, resend the latest review package instead of generating a new one.
2. Change the notification keyboard to a no-op button «⏳ در حال تنظیم رزومه…».
3. Run `tailor_resume`, save the record, and render both PDFs.
4. Send the review package to `review_chat_id`.
5. Transition `NOTIFIED → TAILORED`, and change the button to «📄 رزومه برای بررسی ارسال شد».
6. On failure: send the error to the review chat, restore the keyboard, and leave the status unchanged.

**The review package** consists of:
1. `sendDocument` with the review PDF. The caption (at most 1,024 characters) contains the job title and company, the language, the version (`n/max`), and the change counts.
2. One or more messages (each at most 4,096 characters) with:
   - every rephrased bullet as «قبل ← بعد»
   - the omitted items
   - the warnings (⚠️)
   - the relevance notes
   - the cover letter
3. Buttons on the last message: `✅ تأیید و ارسال`, `🔁 تولید دوباره`, `❌ رد`.

**`r` (regenerate):** allowed only for the latest version while the version count is below `max_versions_per_job`. Runs the same flow and transitions `TAILORED → TAILORED`.

**`a` (approve):** transitions `TAILORED → APPROVED` and calls `mark_tailored_resume_approved`. Then `on_approved(...)` runs:
1. send the clean PDF, named `{Name}_Resume_{lang}.pdf` using the name from the master resume
2. send the cover letter as a message
3. send the job link, saying the user should apply manually

(Task 12 extends `on_approved` for automatic submission.)

**`s` (reject):** transitions `TAILORED → SKIPPED` and changes the buttons to «❌ رد شد».

### 6. Integration

- `run_bot` starts in `--daemon` only when `allowed_user_ids` is non-empty and `llm` is configured. Otherwise it logs an INFO line saying why it did not start.
- `--bot` runs only the bot, for setups where scraping runs from cron.
- On shutdown, stop polling, wait up to 60 s for in-flight work, then cancel it.

### 7. Tests

Use fake client, database, and tailoring components to cover:
- callbacks at the 64-byte limit, and malformed callback data
- escaping and chunking within the 1,024- and 4,096-character limits
- unauthorized callbacks being ignored
- a double tap triggering exactly one action
- stale buttons
- the maximum version limit
- the approval path sending files to the review chat and never to the channel
- offset persistence

## Acceptance Criteria

1. `uv run pytest` passes.
2. The user sends `/start` to the bot in a private chat, puts the returned ID in `TELEGRAM_ALLOWED_USER_IDS`, then runs `uv run python main.py --bot`.
3. The user runs `/tailor` on a `NOTIFIED` job, or taps the button on a new notification:
   - the review PDF and summary arrive in the private chat
   - 🔁 creates version 2
   - ✅ sends the clean PDF and the cover letter
   - ❌ on another job marks it as rejected
4. A tap from a second Telegram account is refused, and that account's ID is logged.
5. `--daemon` runs the scraper and the bot together, and Ctrl+C stops both cleanly.

## Self-Review Checklist

- Unauthorized users can trigger nothing, and resumes never reach the channel
- Every action is idempotent through compare-and-set
- Telegram's size limits (64-byte callback data, 1,024-character captions, 4,096-character messages) are respected and tested
- Graceful shutdown covers the bot

## Addendum (Task 7b): Duplicate Guard

### Specification

**New callback action `d`** ("proceed despite a duplicate"). Its ref is `{jv|jb}:{external_id}`, the same as `t`.

**Guard check:** before `t` or `/tailor` starts tailoring, load the job's duplicate group with `get_duplicate_group_members` and call `find_prior_activity`.

**If another member has activity:** do not tailor. Instead, send this message to the review chat:

> قبلاً برای آگهی مشابه «{title}» در {platform} اقدام کرده‌اید (وضعیت: {status}). ادامه می‌دهید؟

with the buttons `✍️ ادامه` (`d`) and `✖️ لغو` (`n`).

**`d`:** runs the normal tailoring flow without the guard. The usual status checks still apply.

**Review package:** for a job in a duplicate group, the caption also mentions the other postings.

### Tests

Add to `tests/test_bot_service.py`:
- the guard triggers when a group member is `TAILORED` or later
- `d` proceeds with tailoring
- there is no guard when the job has no group, or when the group has no activity

### Acceptance

After `--reevaluate` has grouped the known pairs, tailor one job of a pair. Then tap `✍️ تنظیم رزومه` on the other job: the guard message appears, and `✍️ ادامه` proceeds.