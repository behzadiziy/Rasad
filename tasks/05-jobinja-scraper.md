# Task 5 — Jobinja Scraper

> Use this prompt together with `00-project-contract.md`. Paste the contract above this prompt, or keep it as `CLAUDE.md` in the project root when using Claude Code.

## Goal

Implement the Jobinja scraper from **real samples** captured with the Task 3 probe. Jobinja serves server-side-rendered HTML behind Cloudflare.

## Inputs

**Frozen files:** `src/models.py`, `src/utils.py`, `src/scrapers/base.py`, and the current `src/scrapers/constants.py` (including the `JOBVISION` section from Task 4, if that task is done).

**Samples** from `data/debug/jobinja/`. Trim long samples, but keep their structure intact:

| ID | Sample | Required |
| --- | --- | --- |
| S1 | The search results page HTML, trimmed to the job-list container with 2–3 complete job cards (include a remote job if available), plus the search URL used | Yes |
| S2 | The URL of page 2 of the same search | Yes |
| S3 | One job detail page: the JSON-LD block if present, plus the HTML of the description section and the location/work-type section | Yes |
| S4 | 2–3 full job URLs | Yes |
| S5 | The HTML of a search page with no results | Optional |

## Deliverables

1. `src/scrapers/constants.py` (the full file; only the `JOBINJA` section changes)
2. `src/scrapers/jobinja.py`
3. `tests/fixtures/jobinja/search_page.html` (from S1)
4. `tests/fixtures/jobinja/detail.html` (from S3)
5. `tests/fixtures/jobinja/empty_search.html` (from S5, only if provided)
6. `tests/test_jobinja_parser.py`

## Specification

### 1. Constants (`JOBINJA` section)

Derive these values from the samples:
- the search URL template with keyword and page parameters
- the job-list container selector
- card selectors for title, company, city, link, and work-type badge (if any)
- the external-ID regex for job URLs
- detail-page selectors for the description and the location/work-type field

Mark each value `# Source: S<n>` or `# VERIFY`.

### 2. Pure parsing functions

- **`extract_external_id`:** applies the regex from the constants and returns `None` for URLs that don't match.
- **`parse_search_html`:** parses the cards. The city comes from its selector, and the work mode from the badge via `detect_work_mode`. Cards without a valid link or ID are skipped and logged.
- **`parse_detail_html`:** uses the JSON-LD `description` first, then the DOM selector, and returns plain text (as in Task 4).
- **`parse_detail_work_mode(html: str) -> WorkMode | None`:** a new public helper. It reads only the dedicated work-type field on the detail page, and returns `None` when that field is absent.

### 3. `JobinjaScraper.fetch_jobs`

For each keyword, and each page up to `max_pages_per_keyword`:

1. Navigate to the search URL and call `wait_for_cloudflare`. Let `ScraperBlockedError` propagate, so the orchestrator skips the platform.
2. Wait for the job-list container. If the page loaded and is not blocked but the container is missing, treat it as "no results".
3. Parse the cards, and stop paginating when a page has no cards.
4. For each card, `await is_known("jobinja", external_id)` first. Only for unknown jobs, visit the detail page to set `raw_description`. If the card gave no work-mode badge, use `parse_detail_work_mode` when it returns a value.
5. In debug-capture mode, save every search page and detail page HTML.

Call `human_delay()` between navigations, and deduplicate by `external_id` within the run.

### 4. Smoke run (`__main__`)

`uv run python -m src.scrapers.jobinja --keyword "هوش مصنوعی" [--pages 1] [--headful] [--debug-capture]`

This behaves like the Task 4 smoke run. If Cloudflare blocks it, it prints a clear message recommending `--headful` and exits with code 1, with no traceback.

### 5. Tests (`tests/test_jobinja_parser.py`)

- `extract_external_id` works on the S4 URLs and returns `None` for an unrelated URL.
- `parse_search_html` on the fixture returns the expected number of jobs, with non-empty fields and `platform == "jobinja"`. Each job's ID equals `extract_external_id(job.url)`.
- A card with its link removed is skipped.
- `parse_detail_html` returns non-empty text with no HTML tags.
- `parse_detail_work_mode` returns the expected value for the fixture.
- `parse_search_html` returns `[]` on `empty_search.html`, if that fixture exists.

## Acceptance Criteria

1. `uv run pytest` passes.
2. After a headful probe run, `uv run python -m src.scrapers.jobinja --keyword "هوش مصنوعی" --pages 1` (headless) prints real jobs with descriptions. If Cloudflare blocks it, the message is clear and there is no traceback.
3. Adding `--debug-capture` saves the search and detail HTML under `data/debug/jobinja/`.

## Self-Review Checklist

- Every `JOBINJA` constant marked `# Source: S<n>` or `# VERIFY`; other sections unchanged
- `ScraperBlockedError` propagates; a missing container is treated as "no results"
- `is_known` is checked before every detail-page visit; JSON-LD is preferred on detail pages
- Work mode comes from badges or the dedicated work-type field only, never from description text
- Pagination stops on an empty page; results are deduplicated within the run
- Parsers are pure and tested against the real fixtures
