"""JobVision (jobvision.ir) scraper.

JobVision is an Angular SPA. Each search page calls an internal job-list API;
the scraper intercepts that response and parses it with `parse_search_payload`.
When no API response is captured, it falls back to parsing the rendered job
cards. Descriptions come from detail pages (the DOM first, then JSON-LD), which
are visited only for jobs that `is_known` reports as new.

Every site-specific value lives in `src.scrapers.constants` (`JOBVISION_*`).

Smoke run (prints jobs, writes nothing to the database):
    uv run python -m src.scrapers.jobvision --keyword AI [--pages 1] [--headful] [--debug-capture]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import quote, urlsplit

from loguru import logger
from playwright.async_api import Page, Response, async_playwright
from playwright.async_api import Error as PlaywrightError
from selectolax.lexbor import LexborHTMLParser, LexborNode

from src.models import AppConfig, JobPost, Platform, WorkMode, load_app_config
from src.scrapers import constants
from src.scrapers.base import BaseScraper, ScraperBlockedError, detect_work_mode, extract_json_ld_job_posting
from src.utils import human_delay

_PLATFORM: Platform = "jobvision"
_API_ROUTE_RE = re.compile(constants.JOBVISION_API_ROUTE_PATTERN, re.IGNORECASE)
_CARD_ID_RE = re.compile(constants.JOBVISION_CARD_ID_PATTERN)
_JSON_LD_SELECTOR = 'script[type="application/ld+json"]'
_LABEL_JOINER = " | "
_SPACE_BEFORE_COMMA_RE = re.compile(r"\s+([،,])")

JsonPath = tuple[str, ...]
_DebugSaver = Callable[[str, str | bytes, str], Path | None]


# ── Pure helpers ───────────────────────────────────────────────────────────


def _get_path(data: Any, path: JsonPath) -> Any:
    """Walk dict keys along `path`; return None as soon as a step is missing or not a dict."""
    current = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _text_at(data: Any, path: JsonPath) -> str:
    """Return the stripped string at `path`, or "" when it is missing or not a string."""
    value = _get_path(data, path)
    return value.strip() if isinstance(value, str) else ""


def _first_text(data: Any, paths: Sequence[JsonPath]) -> str:
    """Return the first non-empty string found along `paths`, or ""."""
    for path in paths:
        text = _text_at(data, path)
        if text:
            return text
    return ""


def _external_id(value: Any) -> str:
    """Convert an API ID (int or string) to a string; anything else becomes ""."""
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    return ""


def _job_url(external_id: str) -> str:
    """Build the job's detail URL from its ID."""
    return constants.JOBVISION_JOB_URL_TEMPLATE.format(id=quote(external_id, safe=""))


def _clean_inline(text: str) -> str:
    """Collapse all whitespace runs in a short label into single spaces."""
    return " ".join(text.split())


def _node_to_text(node: LexborNode) -> str | None:
    """Return a node's text, one text run per line, with blank lines removed; None when empty."""
    lines = (line.strip() for line in node.text(separator="\n").splitlines())
    text = "\n".join(line for line in lines if line)
    return text or None


def _html_to_text(html: str) -> str | None:
    """Convert an HTML fragment (or plain text) to plain text; None when nothing remains."""
    tree = LexborHTMLParser(html)
    root = tree.body if tree.body is not None else tree.root
    if root is None:
        return None
    return _node_to_text(root)


def _api_work_mode(item: dict[str, Any]) -> WorkMode:
    """Map an API item's remote flag to a work mode; fall back to its short labels.

    Only the structured `isRemote` flag and the badge labels are used, never
    the description.
    """
    flag = _get_path(item, constants.JOBVISION_FIELD_REMOTE_FLAG)
    if isinstance(flag, bool):
        return WorkMode.REMOTE if flag else WorkMode.ONSITE
    labels = _get_path(item, constants.JOBVISION_FIELD_LABELS)
    texts = [label for label in labels if isinstance(label, str)] if isinstance(labels, list) else []
    return detect_work_mode(_LABEL_JOINER.join(texts))


def _api_city(item: dict[str, Any]) -> str:
    """Join the city and region names; use the province only when no city is given."""
    parts = [_text_at(item, path) for path in constants.JOBVISION_FIELD_CITY_PARTS]
    city = constants.JOBVISION_CITY_SEPARATOR.join(part for part in parts if part)
    return city or _text_at(item, constants.JOBVISION_FIELD_CITY_FALLBACK)


def _parse_item(item: Any) -> JobPost:
    """Build a `JobPost` from one API job item.

    Raises:
        ValueError: If the item is not an object or lacks an ID or title
            (pydantic's `ValidationError` is a `ValueError` too).
    """
    if not isinstance(item, dict):
        raise ValueError(f"job item is {type(item).__name__}, not an object")
    external_id = _external_id(_get_path(item, constants.JOBVISION_FIELD_ID))
    if not external_id:
        raise ValueError("job item has no ID")
    title = _text_at(item, constants.JOBVISION_FIELD_TITLE)
    if not title:
        raise ValueError(f"job item {external_id} has no title")
    description: str | None = None
    if constants.JOBVISION_FIELD_DESCRIPTION is not None:
        raw = _text_at(item, constants.JOBVISION_FIELD_DESCRIPTION)
        description = _html_to_text(raw) if raw else None
    return JobPost(
        platform=_PLATFORM,
        external_id=external_id,
        title=title,
        company=_first_text(item, constants.JOBVISION_FIELD_COMPANY),
        city=_api_city(item),
        work_mode=_api_work_mode(item),
        salary=_text_at(item, constants.JOBVISION_FIELD_SALARY) or None,
        url=_job_url(external_id),
        raw_description=description,
    )


def parse_search_payload(payload: Any) -> list[JobPost]:
    """Parse a job-list API response body into `JobPost`s.

    The job list is read from `JOBVISION_JOB_LIST_PATH`. Malformed items are
    logged and skipped; a payload without a job list yields an empty list.
    This function never raises because of bad data.
    """
    items = _get_path(payload, constants.JOBVISION_JOB_LIST_PATH)
    if not isinstance(items, list):
        logger.warning(
            "[jobvision] No job list at {} in the API payload (got {}); verify JOBVISION_JOB_LIST_PATH",
            "/".join(constants.JOBVISION_JOB_LIST_PATH),
            type(items).__name__,
        )
        return []
    jobs: list[JobPost] = []
    for index, item in enumerate(items):
        try:
            jobs.append(_parse_item(item))
        except (ValueError, TypeError) as exc:
            logger.warning("[jobvision] Skipping malformed API job item #{}: {}", index, exc)
    logger.debug("[jobvision] Parsed {} of {} API job item(s)", len(jobs), len(items))
    return jobs


def parse_detail_html(html: str) -> str | None:
    """Return a detail page's job description as plain text, or None.

    The `JOBVISION_DETAIL_DESCRIPTION_SELECTOR` element is preferred; the
    JSON-LD `JobPosting.description` is the fallback. JobVision's JSON-LD drops
    line breaks without adding spaces («defined toneRequirements:»), which fuses
    words and breaks word-boundary keyword matching. HTML is converted to text
    one text run per line, with blank lines removed.
    """
    node = LexborHTMLParser(html).css_first(constants.JOBVISION_DETAIL_DESCRIPTION_SELECTOR)
    if node is not None:
        text = _node_to_text(node)
        if text:
            return text
    logger.debug("[jobvision] No description element; trying JSON-LD")
    job_posting = extract_json_ld_job_posting(html)
    if job_posting is None:
        return None
    description = job_posting.get("description")
    if not isinstance(description, str) or not description.strip():
        return None
    return _html_to_text(description)


def _card_text(card: LexborNode, selector: str) -> str:
    """Return the inline text of the first match of `selector` in a card, or ""."""
    node = card.css_first(selector)
    return _clean_inline(node.text()) if node is not None else ""


def _parse_card(card: LexborNode) -> JobPost:
    """Build a `JobPost` from one rendered job card.

    Raises:
        ValueError: If the card has no job link, ID, or title.
    """
    link = card.css_first(constants.JOBVISION_CARD_LINK_SELECTOR)
    href = link.attributes.get("href") if link is not None else None
    if not href:
        raise ValueError("card has no job link")
    match = _CARD_ID_RE.search(urlsplit(href).path)
    if match is None:
        raise ValueError(f"no job ID in card link {href!r}")
    external_id = match.group(1)
    title = _card_text(card, constants.JOBVISION_CARD_TITLE_SELECTOR)
    if not title:
        raise ValueError(f"card {external_id} has no title")
    if card.css_first(constants.JOBVISION_CARD_REMOTE_SELECTOR) is not None:
        work_mode = WorkMode.REMOTE
    else:
        labels = [_clean_inline(node.text()) for node in card.css(constants.JOBVISION_CARD_LABEL_SELECTOR)]
        work_mode = detect_work_mode(_LABEL_JOINER.join(label for label in labels if label))
    return JobPost(
        platform=_PLATFORM,
        external_id=external_id,
        title=title,
        company=_card_text(card, constants.JOBVISION_CARD_COMPANY_SELECTOR),
        city=_SPACE_BEFORE_COMMA_RE.sub(r"\1", _card_text(card, constants.JOBVISION_CARD_CITY_SELECTOR)),
        work_mode=work_mode,
        salary=_card_text(card, constants.JOBVISION_CARD_SALARY_SELECTOR) or None,
        url=_job_url(external_id),
    )


def _parse_search_dom(html: str) -> list[JobPost]:
    """Parse the rendered search-results page (DOM fallback); malformed cards are skipped."""
    cards = LexborHTMLParser(html).css(constants.JOBVISION_CARD_SELECTOR)
    jobs: list[JobPost] = []
    for index, card in enumerate(cards):
        try:
            jobs.append(_parse_card(card))
        except ValueError as exc:
            logger.warning("[jobvision] Skipping malformed job card #{}: {}", index, exc)
    logger.debug("[jobvision] Parsed {} of {} job card(s) from the DOM", len(jobs), len(cards))
    return jobs


def _requested_page(post_data: str | None) -> int | None:
    """Return the page number in a job-list request body, or None when it is absent or unreadable."""
    if not post_data:
        return None
    try:
        body = json.loads(post_data)
    except json.JSONDecodeError:
        return None
    value = body.get(constants.JOBVISION_API_PAGE_FIELD) if isinstance(body, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# ── API interception ───────────────────────────────────────────────────────


class _ApiCapture:
    """Collects job-list API bodies for one search page.

    `on_response` filters by route and content type and schedules a body read
    for each match; `drain` awaits every scheduled read and returns the parsed
    bodies. Each read is bounded by `JOBVISION_BODY_READ_TIMEOUT_S`.
    """

    def __init__(self, page_no: int, save_debug: _DebugSaver) -> None:
        """Start with no matches for the given 1-based page number."""
        self.arrived = asyncio.Event()
        self._page_no = page_no
        self._save_debug = save_debug
        self._tasks: list[asyncio.Task[None]] = []
        self._bodies: list[Any] = []

    def on_response(self, response: Response) -> None:
        """Response listener: schedule a body read for matching job-list responses."""
        if not _API_ROUTE_RE.search(response.url):
            return
        if "json" not in response.headers.get("content-type", "").lower():
            logger.debug("[jobvision] Ignoring non-JSON response from {}", response.url)
            return
        try:
            requested = _requested_page(response.request.post_data)
        except UnicodeDecodeError:
            requested = None
        if requested is not None and requested != self._page_no:
            logger.debug("[jobvision] Ignoring job-list response for page {} (want {})", requested, self._page_no)
            return
        logger.debug("[jobvision] Captured job-list response {} {}", response.status, response.url)
        self._tasks.append(asyncio.create_task(self._read(response)))
        self.arrived.set()

    async def _read(self, response: Response) -> None:
        """Read, save (in debug mode), and parse one body; failures are logged, not raised."""
        try:
            raw = await asyncio.wait_for(response.body(), constants.JOBVISION_BODY_READ_TIMEOUT_S)
        except TimeoutError:
            logger.warning("[jobvision] Timed out reading the body of {}", response.url)
            return
        except PlaywrightError as exc:
            logger.warning("[jobvision] Could not read the body of {}: {}", response.url, exc)
            return
        self._save_debug(f"search_api_p{self._page_no}", raw, "json")
        try:
            self._bodies.append(json.loads(raw))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("[jobvision] Job-list response from {} is not valid JSON: {}", response.url, exc)

    async def drain(self) -> list[Any]:
        """Await all scheduled body reads (including ones added meanwhile) and return the bodies."""
        while pending := [task for task in self._tasks if not task.done()]:
            await asyncio.gather(*pending, return_exceptions=True)
        return list(self._bodies)


# ── Scraper ────────────────────────────────────────────────────────────────


class JobVisionScraper(BaseScraper):
    """Scrapes JobVision search results via its job-list API, with a DOM fallback."""

    platform: ClassVar[Platform] = _PLATFORM

    async def fetch_jobs(self, keywords: list[str]) -> list[JobPost]:
        """Scrape every keyword, page by page, and return the new jobs found.

        Keywords and pages are processed sequentially with `human_delay()`
        between navigations. Pagination stops at the first page without jobs
        or at `max_pages_per_keyword`. Jobs are deduplicated by `external_id`;
        known jobs are skipped without visiting their detail page.

        Raises:
            ScraperBlockedError: If a page stays behind a Cloudflare challenge.
        """
        logger.info("[jobvision] Starting: {} keyword(s), up to {} page(s) each", len(keywords), self._max_pages)
        page = await self._new_page()
        jobs: list[JobPost] = []
        seen: set[str] = set()
        navigations = 0
        try:
            for keyword in keywords:
                for page_no in range(1, self._max_pages + 1):
                    if navigations:
                        await human_delay()
                    navigations += 1
                    listing = await self._scrape_search_page(page, keyword, page_no)
                    if listing is None:
                        break
                    if not listing:
                        logger.info("[jobvision] {!r} page {}: no jobs; stopping pagination", keyword, page_no)
                        break
                    new_count = 0
                    for job in listing:
                        if job.external_id in seen:
                            continue
                        seen.add(job.external_id)
                        if await self._known(job.external_id):
                            logger.debug("[jobvision] Skipping known job {}", job.external_id)
                            continue
                        if job.raw_description is None:
                            await human_delay()
                            navigations += 1
                            description = await self._scrape_description(page, job)
                            if description is not None:
                                job = job.model_copy(update={"raw_description": description})
                        jobs.append(job)
                        new_count += 1
                    logger.info(
                        "[jobvision] {!r} page {}: {} job(s), {} new", keyword, page_no, len(listing), new_count
                    )
        finally:
            try:
                await page.close()
            except PlaywrightError as exc:
                logger.debug("[jobvision] Failed to close page: {}", exc)
        logger.info("[jobvision] Finished: {} new job(s)", len(jobs))
        return jobs

    async def _scrape_search_page(self, page: Page, keyword: str, page_no: int) -> list[JobPost] | None:
        """Load one search page and return its jobs; None when navigation fails.

        The API listener is attached before navigating and removed afterwards,
        and every body read is awaited before the page is left. Without a
        captured API response, the rendered cards are parsed instead.
        """
        url = constants.JOBVISION_SEARCH_URL_TEMPLATE.format(keyword=quote(keyword, safe=""), page=page_no)
        capture = _ApiCapture(page_no, self._debug)
        page.on("response", capture.on_response)
        try:
            logger.info("[jobvision] Opening search {!r} page {}: {}", keyword, page_no, url)
            try:
                await page.goto(url, wait_until="domcontentloaded")
            except PlaywrightError as exc:
                logger.warning("[jobvision] Navigation to {} failed: {}", url, str(exc).splitlines()[0])
                return None
            await self._wait_for_cloudflare(page)
            try:
                await asyncio.wait_for(capture.arrived.wait(), constants.JOBVISION_API_WAIT_S)
            except TimeoutError:
                logger.debug("[jobvision] No job-list response within {:.0f}s", constants.JOBVISION_API_WAIT_S)
            bodies = await capture.drain()
        finally:
            page.remove_listener("response", capture.on_response)
            await capture.drain()

        if bodies:
            listing: list[JobPost] = []
            for body in bodies:
                listing.extend(parse_search_payload(body))
            return listing

        logger.warning(
            "[jobvision] No API response matched JOBVISION_API_ROUTE_PATTERN on {}; the pattern needs "
            "verification in src/scrapers/constants.py. Falling back to DOM parsing",
            url,
        )
        try:
            html = await page.content()
        except PlaywrightError as exc:
            logger.warning("[jobvision] Could not read the search page HTML: {}", exc)
            return None
        self._debug(f"search_dom_{keyword}_p{page_no}", html, "html")
        return _parse_search_dom(html)

    async def _scrape_description(self, page: Page, job: JobPost) -> str | None:
        """Open a job's detail page and return its description; None on failure."""
        logger.debug("[jobvision] Opening detail page {}", job.url)
        try:
            await page.goto(job.url, wait_until="domcontentloaded")
        except PlaywrightError as exc:
            logger.warning("[jobvision] Detail page {} failed to load: {}", job.url, str(exc).splitlines()[0])
            return None
        await self._wait_for_cloudflare(page)
        selector = f"{constants.JOBVISION_DETAIL_DESCRIPTION_SELECTOR}, {_JSON_LD_SELECTOR}"
        try:
            await page.locator(selector).first.wait_for(
                state="attached", timeout=constants.JOBVISION_DETAIL_WAIT_TIMEOUT_MS
            )
        except PlaywrightError:
            logger.debug("[jobvision] No description element or JSON-LD on {} yet", job.url)
        try:
            html = await page.content()
        except PlaywrightError as exc:
            logger.warning("[jobvision] Could not read detail page {}: {}", job.url, exc)
            return None
        self._debug(f"detail_{job.external_id}", html, "html")
        description = parse_detail_html(html)
        if description is None:
            logger.warning(
                "[jobvision] No description on {}; verify JOBVISION_DETAIL_DESCRIPTION_SELECTOR", job.url
            )
        return description


# ── Smoke run ──────────────────────────────────────────────────────────────


def _with_max_pages(config: AppConfig, pages: int | None) -> AppConfig:
    """Return the config with `max_pages_per_keyword` overridden when `pages` is given."""
    if pages is None:
        return config
    scraping = config.scraping.model_copy(update={"max_pages_per_keyword": pages})
    return config.model_copy(update={"scraping": scraping})


async def _never_known(platform: str, external_id: str) -> bool:
    """`is_known` for smoke runs: every job is treated as new."""
    return False


async def _smoke_run(config: AppConfig, keywords: list[str], *, headful: bool, debug_capture: bool) -> list[JobPost]:
    """Launch Chromium, run the scraper once, and return its jobs."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not headful)
        try:
            async with JobVisionScraper(
                browser, config, _never_known, headful=headful, debug_capture=debug_capture
            ) as scraper:
                return await scraper.fetch_jobs(keywords)
        finally:
            await browser.close()


def _shorten(text: str, width: int) -> str:
    """Truncate text to `width` characters with an ellipsis."""
    return text if len(text) <= width else text[: width - 1] + "…"


def _print_table(jobs: list[JobPost]) -> None:
    """Print ID, title, company, city, work mode, and description length for each job."""
    print(f"\n{len(jobs)} job(s)")
    print(f"{'ID':<9} {'TITLE':<40} {'COMPANY':<28} {'CITY':<22} {'MODE':<7} {'DESC':>6}")
    for job in jobs:
        print(
            f"{job.external_id:<9} {_shorten(job.title, 40):<40} {_shorten(job.company, 28):<28} "
            f"{_shorten(job.city, 22):<22} {job.work_mode.value:<7} {len(job.raw_description or ''):>6}"
        )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the smoke run's command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="python -m src.scrapers.jobvision",
        description="Smoke-run the JobVision scraper and print the jobs (nothing is stored).",
    )
    parser.add_argument("--keyword", action="append", required=True, help="search keyword (repeatable)")
    parser.add_argument("--pages", type=int, help="override scraping.max_pages_per_keyword")
    parser.add_argument("--headful", action="store_true", help="show the browser window")
    parser.add_argument("--debug-capture", action="store_true", help="save API bodies and detail HTML")
    parser.add_argument("--config", default="config/config.yaml", help="path to config.yaml")
    args = parser.parse_args(argv)
    if args.pages is not None and args.pages < 1:
        parser.error("--pages must be at least 1")
    return args


def main(argv: list[str] | None = None) -> int:
    """Run the smoke run and return the process exit code."""
    args = _parse_args(argv)
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    config = _with_max_pages(load_app_config(args.config), args.pages)
    try:
        jobs = asyncio.run(
            _smoke_run(config, args.keyword, headful=args.headful, debug_capture=args.debug_capture)
        )
    except ScraperBlockedError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except PlaywrightError as exc:
        print(f"ERROR: browser failure: {str(exc).splitlines()[0]}", file=sys.stderr)
        return 1
    _print_table(jobs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
