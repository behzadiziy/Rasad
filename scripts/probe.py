"""Live-site probe: capture real JSON and HTML from JobVision or Jobinja.

Opens a search page (`--keyword`) or any URL (`--url`, e.g. a job detail page),
waits out Cloudflare (headful by default, so you can solve a challenge by hand),
and records every JSON response the page receives, including POST requests.
Artifacts go to `data/debug/{platform}/` and the session to
`data/browser_state/{platform}_state.json`, which later headless runs reuse.

Usage:
    uv run python -m scripts.probe --platform jobinja --keyword "هوش مصنوعی"
    uv run python -m scripts.probe --platform jobvision --keyword AI --wait 20
    uv run python -m scripts.probe --platform jobinja --url <job detail URL> --headless

Exit codes: 0 on success; 1 when blocked by Cloudflare, navigation fails, or
the browser cannot start.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_args
from urllib.parse import quote, unquote, urlsplit

from loguru import logger
from playwright.async_api import BrowserContext, Page, Response, async_playwright
from playwright.async_api import Error as PlaywrightError

from src.models import Platform
from src.scrapers import constants
from src.scrapers.base import (
    ScraperBlockedError,
    create_browser_context,
    extract_json_ld_job_posting,
    save_debug_artifact,
    save_storage_state,
    wait_for_cloudflare,
)

_SEARCH_URL_TEMPLATES: dict[Platform, str] = {
    "jobvision": constants.JOBVISION_SEARCH_URL_TEMPLATE,
    "jobinja": constants.JOBINJA_SEARCH_URL_TEMPLATE,
}
_URL_COLUMN_WIDTH = 90
_HINT_MAX_KEYS = 8
# Streaming or long-poll responses never finish loading, so `response.body()` would
# wait forever; such bodies are recorded as unavailable after this many seconds.
_BODY_READ_TIMEOUT_S = 20.0


@dataclass
class _CapturedJson:
    """One JSON response, as saved to disk."""

    index: int
    request_url: str
    method: str
    request_post_data: str | None
    status: int
    body: Any = None
    size: int = 0
    error: str | None = None
    path: Path | None = None

    def to_record(self) -> dict[str, Any]:
        """Return the on-disk wrapper object."""
        return {
            "request_url": self.request_url,
            "method": self.method,
            "request_post_data": self.request_post_data,
            "status": self.status,
            "body": self.body,
        }


@dataclass
class _ProbeResult:
    """Everything the probe produced, for the summary."""

    captures: list[_CapturedJson] = field(default_factory=list)
    html_path: Path | None = None
    json_ld_path: Path | None = None
    json_ld_found: bool = False
    state_path: Path | None = None
    error: str | None = None


class _JsonRecorder:
    """Records every JSON response in a context, reading bodies as they arrive.

    Body reads start immediately in tasks (bodies of earlier navigations can be
    evicted later) and `drain` awaits them all before the page is closed. Each
    read is bounded by `_BODY_READ_TIMEOUT_S`, so `drain` always finishes.
    """

    def __init__(self) -> None:
        """Start with no captures and no pending body reads."""
        self.captures: list[_CapturedJson] = []
        self._tasks: list[asyncio.Task[None]] = []

    def on_response(self, response: Response) -> None:
        """Response listener: schedule a capture for JSON content types."""
        content_type = response.headers.get("content-type", "").lower()
        if "json" not in content_type:
            return
        capture = _CapturedJson(
            index=len(self.captures) + 1,
            request_url=response.url,
            method=response.request.method,
            request_post_data=_post_data(response),
            status=response.status,
        )
        self.captures.append(capture)
        self._tasks.append(asyncio.create_task(self._read_body(response, capture)))

    async def _read_body(self, response: Response, capture: _CapturedJson) -> None:
        """Read and parse one response body; record failures instead of raising."""
        try:
            raw = await asyncio.wait_for(response.body(), _BODY_READ_TIMEOUT_S)
        except TimeoutError:
            capture.error = f"body not finished after {_BODY_READ_TIMEOUT_S:.0f}s (streaming or long-poll?)"
            logger.debug("Timed out reading body of {}", capture.request_url)
            return
        except PlaywrightError as exc:
            capture.error = f"body unavailable: {exc}".splitlines()[0]
            logger.debug("Could not read body of {}: {}", capture.request_url, exc)
            return
        capture.size = len(raw)
        try:
            capture.body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            capture.body = raw.decode("utf-8", errors="replace")
            capture.error = "not valid JSON; saved as text"

    async def drain(self) -> None:
        """Await all body reads, including ones scheduled while waiting."""
        while pending := [task for task in self._tasks if not task.done()]:
            await asyncio.gather(*pending, return_exceptions=True)


def _post_data(response: Response) -> str | None:
    """Return the request's POST body as text (binary bodies are summarized)."""
    request = response.request
    try:
        return request.post_data
    except UnicodeDecodeError:
        buffer = request.post_data_buffer
        return f"<binary body, {len(buffer) if buffer else 0} bytes>"


def _build_url(platform: Platform, keyword: str | None, url: str | None) -> str:
    """Return `--url` as given, or page 1 of the platform's keyword search."""
    if url is not None:
        return url
    if keyword is None:
        raise ValueError("Either a keyword or a URL is required")
    return _SEARCH_URL_TEMPLATES[platform].format(keyword=quote(keyword, safe=""), page=1)


def _artifact_name(capture: _CapturedJson) -> str:
    """Build a readable artifact name from the response index, method, and URL path."""
    parts = urlsplit(capture.request_url)
    return f"response_{capture.index:03d}_{capture.method}_{parts.netloc}{unquote(parts.path)}"


def _save_captures(platform: Platform, captures: list[_CapturedJson]) -> None:
    """Save each captured response as its own JSON artifact."""
    for capture in captures:
        record = json.dumps(capture.to_record(), ensure_ascii=False, indent=2)
        capture.path = save_debug_artifact(platform, _artifact_name(capture), record, "json")


async def _save_page(page: Page, platform: Platform, result: _ProbeResult) -> None:
    """Save the final page HTML and, if present, its JSON-LD `JobPosting` block."""
    try:
        html = await page.content()
    except PlaywrightError as exc:
        logger.warning("Could not read the page HTML: {}", exc)
        return
    result.html_path = save_debug_artifact(platform, f"page_{unquote(urlsplit(page.url).path)}", html, "html")
    job_posting = extract_json_ld_job_posting(html)
    if job_posting is not None:
        result.json_ld_found = True
        text = json.dumps(job_posting, ensure_ascii=False, indent=2)
        result.json_ld_path = save_debug_artifact(platform, "json_ld_job_posting", text, "json")


async def _finish(context: BrowserContext, platform: Platform, result: _ProbeResult) -> None:
    """Save `storage_state` and close the context; each step runs even if the other fails."""
    try:
        await save_storage_state(context, platform)
        result.state_path = constants.BROWSER_STATE_DIR / f"{platform}_state.json"
    except (PlaywrightError, OSError):
        logger.exception("[{}] Failed to save storage state", platform)
    finally:
        try:
            await context.close()
        except PlaywrightError as exc:
            logger.warning("[{}] Failed to close the browser context: {}", platform, exc)


async def _probe(platform: Platform, url: str, *, headless: bool, wait_s: float) -> _ProbeResult:
    """Run one probe and return what it captured; errors are recorded, not raised."""
    result = _ProbeResult()
    recorder = _JsonRecorder()
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=headless)
        except PlaywrightError as exc:
            result.error = (
                f"Could not launch Chromium: {str(exc).splitlines()[0]}\n"
                "Install it with: uv run playwright install chromium"
            )
            return result
        try:
            context = await create_browser_context(browser, platform)
            # Attached before navigating, at context level, so responses of every
            # page (including tabs the user opens) are captured.
            context.on("response", recorder.on_response)
            try:
                page = await context.new_page()
                try:
                    logger.info("[{}] Opening {}", platform, url)
                    await page.goto(url, wait_until="domcontentloaded")
                    await wait_for_cloudflare(page, platform, headful=not headless)
                    logger.info("[{}] Waiting {:.0f}s for the page's API calls", platform, wait_s)
                    await asyncio.sleep(wait_s)
                except ScraperBlockedError as exc:
                    result.error = f"Blocked by Cloudflare: {exc}"
                except PlaywrightError as exc:
                    result.error = f"Navigation failed for {url}: {str(exc).splitlines()[0]}"
                await recorder.drain()
                if not page.is_closed():
                    await _save_page(page, platform, result)
                _save_captures(platform, recorder.captures)
                result.captures = recorder.captures
            finally:
                await recorder.drain()
                await _finish(context, platform, result)
        finally:
            await browser.close()
    return result


def _structure_hint(body: Any) -> str:
    """Describe a JSON body: top-level keys, or list length plus first-item keys."""
    if isinstance(body, dict):
        return "{" + _keys(body) + "}"
    if isinstance(body, list):
        first = body[0] if body else None
        inner = f", item keys {{{_keys(first)}}}" if isinstance(first, dict) else ""
        return f"list[{len(body)}]{inner}"
    if body is None:
        return "-"
    return type(body).__name__


def _keys(obj: dict[str, Any]) -> str:
    """Join up to `_HINT_MAX_KEYS` keys, noting how many were left out."""
    keys = list(obj)
    shown = ", ".join(str(key) for key in keys[:_HINT_MAX_KEYS])
    return shown + (f", +{len(keys) - _HINT_MAX_KEYS}" if len(keys) > _HINT_MAX_KEYS else "")


def _shorten(text: str, width: int) -> str:
    """Truncate text to `width` characters with an ellipsis."""
    return text if len(text) <= width else text[: width - 1] + "…"


def _print_summary(platform: Platform, url: str, result: _ProbeResult) -> None:
    """Print the capture table and every output path."""
    print(f"\nProbe summary: {platform} — {url}")
    print(f"JSON responses captured: {len(result.captures)}")
    if result.captures:
        print(f"{'#':>3}  {'METHOD':<6} {'STATUS':>6} {'BYTES':>9}  {'URL':<{_URL_COLUMN_WIDTH}}  STRUCTURE")
        for capture in result.captures:
            hint = _structure_hint(capture.body)
            if capture.error:
                hint = f"{hint}  [{capture.error}]"
            print(
                f"{capture.index:>3}  {capture.method:<6} {capture.status:>6} {capture.size:>9}  "
                f"{_shorten(capture.request_url, _URL_COLUMN_WIDTH):<{_URL_COLUMN_WIDTH}}  {hint}"
            )
    print(f"\nJSON-LD JobPosting found: {'yes' if result.json_ld_found else 'no'}")
    print("\nOutput files:")
    print(f"  page HTML:     {result.html_path or '(not saved)'}")
    if result.json_ld_path:
        print(f"  JSON-LD:       {result.json_ld_path}")
    print(f"  storage state: {result.state_path or '(not saved)'}")
    for capture in result.captures:
        print(f"  response #{capture.index:03d}: {capture.path}")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the probe's command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="python -m scripts.probe",
        description="Capture real JSON and HTML from a job board for building scrapers.",
    )
    parser.add_argument("--platform", required=True, choices=get_args(Platform))
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--keyword", help="search keyword; opens page 1 of SEARCH_URL_TEMPLATE")
    target.add_argument("--url", help="open this URL instead (e.g. a job detail page)")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="run without a window (default: headful, so you can pass Cloudflare manually)",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=15.0,
        metavar="SECONDS",
        help="extra seconds after load for API calls and manual scrolling (default: 15)",
    )
    args = parser.parse_args(argv)
    if args.wait < 0:
        parser.error("--wait must be zero or positive")
    return args


def main(argv: list[str] | None = None) -> int:
    """Run the probe and return the process exit code."""
    args = _parse_args(argv)
    platform: Platform = args.platform
    url = _build_url(platform, args.keyword, args.url)
    result = asyncio.run(_probe(platform, url, headless=args.headless, wait_s=args.wait))
    _print_summary(platform, url, result)
    if result.error:
        sys.stdout.flush()
        print(f"\nERROR: {result.error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
