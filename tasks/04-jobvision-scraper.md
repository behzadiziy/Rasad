# Task 4 — JobVision Scraper

> Use this prompt together with `00-project-contract.md`. Paste the contract above this prompt, or keep it as `CLAUDE.md` in the project root when using Claude Code.

## Goal

Implement the JobVision scraper from **real samples** captured with the Task 3 probe. JobVision is an Angular SPA. The primary strategy is intercepting its internal search API, with DOM parsing as the fallback.

## Inputs

**Frozen files:** `src/models.py`, `src/utils.py`, `src/scrapers/base.py`, and the current `src/scrapers/constants.py`.

**Samples** from `data/debug/jobvision/`. Trim long samples, but keep their structure intact:

| ID | Sample | Required |
| --- | --- | --- |
| S1 | The captured JSON response that contains the job list, with its request URL, method, and POST body. Trim it to 2–3 complete job items, and keep any pagination or total-count metadata. Include a remote job if one exists. | Yes |
| S2 | For at least two searches (another keyword, or page 2 of the same search): the page URL you opened, plus the matching API request URL and POST body | Yes |
| S3 | One job detail page: the JSON-LD block if present, plus the HTML of the description section | Yes |
| S4 | The HTML of the search-results list with 2–3 job cards, for the DOM fallback | Optional |

## Deliverables

1. `src/scrapers/constants.py` (the full file; only the `JOBVISION` section changes)
2. `src/scrapers/jobvision.py`
3. `tests/fixtures/jobvision/search_response.json` (from S1)
4. `tests/fixtures/jobvision/detail.html` (from S3)
5. `tests/fixtures/jobvision/search_page.html` (from S4, only if provided)
6. `tests/test_jobvision_parser.py`

## Specification

### 1. Constants (`JOBVISION` section)

Derive these values from the samples:
- the search page URL template and its pagination parameter
- the API route pattern, matched against response URLs
- the JSON path to the job list
- field paths for: ID, title, company, city, work mode or remote flag, salary, and description (if present)
- the job URL, or a URL template built from the ID
- DOM selectors for the fallback and for the detail-page description

Mark each value `# Source: S<n>` or `# VERIFY`.

### 2. Pure parsing functions

**`parse_search_payload`:**
- Reads the payload through the constants.
- Maps the work mode from a structured flag or label if one exists; otherwise uses `detect_work_mode` on short label fields only.
- Skips and logs malformed items. Never raises because of a bad item.

**`parse_detail_html`:**
- Uses the JSON-LD `description` first, then the DOM selector.
- Converts any HTML to plain text (selectolax, `text(separator="\n")`) and collapses blank lines.
- Returns the text, or `None`.

**DOM fallback:** if S4 is provided, add a private `_parse_search_dom(html) -> list[JobPost]`.

### 3. `JobVisionScraper.fetch_jobs`

For each keyword, and each page up to `max_pages_per_keyword`:

1. Register a fresh `page.on("response", handler)` **before** navigating, and remove it afterwards. The handler filters responses by the API route pattern and a JSON content type, and schedules a task to read each matching body.
2. Navigate and call `wait_for_cloudflare`. Then wait until a matching response arrives, or until a 20 s timeout passes.
3. `await asyncio.gather(...)` all pending body tasks before leaving the page.
4. Parse the bodies with `parse_search_payload`.
5. If no matching response was captured, fall back to DOM parsing of `await page.content()`, and log a WARNING that the API pattern needs verification.
6. Stop paginating when a page returns no jobs.
7. For each job, skip it if `is_known` returns `True`. If its description is missing, open the detail page and use `parse_detail_html`.
8. In debug-capture mode, save every intercepted JSON body and every detail HTML.

Call `human_delay()` between navigations, and deduplicate by `external_id` within the run.

### 4. Smoke run (`__main__`)

`uv run python -m src.scrapers.jobvision --keyword AI [--pages 1] [--headful] [--debug-capture]`

- Runs the scraper with an `is_known` that always returns `False`.
- `--pages` overrides `max_pages_per_keyword` via `model_copy`.
- Prints a table of ID, title, company, city, work mode, and description length.
- Writes nothing to the database.

### 5. Tests (`tests/test_jobvision_parser.py`)

- `parse_search_payload` on the fixture returns the expected number of jobs. Each job has a non-empty `external_id`, `title`, and `url`, and `platform == "jobvision"`. The work mode is correct for the remote sample, if one is included.
- A payload containing one malformed item still parses the valid items.
- `parse_detail_html` on the fixture returns non-empty text with no HTML tags.
- The DOM fallback is tested on `search_page.html`, if that fixture exists.

## Acceptance Criteria

1. `uv run pytest` passes.
2. `uv run python -m src.scrapers.jobvision --keyword AI --pages 1` prints real jobs with titles, companies, cities, and non-zero description lengths.
3. If S4 was provided: temporarily breaking the API route pattern in `constants.py` makes the smoke run log the fallback WARNING and still return jobs via the DOM.

## Self-Review Checklist

- Every `JOBVISION` constant marked `# Source: S<n>` or `# VERIFY`; other sections unchanged
- The listener is registered before navigation, and every body task is awaited before leaving the page
- Defensive parsing: malformed items are skipped, and the run never crashes on bad data
- The DOM fallback triggers when no API response is captured and logs a WARNING
- `is_known` is checked before every detail-page visit; JSON-LD is preferred on detail pages
- Work mode is never inferred from description text
- Parsers are pure and tested against the real fixtures
