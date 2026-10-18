# Task 3 — Scraper Infrastructure & Live-Site Probe

> Use this prompt together with `00-project-contract.md`. Paste the contract above this prompt, or keep it as `CLAUDE.md` in the project root when using Claude Code.

## Goal

Build two things:
- **The shared scraper foundation:** browser context, persistent `storage_state`, Cloudflare handling, debug capture, and JSON-LD and work-mode helpers.
- **A standalone probe script:** it captures **real** JSON and HTML from both sites. Tasks 4 and 5 will be built from these captures instead of guessed selectors.

## Inputs

- Frozen from Task 1: `src/models.py`, `src/utils.py`

## Deliverables

1. `src/scrapers/__init__.py`
2. `src/scrapers/constants.py`
3. `src/scrapers/base.py`
4. `scripts/probe.py`
5. `tests/test_scraper_helpers.py`

## Specification

### 1. `constants.py`

The file has three clearly delimited sections.

**`SHARED`:**
- browser settings: a desktop Chrome User-Agent, viewport 1920×1080, locale `fa-IR`, timezone `Asia/Tehran`, and `Accept-Language: fa-IR,fa;q=0.9,en;q=0.8`
- Cloudflare challenge markers (page title plus DOM/iframe markers)
- Cloudflare wait limits: 30 s headless, 120 s headful
- runtime paths: `data/browser_state/` and `data/debug/`
- work-mode cue lists:
  - HYBRID, e.g., `هیبریدی`, `هیبرید`, `hybrid`
  - REMOTE, e.g., `دورکاری`, `دور کاری`, `remote`, `work from home`

**`JOBVISION` and `JOBINJA`:** only `BASE_URL` and `SEARCH_URL_TEMPLATE` (with a keyword placeholder, plus a page placeholder if applicable). Tasks 4 and 5 add everything else.

Mark every site-specific value, including the cue lists and Cloudflare markers, with `# VERIFY`.

### 2. `base.py`

**`create_browser_context`:** creates the context with the `SHARED` settings. It loads `storage_state` from `data/browser_state/{platform}_state.json` if that file exists.

**`save_storage_state`:** creates the directory if needed and writes the state file.

**`wait_for_cloudflare`:**
- Detects the challenge markers and waits up to the headless or headful limit. The headful limit gives the user time to solve the challenge manually.
- If the page is still blocked, it logs a WARNING recommending a headful run and raises `ScraperBlockedError`.
- It never interacts with the challenge itself.

**`save_debug_artifact`:** writes `data/debug/{platform}/{UTC timestamp}_{slug}.{ext}`. The slug is a filesystem-safe version of `name`, at most 60 characters. The function returns the path.

**`extract_json_ld_job_posting`:**
- Parses every `<script type="application/ld+json">` block with selectolax.
- Skips blocks that are not valid JSON.
- Returns the first object whose `@type` is (or contains) `JobPosting`, handling top-level lists and `@graph`.

**`detect_work_mode`:**
1. Normalizes the text with `normalize_text`.
2. Checks HYBRID cues first, then REMOTE cues.
3. Otherwise returns `ONSITE`, logging at DEBUG.

It is intended for short labels and badges, never for full descriptions (see Domain Rules).

**`BaseScraper`:**
- `__aenter__` calls `create_browser_context`.
- `__aexit__` always calls `save_storage_state` and then closes the context, even after errors.
- The constructor stores its arguments and reads `max_pages_per_keyword` from the config.
- Private convenience helpers for subclasses are allowed. For example, a debug helper that calls `save_debug_artifact` only when `debug_capture` is on.

### 3. `scripts/probe.py`

**How to run:** `uv run python -m scripts.probe ...`. The `-m` form puts the project root on `sys.path`, so `scripts/` needs no `__init__.py`.

**CLI arguments:**

| Argument | Meaning |
| --- | --- |
| `--platform {jobvision,jobinja}` | Required. |
| `--keyword TEXT` | Builds the URL from `SEARCH_URL_TEMPLATE` (page 1). Exactly one of `--keyword` or `--url` is required. |
| `--url URL` | Opens any URL; used for job detail pages. |
| `--headless` | Runs without a window. The default is headful, because the probe is also how the user passes Cloudflare manually. |
| `--wait SECONDS` | Default 15. Extra time after load, so the SPA can fire its API calls and the user can scroll or click. |

**Behavior:**
1. Launch Chromium and create the context with `create_browser_context`, which reuses any saved `storage_state`.
2. **Before** navigating, attach a response listener that captures **every** response with a JSON content type. Do not filter by URL, since the routes are unknown. Await all body reads before the page closes.
3. Navigate, call `wait_for_cloudflare`, then wait `--wait` seconds.
4. Save these artifacts with `save_debug_artifact`, always, regardless of `debug_capture`:
   - the final page HTML
   - each JSON response, wrapped as `{"request_url", "method", "request_post_data", "status", "body"}`
   - the JSON-LD `JobPosting` block, if one is found
5. Save `storage_state` and close everything, even after errors.
6. Print a summary table with index, method, status, body size, URL, and a structure hint (the top-level keys, or the list length plus the keys of the first item). Also print whether JSON-LD was found and all output paths.

**Exit codes:** 0 on success. On `ScraperBlockedError` or a navigation failure, print a clear message and exit with 1.

### 4. Tests (`tests/test_scraper_helpers.py`)

- **`extract_json_ld_job_posting`:** a single object, a list, `@graph`, an invalid block followed by a valid one, and no `JobPosting` at all (→ `None`).
- **`detect_work_mode`:**
  - Persian cues, including ZWNJ and Arabic-Yeh variants (e.g., `دور‌کاری`)
  - English cues in mixed case
  - HYBRID taking precedence when both kinds of cue appear
  - `ONSITE` when no cue is present

## Acceptance Criteria

1. `uv run pytest` passes.
2. `uv run python -m scripts.probe --platform jobinja --keyword "هوش مصنوعی"`:
   - opens the browser, and Cloudflare is passed (solve it manually if prompted)
   - creates `data/browser_state/jobinja_state.json`
   - saves the page HTML under `data/debug/jobinja/`
   - prints the summary
3. `--platform jobvision --keyword AI` captures at least one JSON response that contains the job list, identifiable from the summary.
4. `--url <job detail URL>` for one job on each site saves the HTML and, if present, the JSON-LD block.
5. A second Jobinja probe with `--headless` reuses the saved state. If Cloudflare still blocks it, the probe exits with a clear message instead of a traceback.

## Self-Review Checklist

- `constants.py` has the three sections, and every site-specific value is marked `# VERIFY`
- `storage_state` is loaded when present and always saved on exit, even after errors
- `wait_for_cloudflare` uses separate headless and headful limits, raises `ScraperBlockedError`, and never touches the challenge
- The probe captures all JSON responses, including POST bodies, without losing any, and saves HTML, JSON-LD, and `storage_state`
- `extract_json_ld_job_posting` and `detect_work_mode` are pure functions and fully tested
