"""Jobinja (jobinja.ir) scraper.

Jobinja serves server-rendered HTML behind Cloudflare, with no JSON API. Search
pages are parsed with `parse_search_html`. For jobs that `is_known` reports as
new, the detail page supplies the description (JSON-LD first, then the DOM),
the salary, and the dedicated «نوع همکاری» field, which is combined with the
card's contract badge: either one saying «دورکاری» makes the job REMOTE.

Every site-specific value lives in `src.scrapers.constants` (`JOBINJA_*`).

Smoke run (prints jobs, writes nothing to the database):
    uv run python -m src.scrapers.jobinja --keyword "هوش مصنوعی" [--pages 1] [--headful] [--debug-capture]
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from typing import ClassVar
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from loguru import logger
from playwright.async_api import Page, async_playwright
from playwright.async_api import Error as PlaywrightError
from selectolax.lexbor import LexborHTMLParser, LexborNode

from src.models import AppConfig, JobPost, Platform, WorkMode, load_app_config
from src.scrapers import constants
from src.scrapers.base import BaseScraper, ScraperBlockedError, detect_work_mode, extract_json_ld_job_posting
from src.utils import human_delay, normalize_text

_PLATFORM: Platform = "jobinja"
_EXTERNAL_ID_RE = re.compile(constants.JOBINJA_EXTERNAL_ID_PATTERN)
_JSON_LD_SELECTOR = 'script[type="application/ld+json"]'
_VALUE_JOINER = " | "
# A private-use character marks line breaks while HTML is flattened to text, so
# they survive the collapsing of the source's own whitespace.
_LINE_MARK = ""
_BLOCK_SELECTOR = ", ".join(
    (
        "address", "article", "aside", "blockquote", "dd", "div", "dl", "dt", "figcaption", "figure",
        "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p",
        "pre", "section", "table", "tr", "ul",
    )
)  # fmt: skip
# Precedence when the card badge and the detail field disagree: any remote signal wins,
# because a job stored as ONSITE by mistake is saved as IGNORED and never re-evaluated.
_WORK_MODE_PRECEDENCE: tuple[WorkMode, ...] = (WorkMode.REMOTE, WorkMode.HYBRID, WorkMode.ONSITE)


# ── Pure helpers ───────────────────────────────────────────────────────────


def extract_external_id(url: str) -> str | None:
    """Return the job code from a Jobinja job URL, or None if the URL is not a job URL.

    Matches `JOBINJA_EXTERNAL_ID_PATTERN`, e.g. «tEuf» in
    `https://jobinja.ir/companies/aloostad/jobs/tEuf/<slug>?_ref=16`.
    """
    match = _EXTERNAL_ID_RE.search(url.strip())
    return match.group(1) if match else None


def _clean_inline(text: str) -> str:
    """Collapse all whitespace runs in a short label into single spaces."""
    return " ".join(text.split())


def _canonical_job_url(href: str) -> str:
    """Resolve a card link against the site and drop its tracking query and fragment."""
    parts = urlsplit(urljoin(constants.JOBINJA_BASE_URL, href.strip()))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _node_to_text(node: LexborNode) -> str | None:
    """Flatten an HTML node to plain text with one line per block or `<br>`; None when empty.

    The node's tree is modified (line-break markers are inserted), so pass a
    node from a tree parsed for this purpose. Inline elements such as
    `<strong>` stay on their line; blank lines are removed.
    """
    for br in node.css("br"):
        br.replace_with(_LINE_MARK)
    for block in node.css(_BLOCK_SELECTOR):
        block.insert_before(_LINE_MARK)
        block.insert_after(_LINE_MARK)
    raw = node.text(deep=True, separator="")
    lines = (_clean_inline(part) for part in raw.split(_LINE_MARK))
    text = "\n".join(line for line in lines if line)
    return text or None


def _html_to_text(html: str) -> str | None:
    """Convert an HTML fragment (or plain text) to plain text; None when nothing remains."""
    tree = LexborHTMLParser(html)
    root = tree.body if tree.body is not None else tree.root
    if root is None:
        return None
    return _node_to_text(root)


def _card_text(card: LexborNode, selector: str) -> str:
    """Return the inline text of the first match of `selector` in a card, or ""."""
    node = card.css_first(selector)
    return _clean_inline(node.text()) if node is not None else ""


def _parse_card(card: LexborNode) -> JobPost:
    """Build a `JobPost` from one search card.

    The work mode comes only from the contract badge; a card without one is
    ONSITE here. `_apply_detail` later combines it with the detail page's field.

    Raises:
        ValueError: If the card has no job link, ID, or title.
    """
    link = card.css_first(constants.JOBINJA_CARD_LINK_SELECTOR)
    href = link.attributes.get("href") if link is not None else None
    if not href or not href.strip():
        raise ValueError("card has no job link")
    url = _canonical_job_url(href)
    external_id = extract_external_id(url)
    if external_id is None:
        raise ValueError(f"no job ID in card link {href!r}")
    title = _card_text(card, constants.JOBINJA_CARD_TITLE_SELECTOR)
    if not title:
        raise ValueError(f"card {external_id} has no title")
    badge = _card_text(card, constants.JOBINJA_CARD_WORK_TYPE_SELECTOR)
    if badge:
        work_mode = detect_work_mode(badge)
    else:
        logger.debug("[jobinja] Card {} has no work-type badge", external_id)
        work_mode = WorkMode.ONSITE
    return JobPost(
        platform=_PLATFORM,
        external_id=external_id,
        title=title,
        company=_card_text(card, constants.JOBINJA_CARD_COMPANY_SELECTOR),
        city=_card_text(card, constants.JOBINJA_CARD_CITY_SELECTOR),
        work_mode=work_mode,
        url=url,
    )


def parse_search_html(html: str) -> list[JobPost]:
    """Parse a search-results page into `JobPost`s.

    Cards are read from `JOBINJA_RESULTS_CONTAINER_SELECTOR`; a page without
    that container (e.g. "no results") yields an empty list. The city comes
    from the card's location item and the work mode from its contract badge
    via `detect_work_mode`. Cards without a valid link or job ID are logged
    and skipped. Salaries are not shown on cards, so `salary` is None.
    """
    container = LexborHTMLParser(html).css_first(constants.JOBINJA_RESULTS_CONTAINER_SELECTOR)
    if container is None:
        logger.debug("[jobinja] No results container ({})", constants.JOBINJA_RESULTS_CONTAINER_SELECTOR)
        return []
    nodes = container.css(constants.JOBINJA_CARD_SELECTOR)
    jobs: list[JobPost] = []
    for index, node in enumerate(nodes):
        try:
            jobs.append(_parse_card(node))
        except ValueError as exc:
            logger.warning("[jobinja] Skipping malformed job card #{}: {}", index, exc)
    logger.debug("[jobinja] Parsed {} of {} job card(s)", len(jobs), len(nodes))
    return jobs


def parse_detail_html(html: str) -> str | None:
    """Return a detail page's job description as plain text, or None.

    The JSON-LD `JobPosting.description` is preferred: on Jobinja it is the
    same HTML as the page's description, line breaks included. The
    `JOBINJA_DETAIL_DESCRIPTION_SELECTOR` element is the fallback, without the
    site's own trailing link. HTML becomes one line per block or `<br>`.
    """
    job_posting = extract_json_ld_job_posting(html)
    if job_posting is not None:
        description = job_posting.get("description")
        if isinstance(description, str) and description.strip():
            text = _html_to_text(description)
            if text:
                return text
    logger.debug("[jobinja] No JSON-LD description; trying the description element")
    node = LexborHTMLParser(html).css_first(constants.JOBINJA_DETAIL_DESCRIPTION_SELECTOR)
    if node is None:
        return None
    for excluded in node.css(constants.JOBINJA_DETAIL_DESCRIPTION_EXCLUDE_SELECTOR):
        excluded.decompose()
    return _node_to_text(node)


def _detail_field_values(html: str, title: str) -> list[str] | None:
    """Return the values of the detail info-box field with this title; None when the field is absent.

    Titles are compared after `normalize_text`, so ZWNJ and Arabic/Persian
    letter variants do not matter.
    """
    wanted = normalize_text(title)
    for item in LexborHTMLParser(html).css(constants.JOBINJA_DETAIL_INFO_ITEM_SELECTOR):
        title_node = item.css_first(constants.JOBINJA_DETAIL_INFO_TITLE_SELECTOR)
        if title_node is None or normalize_text(title_node.text()) != wanted:
            continue
        values = (_clean_inline(node.text()) for node in item.css(constants.JOBINJA_DETAIL_INFO_VALUE_SELECTOR))
        return [value for value in values if value]
    return None


def parse_detail_work_mode(html: str) -> WorkMode | None:
    """Return the work mode from the detail page's dedicated work-type field, or None.

    Only the «نوع همکاری» field (`JOBINJA_DETAIL_WORK_TYPE_TITLE`) is read, never
    the description or JSON-LD (whose `jobLocationType` is TELECOMMUTE even for
    on-site jobs). Its tags, e.g. «تمام وقت» and «دورکاری», are classified with
    `detect_work_mode`. Returns None when the field is absent or empty.
    """
    values = _detail_field_values(html, constants.JOBINJA_DETAIL_WORK_TYPE_TITLE)
    if not values:
        return None
    return detect_work_mode(_VALUE_JOINER.join(values))


def _parse_detail_salary(html: str) -> str | None:
    """Return the detail page's salary text (e.g. «توافقی»), or None when the field is absent."""
    values = _detail_field_values(html, constants.JOBINJA_DETAIL_SALARY_TITLE)
    return _VALUE_JOINER.join(values) if values else None


def _merge_work_modes(card_mode: WorkMode, detail_mode: WorkMode | None) -> WorkMode:
    """Combine the card badge's and the detail field's work modes; the more remote one wins.

    REMOTE if either says REMOTE, else HYBRID if either says HYBRID, else ONSITE.
    A card showing only «قرارداد تمام‌وقت» can still be remote on its detail page.
    """
    if detail_mode is None:
        return card_mode
    return next(mode for mode in _WORK_MODE_PRECEDENCE if mode in (card_mode, detail_mode))


def _apply_detail(job: JobPost, html: str) -> JobPost:
    """Return the card's job updated with a detail page's description, salary, and work mode.

    The work mode merges the card badge with the «نوع همکاری» field via
    `_merge_work_modes`. Missing detail fields leave the card's values in place.
    """
    updates: dict[str, object] = {}
    description = parse_detail_html(html)
    if description is None:
        logger.warning("[jobinja] No description on {}; verify JOBINJA_DETAIL_DESCRIPTION_SELECTOR", job.url)
    else:
        updates["raw_description"] = description
    if job.salary is None:
        salary = _parse_detail_salary(html)
        if salary is not None:
            updates["salary"] = salary
    work_mode = _merge_work_modes(job.work_mode, parse_detail_work_mode(html))
    if work_mode != job.work_mode:
        logger.debug("[jobinja] Work mode of {}: card {} -> detail {}", job.external_id, job.work_mode, work_mode)
        updates["work_mode"] = work_mode
    return job.model_copy(update=updates) if updates else job


# ── Scraper ────────────────────────────────────────────────────────────────


class JobinjaScraper(BaseScraper):
    """Scrapes Jobinja's server-rendered search results and detail pages."""

    platform: ClassVar[Platform] = _PLATFORM

    async def fetch_jobs(self, keywords: list[str]) -> list[JobPost]:
        """Scrape every keyword, page by page, and return the new jobs found.

        Keywords and pages are processed sequentially with `human_delay()`
        between navigations. Pagination stops at the first page without cards
        or at `max_pages_per_keyword`. Jobs are deduplicated by `external_id`;
        known jobs are skipped without visiting their detail page.

        Raises:
            ScraperBlockedError: If a page stays behind a Cloudflare challenge.
        """
        logger.info("[jobinja] Starting: {} keyword(s), up to {} page(s) each", len(keywords), self._max_pages)
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
                        logger.info("[jobinja] {!r} page {}: no jobs; stopping pagination", keyword, page_no)
                        break
                    new_count = 0
                    for job in listing:
                        if job.external_id in seen:
                            continue
                        seen.add(job.external_id)
                        if await self._known(job.external_id):
                            logger.debug("[jobinja] Skipping known job {}", job.external_id)
                            continue
                        await human_delay()
                        navigations += 1
                        html = await self._load_detail(page, job)
                        jobs.append(_apply_detail(job, html) if html is not None else job)
                        new_count += 1
                    logger.info(
                        "[jobinja] {!r} page {}: {} job(s), {} new", keyword, page_no, len(listing), new_count
                    )
        finally:
            try:
                await page.close()
            except PlaywrightError as exc:
                logger.debug("[jobinja] Failed to close page: {}", exc)
        logger.info("[jobinja] Finished: {} new job(s)", len(jobs))
        return jobs

    async def _scrape_search_page(self, page: Page, keyword: str, page_no: int) -> list[JobPost] | None:
        """Load one search page and return its jobs; None when navigation fails.

        After the Cloudflare check, waits for the results container or the
        no-result notice. A loaded page without the container counts as
        "no results" (an empty list).
        """
        url = constants.JOBINJA_SEARCH_URL_TEMPLATE.format(keyword=quote(keyword, safe=""), page=page_no)
        logger.info("[jobinja] Opening search {!r} page {}: {}", keyword, page_no, url)
        try:
            await page.goto(url, wait_until="domcontentloaded")
        except PlaywrightError as exc:
            logger.warning("[jobinja] Navigation to {} failed: {}", url, str(exc).splitlines()[0])
            return None
        await self._wait_for_cloudflare(page)
        selector = f"{constants.JOBINJA_RESULTS_CONTAINER_SELECTOR}, {constants.JOBINJA_NO_RESULTS_SELECTOR}"
        try:
            await page.locator(selector).first.wait_for(
                state="attached", timeout=constants.JOBINJA_SEARCH_WAIT_TIMEOUT_MS
            )
        except PlaywrightError:
            logger.warning(
                "[jobinja] Neither the results container nor the no-result notice appeared on {}; treating it as "
                "no results. If the search has results, verify JOBINJA_RESULTS_CONTAINER_SELECTOR",
                url,
            )
        try:
            html = await page.content()
        except PlaywrightError as exc:
            logger.warning("[jobinja] Could not read the search page HTML: {}", exc)
            return None
        self._debug(f"search_{keyword}_p{page_no}", html, "html")
        return parse_search_html(html)

    async def _load_detail(self, page: Page, job: JobPost) -> str | None:
        """Open a job's detail page and return its HTML; None on failure."""
        logger.debug("[jobinja] Opening detail page {}", job.url)
        try:
            await page.goto(job.url, wait_until="domcontentloaded")
        except PlaywrightError as exc:
            logger.warning("[jobinja] Detail page {} failed to load: {}", job.url, str(exc).splitlines()[0])
            return None
        await self._wait_for_cloudflare(page)
        selector = f"{_JSON_LD_SELECTOR}, {constants.JOBINJA_DETAIL_DESCRIPTION_SELECTOR}"
        try:
            await page.locator(selector).first.wait_for(
                state="attached", timeout=constants.JOBINJA_DETAIL_WAIT_TIMEOUT_MS
            )
        except PlaywrightError:
            logger.debug("[jobinja] No JSON-LD or description element on {} yet", job.url)
        try:
            html = await page.content()
        except PlaywrightError as exc:
            logger.warning("[jobinja] Could not read detail page {}: {}", job.url, exc)
            return None
        self._debug(f"detail_{job.external_id}", html, "html")
        return html


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
            async with JobinjaScraper(
                browser, config, _never_known, headful=headful, debug_capture=debug_capture
            ) as scraper:
                return await scraper.fetch_jobs(keywords)
        finally:
            await browser.close()


def _shorten(text: str, width: int) -> str:
    """Truncate text to `width` characters with an ellipsis."""
    return text if len(text) <= width else text[: width - 1] + "…"


def _print_table(jobs: list[JobPost]) -> None:
    """Print ID, title, company, city, work mode, salary, and description length for each job."""
    print(f"\n{len(jobs)} job(s)")
    print(f"{'ID':<6} {'TITLE':<40} {'COMPANY':<28} {'CITY':<20} {'MODE':<7} {'SALARY':<18} {'DESC':>6}")
    for job in jobs:
        print(
            f"{job.external_id:<6} {_shorten(job.title, 40):<40} {_shorten(job.company, 28):<28} "
            f"{_shorten(job.city, 20):<20} {job.work_mode.value:<7} {_shorten(job.salary or '-', 18):<18} "
            f"{len(job.raw_description or ''):>6}"
        )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the smoke run's command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="python -m src.scrapers.jobinja",
        description="Smoke-run the Jobinja scraper and print the jobs (nothing is stored).",
    )
    parser.add_argument("--keyword", action="append", required=True, help="search keyword (repeatable)")
    parser.add_argument("--pages", type=int, help="override scraping.max_pages_per_keyword")
    parser.add_argument("--headful", action="store_true", help="show the browser window")
    parser.add_argument("--debug-capture", action="store_true", help="save search and detail page HTML")
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
        if not args.headful:
            print(
                "Cloudflare blocked the headless run. Re-run with --headful and solve the challenge in the "
                "browser window; the saved session lets later headless runs through.",
                file=sys.stderr,
            )
        return 1
    except PlaywrightError as exc:
        print(f"ERROR: browser failure: {str(exc).splitlines()[0]}", file=sys.stderr)
        return 1
    _print_table(jobs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
