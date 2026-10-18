# Task 12 — Application Automation

## Goal

After the user approves a tailored resume, submit the application on the job board. Verify everything in code before submitting, send a receipt, and never act without approval.

## Inputs

- `docs/apply_flows.md` (reviewed by the user)
- the Task 11 captures
- the `*_auth_state.json` files

## Authorized Changes

- `src/bot/service.py`: extend `on_approved` for automatic submission, and add handlers for `y`, `c`, and `x`.

## Deliverables

1. `src/apply/__init__.py`
2. `src/apply/constants.py`
3. `src/apply/base.py`
4. `src/apply/jobvision.py`
5. `src/apply/jobinja.py`
6. `src/bot/service.py`
7. `tests/fixtures/apply/` (redacted form HTML taken from the captures)
8. `tests/test_apply_checks.py`
9. `tests/test_apply_parsers.py`
10. `tests/test_bot_apply_flow.py`

## Specification

### 1. Constants

All URLs, selectors, and signals from `docs/apply_flows.md` go in `src/apply/constants.py`, each marked `Source` or `VERIFY`.

### 2. `BaseApplier.apply`

Use the auth state (never the scraping state), headless by default. The steps run in this order:

1. **Preconditions (in code):**
   - `apply.enabled` is true
   - the job is `APPROVED`
   - the resume is approved and belongs to this job
   - `has_submitted_application` is false
   - the number of applications since local midnight (in the configured timezone) is below `daily_limit`
2. **Open the job page.** If the session is logged out, raise `ApplyAuthError`. If the site shows "already applied", record it and transition the job to `APPLIED`.
3. **Fill the form** according to the platform mapping. Attach the clean PDF where the document says this is possible. Put the cover letter in the message field, if there is one.
4. **Pre-submit checks:**
   - every required field is filled
   - the file input holds the expected file name
   - there are no unknown required fields or questions (otherwise abort, listing their labels)
   - there is no CAPTCHA (otherwise abort with "apply manually")

   Then take a screenshot of the filled form.
5. **Optional confirmation.** If `confirm_before_submit` is on, send the screenshot to the review chat with `✅ ارسال نهایی` (`c`) and `✖️ لغو` (`x`). Wait up to `confirm_timeout_minutes`. On a timeout, cancel the submission and leave the status `APPROVED`.
6. **Submit** (unless `dry_run`):
   1. Wait for the success signal.
   2. Take a receipt screenshot.
   3. Call `record_application(submitted=True)`.
   4. Transition `APPROVED → APPLIED`.
   5. Send the receipt to the review chat.
7. **On any failure:**
   1. Call `record_application(submitted=False, error=...)`.
   2. Transition `APPROVED → APPLY_FAILED`.
   3. Send the error and the last screenshot, with `🔁 تلاش دوباره` (`y`) and `❌ رد` (`s`).

Never retry automatically.

**Expired session:** on `ApplyAuthError`, tell the user to run `uv run python -m scripts.apply_probe --platform X --login`.

### 3. Bot integration

- When `apply.enabled` is on, `a` runs the applier in the background, with at most one application at a time.
- `y` transitions `APPLY_FAILED → APPROVED` and applies again.
- `c` and `x` resolve the pending confirmation.

### 4. CLI dry run

`uv run python -m src.apply.<platform> --resume-id N --dry-run [--headful]` fills the form, runs the checks, and saves a screenshot. It never submits.

### 5. Tests

Using redacted fixtures and fakes, cover:
- every precondition
- the pre-submit checks on the fixture forms
- detection of the success and "already applied" signals
- the bot flow:
  - approve calls the applier once, and a double tap still calls it once
  - the daily limit refuses a submission with a message
  - confirm, cancel, and timeout

## Acceptance Criteria

1. `uv run pytest` passes.
2. The user runs a dry run with `--headful` on one approved job per platform. The form is filled correctly, the checks pass, a screenshot is saved, and nothing is submitted.
3. The user sets `apply.enabled: true` (and `confirm_before_submit: true` for the first real application), then approves a job they really want:
   - the receipt arrives
   - the status becomes `APPLIED`
   - a second ✅ tap does nothing
4. With `daily_limit: 1`, a second approval on the same day is refused with a message.

## Self-Review Checklist

- Nothing is submitted without approval for that specific job, and nothing beyond the daily limit
- Any unexpected state aborts the submission and notifies the user; there are no automatic retries
- The auth state is separate from the scraping state, and CAPTCHAs are never touched
- Every application is recorded, with a screenshot


## Addendum (Task 7b): Duplicate Guard

### Specification

**Guard check:** before any application, in `on_approved` and on a `y` retry, call `has_submitted_application` for every other member of the job's duplicate group.

**If one of them was submitted:** never submit automatically. Force the confirmation step (step 5 of §2) for this job, even when `confirm_before_submit` is off. The message starts with:

> ⚠️ برای آگهی مشابه «{title}» در {platform} در تاریخ {date} درخواست ارسال شده است.

The user decides with `c` (submit) or `x` (cancel). The daily limit still applies.

### Tests

Add to `tests/test_bot_apply_flow.py`:
- a submitted application in the group forces confirmation
- `x` cancels the submission, and the status stays `APPROVED`
- there is no forced confirmation when no other member was submitted

### Acceptance

When an application has already been submitted for one job of a duplicate pair, approving the other job shows the warning and the confirmation buttons instead of submitting.