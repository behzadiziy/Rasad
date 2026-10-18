"""Shared scraper infrastructure.

- Browser contexts with fixed browser settings and a persistent per-platform
  `storage_state` (cookies and local storage, including Cloudflare clearance).
- Cloudflare challenge detection that only waits: it never interacts with the
  challenge. In headful mode the user solves it manually.
- Debug artifacts under `data/debug/{platform}/`.
- Pure helpers: JSON-LD `JobPosting` extraction and work-mode detection.
- `BaseScraper`, the async context manager that platform scrapers subclass.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Self

from loguru import logger
from playwright.async_api import Browser, BrowserContext, Page
from playwright.async_api import Error as PlaywrightError
from selectolax.lexbor import LexborHTMLParser

from src.models import AppConfig, JobPost, Platform, WorkMode
from src.scrapers import constants
from src.utils import normalize_text

IsKnown = Callable[[str, str], Awaitable[bool]]

_SLUG_INVALID_RE = re.compile(r"[^\w-]+")
_SLUG_MAX_LEN = 60
_SLUG_FALLBACK = "artifact"
_JOB_POSTING_TYPE = "JobPosting"


class ScraperBlockedError(Exception):
    """Raised when a page stays behind a Cloudflare challenge past the wait limit."""


# ── Browser context and storage_state ──────────────────────────────────────


def _state_path(platform: Platform) -> Path:
    """Return the `storage_state` file for a platform."""
    return constants.BROWSER_STATE_DIR / f"{platform}_state.json"


async def create_browser_context(browser: Browser, platform: Platform) -> BrowserContext:
    """Create a browser context with the shared settings and the platform's saved state.

    The saved `storage_state` is loaded when its file exists. If the file is
    unreadable or corrupt, a warning is logged and a fresh context is created.
    """
    options: dict[str, Any] = {
        "user_agent": constants.USER_AGENT,
        "viewport": {"width": constants.VIEWPORT_WIDTH, "height": constants.VIEWPORT_HEIGHT},
        "locale": constants.LOCALE,
        "timezone_id": constants.TIMEZONE_ID,
        "extra_http_headers": {"Accept-Language": constants.ACCEPT_LANGUAGE},
    }
    state_path = _state_path(platform)
    context: BrowserContext | None = None
    if state_path.is_file():
        try:
            context = await browser.new_context(storage_state=state_path, **options)
            logger.info("[{}] Created browser context with saved state from {}", platform, state_path)
        except (PlaywrightError, OSError, ValueError) as exc:
            logger.warning("[{}] Ignoring unusable storage state {}: {}", platform, state_path, exc)
    if context is None:
        context = await browser.new_context(**options)
        logger.info("[{}] Created browser context without saved state", platform)
    context.set_default_timeout(constants.NAVIGATION_TIMEOUT_MS)
    return context


async def save_storage_state(context: BrowserContext, platform: Platform) -> None:
    """Write the context's `storage_state` to `data/browser_state/{platform}_state.json`.

    The file is written to a temporary sibling first and then renamed, so an
    interrupted save never leaves a truncated state file behind.
    """
    state = await context.storage_state()
    state_path = _state_path(platform)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_name(f"{state_path.name}.tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, state_path)
    logger.info("[{}] Saved storage state to {}", platform, state_path)


# ── Cloudflare ─────────────────────────────────────────────────────────────


async def _is_challenge_page(page: Page) -> bool:
    """Return True while the page shows a Cloudflare challenge or block page.

    The challenge reloads the page when it completes, which can destroy the
    execution context mid-check; that transient state counts as "still challenged".
    """
    try:
        title = (await page.title()).casefold()
        if any(marker in title for marker in constants.CLOUDFLARE_TITLE_MARKERS):
            return True
        selector = ", ".join(constants.CLOUDFLARE_DOM_MARKERS)
        return await page.locator(selector).count() > 0
    except PlaywrightError as exc:
        if page.is_closed():
            raise
        logger.debug("Cloudflare check interrupted (page is navigating): {}", exc)
        return True


async def wait_for_cloudflare(page: Page, platform: Platform, *, headful: bool) -> None:
    """Wait until the page is no longer behind a Cloudflare challenge.

    Waits up to `CLOUDFLARE_WAIT_HEADFUL_S` (headful: time for the user to solve
    the challenge in the window) or `CLOUDFLARE_WAIT_HEADLESS_S`. This function
    only observes the page; it never clicks or otherwise touches the challenge.

    Raises:
        ScraperBlockedError: If the challenge is still present after the limit,
            or the page was closed while waiting.
    """
    try:
        challenged = await _is_challenge_page(page)
    except PlaywrightError as exc:
        raise ScraperBlockedError(f"[{platform}] Page closed before the Cloudflare check: {exc}") from exc
    if not challenged:
        logger.debug("[{}] No Cloudflare challenge on {}", platform, page.url)
        return

    limit = constants.CLOUDFLARE_WAIT_HEADFUL_S if headful else constants.CLOUDFLARE_WAIT_HEADLESS_S
    if headful:
        logger.warning(
            "[{}] Cloudflare challenge on {}. Solve it in the browser window; waiting up to {:.0f}s",
            platform,
            page.url,
            limit,
        )
    else:
        logger.info("[{}] Cloudflare challenge on {}; waiting up to {:.0f}s", platform, page.url, limit)

    started = time.monotonic()
    deadline = started + limit
    while time.monotonic() < deadline:
        await asyncio.sleep(constants.CLOUDFLARE_POLL_INTERVAL_S)
        try:
            challenged = await _is_challenge_page(page)
        except PlaywrightError as exc:
            raise ScraperBlockedError(f"[{platform}] Page closed while waiting for Cloudflare: {exc}") from exc
        if not challenged:
            logger.info("[{}] Cloudflare challenge passed after {:.1f}s", platform, time.monotonic() - started)
            return

    if headful:
        advice = "Solve the challenge within the time limit, or check your network/IP reputation."
    else:
        advice = (
            f"Run the probe headful once to pass it manually and save the state: "
            f"uv run python -m scripts.probe --platform {platform} --keyword <keyword>"
        )
    logger.warning("[{}] Still blocked by Cloudflare after {:.0f}s on {}. {}", platform, limit, page.url, advice)
    raise ScraperBlockedError(f"[{platform}] Blocked by Cloudflare after {limit:.0f}s on {page.url}. {advice}")


# ── Debug artifacts ────────────────────────────────────────────────────────


def _slugify(name: str) -> str:
    """Make a filesystem-safe slug of at most `_SLUG_MAX_LEN` characters."""
    slug = _SLUG_INVALID_RE.sub("_", name).strip("_-")[:_SLUG_MAX_LEN].strip("_-")
    return slug or _SLUG_FALLBACK


def save_debug_artifact(platform: Platform, name: str, content: str | bytes, ext: str) -> Path:
    """Write `data/debug/{platform}/{UTC timestamp}_{slug}.{ext}` and return its path.

    `name` is reduced to a filesystem-safe slug (at most 60 characters). Text is
    written as UTF-8. An existing file is never overwritten: a numeric suffix is
    added instead.
    """
    directory = constants.DEBUG_DIR / platform
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
    suffix = ext.lstrip(".") or "txt"
    stem = f"{timestamp}_{_slugify(name)}"
    path = directory / f"{stem}.{suffix}"
    counter = 2
    while path.exists():
        path = directory / f"{stem}_{counter}.{suffix}"
        counter += 1
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    logger.debug("[{}] Saved debug artifact {}", platform, path)
    return path


# ── JSON-LD ────────────────────────────────────────────────────────────────


def _is_job_posting_type(value: Any) -> bool:
    """Return True if an `@type` value is, or contains, `JobPosting`.

    Prefixed forms such as `schema:JobPosting` or `https://schema.org/JobPosting`
    are accepted too.
    """
    types = value if isinstance(value, list) else [value]
    for item in types:
        if isinstance(item, str) and re.split(r"[/:#]", item)[-1] == _JOB_POSTING_TYPE:
            return True
    return False


def _find_job_posting(data: Any) -> dict[str, Any] | None:
    """Search a parsed JSON-LD value (object, list, or `@graph`) for a `JobPosting`."""
    if isinstance(data, list):
        for item in data:
            found = _find_job_posting(item)
            if found is not None:
                return found
        return None
    if isinstance(data, dict):
        if _is_job_posting_type(data.get("@type")):
            return data
        if "@graph" in data:
            return _find_job_posting(data["@graph"])
    return None


def extract_json_ld_job_posting(html: str) -> dict[str, Any] | None:
    """Return the first JSON-LD object whose `@type` is (or contains) `JobPosting`.

    Every `<script type="application/ld+json">` block is parsed; blocks that are
    not valid JSON are skipped. Top-level lists and `@graph` containers are
    searched. Control characters inside strings (raw newlines in descriptions)
    are tolerated. Returns None when no block contains a `JobPosting`.
    """
    tree = LexborHTMLParser(html)
    for index, node in enumerate(tree.css('script[type="application/ld+json" i]')):
        text = node.text(deep=True).strip()
        if not text:
            continue
        try:
            data = json.loads(text, strict=False)
        except json.JSONDecodeError as exc:
            logger.debug("Skipping invalid JSON-LD block #{}: {}", index, exc)
            continue
        found = _find_job_posting(data)
        if found is not None:
            return found
    return None


# ── Work mode ──────────────────────────────────────────────────────────────

_HYBRID_CUES = tuple(normalize_text(cue) for cue in constants.HYBRID_CUES)
_REMOTE_CUES = tuple(normalize_text(cue) for cue in constants.REMOTE_CUES)


def detect_work_mode(text: str) -> WorkMode:
    """Classify a short label or badge as HYBRID, REMOTE, or ONSITE.

    The text is normalized first, then HYBRID cues are checked before REMOTE
    cues. Without any cue the result is ONSITE (logged at DEBUG). Never pass a
    full job description: negations such as «امکان دورکاری وجود ندارد» would
    produce false positives.
    """
    normalized = normalize_text(text)
    if any(cue in normalized for cue in _HYBRID_CUES):
        return WorkMode.HYBRID
    if any(cue in normalized for cue in _REMOTE_CUES):
        return WorkMode.REMOTE
    logger.debug("No work-mode cue in {!r}; defaulting to ONSITE", text)
    return WorkMode.ONSITE


# ── BaseScraper ────────────────────────────────────────────────────────────


class BaseScraper(ABC):
    """Base class for platform scrapers; use it as an async context manager.

    Entering creates a browser context (loading the saved `storage_state`);
    exiting always saves the state and closes the context, even after errors.
    """

    platform: ClassVar[Platform]

    def __init__(
        self,
        browser: Browser,
        config: AppConfig,
        is_known: IsKnown,
        *,
        headful: bool = False,
        debug_capture: bool = False,
    ) -> None:
        """Store the shared browser, config, and dedup callback.

        Args:
            browser: A launched Playwright browser; the scraper owns only its context.
            config: The application config; `max_pages_per_keyword` is read from it.
            is_known: `await is_known(platform, external_id)` returns True for stored jobs.
            headful: Whether the browser has a visible window (longer Cloudflare wait).
            debug_capture: Whether to save HTML/JSON debug artifacts while scraping.
        """
        self._browser = browser
        self._config = config
        self._is_known = is_known
        self._headful = headful
        self._debug_capture = debug_capture
        self._max_pages = config.scraping.max_pages_per_keyword
        self._context: BrowserContext | None = None

    async def __aenter__(self) -> Self:
        """Create the browser context for this platform."""
        self._context = await create_browser_context(self._browser, self.platform)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Save `storage_state`, then close the context; both run even after errors."""
        context = self._context
        if context is None:
            return
        self._context = None
        try:
            await save_storage_state(context, self.platform)
        except (PlaywrightError, OSError):
            logger.exception("[{}] Failed to save storage state", self.platform)
        finally:
            try:
                await context.close()
            except PlaywrightError as exc:
                logger.warning("[{}] Failed to close browser context: {}", self.platform, exc)
            logger.debug("[{}] Browser context closed", self.platform)

    @abstractmethod
    async def fetch_jobs(self, keywords: list[str]) -> list[JobPost]:
        """Scrape all keywords sequentially and return the jobs found.

        Detail pages are visited only for jobs where `is_known` returns False.
        """

    def _require_context(self) -> BrowserContext:
        """Return the active context, or raise if the scraper was not entered."""
        if self._context is None:
            raise RuntimeError(f"{type(self).__name__} must be used as 'async with' before scraping")
        return self._context

    async def _new_page(self) -> Page:
        """Open a new page in the active context."""
        return await self._require_context().new_page()

    async def _wait_for_cloudflare(self, page: Page) -> None:
        """Wait out a Cloudflare challenge using this scraper's headful setting."""
        await wait_for_cloudflare(page, self.platform, headful=self._headful)

    async def _known(self, external_id: str) -> bool:
        """Return True if this platform's job is already stored."""
        return await self._is_known(self.platform, external_id)

    def _debug(self, name: str, content: str | bytes, ext: str) -> Path | None:
        """Save a debug artifact only when `debug_capture` is on."""
        if not self._debug_capture:
            return None
        return save_debug_artifact(self.platform, name, content, ext)
