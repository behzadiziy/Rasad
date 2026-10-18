# Task 11 — Login and Apply-Flow Probe

## Goal

Capture the real application flow on both sites before writing any automation, using the same approach as Tasks 3–5. The user logs in manually; nothing is clicked or submitted by code.

## Deliverables

1. `scripts/apply_probe.py`
2. `tests/test_apply_probe_redaction.py`
3. `docs/apply_flows.md` (written after the user's captures)

## Specification

### 1. Login mode

`--platform X --login`:
1. Open a visible browser at the site.
2. The user logs in manually, then presses Enter in the terminal.
3. Save `data/browser_state/{platform}_auth_state.json`, and print only the cookie count, never values.

### 2. Capture mode

`--platform X --job-url URL` opens a visible browser with the auth state.

**Network:** record every request and response (method, URL, status). Redact them before saving:
- Headers: drop `Cookie`, `Set-Cookie`, `Authorization`, and any header whose name contains `token` or `csrf`.
- Request bodies: keep field names but replace values with `<redacted len=N>`. Replace multipart files with `<file name, size, type>`.

**Snapshots:** save a snapshot (HTML plus a screenshot) on every navigation, and whenever the user types `s` and presses Enter. Typing `q` finishes the capture.

**Output:** save everything under `data/debug/{platform}/apply/{timestamp}/`, with an `index.json` timeline.

**Never** click or submit anything from code.

### 3. Pause for the user

After implementing the script, stop and give the user exact instructions:
1. Run login mode for each platform.
2. On each site, pick one job you genuinely want, run capture mode, and go through the application manually up to and including the final submit. That single application is real.
3. Press `s` at each step, and `q` to finish.
4. If you prefer not to submit, stop before the final submit. The final request will then be marked `# VERIFY`.

### 4. Write `docs/apply_flows.md`

After the captures, write this section for each platform:
- **Entry and steps:** the entry point and apply button, and each step or screen
- **Fields:** required and optional fields, their type, and whether they are prefilled from the profile
- **Resume choice:** **whether the profile resume or an uploaded file is sent, and whether a tailored PDF can be attached**, including file limits
- **Extra inputs:** the cover letter or message field, and any screening questions
- **Final request:** the method, endpoint, and payload field names
- **Signals:** the success signal, the "already applied" indicator, the session-expired signal, and whether a CAPTCHA appears
- **Open questions**

Mark every item `Source: <capture file>` or `VERIFY`.

### 5. Tests

Test the redaction and multipart summarization as pure functions.

## Acceptance Criteria

1. `uv run pytest` passes.
2. Both `*_auth_state.json` files exist.
3. There is one capture per platform.
4. `docs/apply_flows.md` is complete, and the user has reviewed it, especially the "Resume choice" section.

## Self-Review Checklist

- Code never clicks or submits anything
- No secrets or personal values remain in saved captures
- Every item in the document has a source, or is marked VERIFY