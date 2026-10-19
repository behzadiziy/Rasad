"""Apply-flow probe: log in by hand, then record a manual job application without automating it.

Two modes, both with a visible browser that only the user drives:

    uv run python -m scripts.apply_probe --platform jobinja --login
    uv run python -m scripts.apply_probe --platform jobinja --job-url https://jobinja.ir/companies/.../jobs/...

`--login` opens the site; you log in by hand and press Enter in the terminal.
The session is saved to `data/browser_state/{platform}_auth_state.json` (kept
apart from the anonymous scraping state, readable only by you) and only its
cookie count is printed.

`--job-url` opens the job with that session and records the application you
make by hand: every request and response (method, URL, status), and a snapshot
(HTML and screenshot) on every navigation and whenever you type `s` + Enter.
Type `q` + Enter (or close the browser) to finish. Everything is saved under
`data/debug/{platform}/apply/{timestamp}/` with an `index.json` timeline.

Captures are redacted before they are written:
- headers: Cookie, Set-Cookie, Authorization, and anything that names a token,
  CSRF/XSRF value, session, secret, API key, or auth are dropped;
- URL query strings and request bodies keep their field names, and every value
  becomes `<redacted len=N>`; uploaded files become `<file name, size, type>`
  (Chromium's request events omit file contents, so a browser capture shows
  "size not captured");
- HTML snapshots lose inline scripts, form values, token meta tags, and email
  addresses and phone numbers; screenshots mask every form field.
Visible page text (e.g. your name in the site header) can still appear in
screenshots; captures stay local under `data/` (gitignored).

The code never clicks, types, or submits anything: it only opens the start
URL and observes.

Exit codes: 0 on success; 1 on a usage or browser error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from loguru import logger
from playwright.async_api import Browser, BrowserContext, Frame, Page, Request, Response, async_playwright
from playwright.async_api import Error as PlaywrightError
from selectolax.lexbor import LexborHTMLParser

from src.models import Platform
from src.scrapers import constants

_BASE_URLS: dict[Platform, str] = {
    "jobvision": constants.JOBVISION_BASE_URL,
    "jobinja": constants.JOBINJA_BASE_URL,
}
# Hosts a job URL may use (the site itself and its subdomains).
_SITE_HOSTS: dict[Platform, str] = {platform: urlsplit(url).hostname or "" for platform, url in _BASE_URLS.items()}

_APPLY_DIR = "apply"
_SNAPSHOT_LOAD_TIMEOUT_MS = 15_000
_SNAPSHOT_SETTLE_S = 1.0
_BODY_SUMMARY_MAX = 200

# Header names (lower-cased) that are always dropped, and substrings that drop a header.
_DROPPED_HEADERS = frozenset({"cookie", "set-cookie", "authorization", "proxy-authorization"})
_DROPPED_HEADER_PARTS = ("token", "csrf", "xsrf", "session", "secret", "api-key", "apikey", "auth")
# Header values that are URLs and may carry tokens in their query strings.
_URL_HEADERS = frozenset({"location", "referer", "origin", "content-location"})
# `<input>` types whose `value` is a button label, not user data.
_LABEL_INPUT_TYPES = frozenset({"submit", "button", "reset", "image"})
_SENSITIVE_META_PARTS = ("token", "csrf", "xsrf", "session", "secret")
# HTML attributes that hold URLs, whose query strings may carry tokens (e.g. signed links).
_URL_ATTRIBUTES = ("href", "src", "action", "formaction")

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Iranian mobile numbers (ASCII or Persian digits), with or without +98/0098/0 and separators.
_DIGIT = r"[0-9۰-۹]"
_PHONE_RE = re.compile(
    rf"(?<!{_DIGIT})(?:(?:\+|00)98|[0۰])?[\s-]?[9۹]{_DIGIT}{{2}}[\s-]?{_DIGIT}{{3}}[\s-]?{_DIGIT}{{4}}(?!{_DIGIT})"
)
_FILENAME_RE = re.compile(r'filename\*?="?([^";]*)"?', re.IGNORECASE)
_NAME_RE = re.compile(r'(?<![\w*])name="?([^";]*)"?', re.IGNORECASE)
_BOUNDARY_RE = re.compile(r'boundary="?([^";]+)"?', re.IGNORECASE)


# ── Redaction (pure functions) ─────────────────────────────────────────────


def placeholder(value: str) -> str:
    """Return the stand-in for a redacted value: `<redacted len=N>`."""
    return f"<redacted len={len(value)}>"


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Drop secret-bearing headers; redact query strings inside URL-valued headers."""
    kept: dict[str, str] = {}
    for name, value in headers.items():
        lower = name.lower()
        if lower in _DROPPED_HEADERS or any(part in lower for part in _DROPPED_HEADER_PARTS):
            continue
        kept[name] = redact_url(value) if lower in _URL_HEADERS else value
    return kept


def redact_url(url: str) -> str:
    """Keep the scheme, host, and path; replace every query and fragment value with a placeholder."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return placeholder(url)
    if parts.scheme in ("data", "blob"):
        return f"{parts.scheme}:{placeholder(url)}"
    query = "&".join(
        f"{key}={placeholder(value)}" for key, value in parse_qsl(parts.query, keep_blank_values=True)
    )
    fragment = placeholder(parts.fragment) if parts.fragment else ""
    # Credentials in the URL (user:pass@host) are dropped with the netloc rebuild.
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, query, fragment))


def redact_json(value: Any) -> Any:
    """Keep the structure and keys of a JSON value; replace every leaf with a placeholder."""
    if isinstance(value, dict):
        return {key: redact_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value)
    return placeholder(text)


def _content_type(content_type: str | None) -> str:
    """Return the bare, lower-cased media type."""
    return (content_type or "").split(";", 1)[0].strip().lower()


def summarize_multipart(body: bytes, content_type: str) -> list[dict[str, str]]:
    """Summarize a multipart/form-data body: field names with redacted values, files as `<file name, size, type>`."""
    match = _BOUNDARY_RE.search(content_type)
    if match is None:
        return [{"error": "multipart body without a boundary"}]
    delimiter = b"--" + match.group(1).encode("latin-1")
    fields: list[dict[str, str]] = []
    for segment in body.split(delimiter)[1:]:
        if segment.startswith(b"--"):
            break  # the closing delimiter
        segment = segment.removeprefix(b"\r\n")
        head, _, content = segment.partition(b"\r\n\r\n")
        content = content.removesuffix(b"\r\n")
        headers: dict[str, str] = {}
        for line in head.decode("utf-8", errors="replace").split("\r\n"):
            name, _, value = line.partition(":")
            if name.strip():
                headers[name.strip().lower()] = value.strip()
        disposition = headers.get("content-disposition", "")
        name_match = _NAME_RE.search(disposition)
        field_name = name_match.group(1) if name_match else "<unnamed>"
        file_match = _FILENAME_RE.search(disposition)
        if file_match is not None:
            part_type = headers.get("content-type", "application/octet-stream")
            # Chromium's request events omit uploaded file contents, so an empty
            # part means "not captured", not an empty file.
            size = f"{len(content)} bytes" if content else "size not captured"
            summary = f"<file {file_match.group(1)}, {size}, {part_type}>"
            fields.append({"name": field_name, "value": summary})
        else:
            fields.append({"name": field_name, "value": placeholder(content.decode("utf-8", errors="replace"))})
    return fields


def redact_body(body: bytes | None, content_type: str | None) -> Any:
    """Redact a request body by its content type, keeping only field names and value lengths.

    Returns None for an empty body, a list of `{name, value}` for form and
    multipart bodies, the redacted structure for JSON, and a summary string
    for anything else.
    """
    if not body:
        return None
    media_type = _content_type(content_type)
    if media_type == "multipart/form-data":
        return summarize_multipart(body, content_type or "")
    text = body.decode("utf-8", errors="replace")
    if media_type == "application/x-www-form-urlencoded":
        pairs = parse_qsl(text, keep_blank_values=True)
        return [{"name": key, "value": placeholder(value)} for key, value in pairs]
    if media_type.endswith("json") or media_type.endswith("+json"):
        try:
            return redact_json(json.loads(text))
        except ValueError:
            pass
    return f"<redacted body len={len(body)} type={media_type or 'unknown'}>"


def scrub_text(text: str) -> str:
    """Replace email addresses and Iranian mobile numbers in free text with placeholders."""
    text = _EMAIL_RE.sub(lambda match: placeholder(match.group()), text)
    return _PHONE_RE.sub(lambda match: placeholder(match.group()), text)


def redact_html(html: str) -> str:
    """Redact an HTML snapshot without changing its structure.

    Inline scripts are removed (external `<script src>` tags stay), form values
    (`<input value>`, `<textarea>` text) become placeholders except button
    labels, token-like `<meta>` contents are redacted, query strings in URL
    attributes are redacted, and email addresses and phone numbers are
    scrubbed from the rest.
    """
    tree = LexborHTMLParser(html)
    for attribute in _URL_ATTRIBUTES:
        for node in tree.css(f"[{attribute}]"):
            value = node.attributes.get(attribute) or ""
            if "?" in value or "#" in value or value.startswith(("data:", "blob:")):
                node.attrs[attribute] = redact_url(value)
    for script in tree.css("script"):
        if "src" not in script.attributes:
            script.decompose()
    for node in tree.css("input"):
        input_type = (node.attributes.get("type") or "text").lower()
        value = node.attributes.get("value")
        if value is not None and input_type not in _LABEL_INPUT_TYPES:
            node.attrs["value"] = placeholder(value)
    for node in tree.css("textarea"):
        content = node.text(deep=True)
        for child in list(node.iter(include_text=True)):
            child.decompose()
        if content:
            node.insert_child(placeholder(content))
    for node in tree.css("meta"):
        label = " ".join(node.attributes.get(key) or "" for key in ("name", "property", "http-equiv")).lower()
        content = node.attributes.get("content")
        if content is not None and any(part in label for part in _SENSITIVE_META_PARTS):
            node.attrs["content"] = placeholder(content)
    return scrub_text(tree.html or "")


# ── Capture session ────────────────────────────────────────────────────────


def _timestamp() -> str:
    """Return a UTC timestamp for directory names, e.g. 20261019T143000Z."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON to a temporary sibling, then rename it into place."""
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class ApplyCapture:
    """Records requests, responses, navigations, and snapshots of a manual application.

    Attach it to a browser context with `attach`, then call `snapshot` (or
    `run`, which also handles the `s`/`q` commands). Nothing here clicks,
    types, or submits.
    """

    platform: Platform
    out_dir: Path
    start_url: str
    events: list[dict[str, Any]] = field(default_factory=list)
    _started: float = field(default_factory=time.monotonic)
    _request_seq: dict[int, int] = field(default_factory=dict)
    _pending: set[asyncio.Task[None]] = field(default_factory=set)
    _snapshot_count: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ── Events ──────────────────────────────────────────────────────────────

    def _add(self, event_type: str, **data: Any) -> int:
        """Append a timeline event and return its sequence number."""
        seq = len(self.events) + 1
        elapsed = round(time.monotonic() - self._started, 3)
        self.events.append({"seq": seq, "t": elapsed, "type": event_type, **data})
        return seq

    def attach(self, context: BrowserContext) -> None:
        """Record every request and response of the context, and snapshot every page navigation."""
        context.on("request", self._on_request)
        context.on("response", self._on_response)
        context.on("requestfailed", self._on_request_failed)
        context.on("page", self._watch_page)
        for page in context.pages:
            self._watch_page(page)

    def _watch_page(self, page: Page) -> None:
        """Snapshot a page after each main-frame navigation."""

        def on_navigated(frame: Frame) -> None:
            if frame != page.main_frame:
                return
            self._add("navigation", url=redact_url(frame.url))
            task = asyncio.create_task(self._snapshot_after_load(page))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

        page.on("framenavigated", on_navigated)

    def _on_request(self, request: Request) -> None:
        """Record a request with redacted headers, query, and body."""
        headers = request.headers
        try:
            body = request.post_data_buffer
        except (PlaywrightError, UnicodeDecodeError):
            body = None
        seq = self._add(
            "request",
            method=request.method,
            url=redact_url(request.url),
            resource_type=request.resource_type,
            headers=redact_headers(headers),
            body=redact_body(body, headers.get("content-type")),
        )
        self._request_seq[id(request)] = seq

    def _on_response(self, response: Response) -> None:
        """Record a response's status and redacted headers."""
        self._add(
            "response",
            request_seq=self._request_seq.get(id(response.request)),
            status=response.status,
            url=redact_url(response.url),
            headers=redact_headers(response.headers),
        )

    def _on_request_failed(self, request: Request) -> None:
        """Record a request that failed (e.g. aborted or blocked)."""
        self._add(
            "request_failed",
            request_seq=self._request_seq.get(id(request)),
            url=redact_url(request.url),
            failure=request.failure,
        )

    # ── Snapshots ───────────────────────────────────────────────────────────

    async def _snapshot_after_load(self, page: Page) -> None:
        """Wait for a navigated page to load, then snapshot it."""
        try:
            await page.wait_for_load_state("load", timeout=_SNAPSHOT_LOAD_TIMEOUT_MS)
        except PlaywrightError:
            logger.debug("Page did not finish loading before its snapshot")
        await asyncio.sleep(_SNAPSHOT_SETTLE_S)
        await self.snapshot(page, "navigation")

    async def snapshot(self, page: Page, reason: str) -> None:
        """Save a redacted HTML copy and a screenshot (form fields masked) of a page."""
        if page.is_closed():
            return
        async with self._lock:
            self._snapshot_count += 1
            stem = f"{self._snapshot_count:03d}-{reason}"
            html_path = self.out_dir / f"{stem}.html"
            png_path = self.out_dir / f"{stem}.png"
            try:
                html = await page.content()
                title = await page.title()
                html_path.write_text(redact_html(html), encoding="utf-8")
                await page.screenshot(
                    path=str(png_path), full_page=True, mask=[page.locator("input, textarea, select")]
                )
            except PlaywrightError as exc:
                self._add("snapshot_failed", reason=reason, url=redact_url(page.url), error=type(exc).__name__)
                logger.warning("Snapshot {} failed ({})", stem, type(exc).__name__)
                return
            self._add(
                "snapshot",
                reason=reason,
                url=redact_url(page.url),
                title=scrub_text(title),
                html=html_path.name,
                screenshot=png_path.name,
            )
            self.write_index()
        print(f"  snapshot {stem} saved", flush=True)

    async def snapshot_all(self, context: BrowserContext, reason: str) -> None:
        """Snapshot every open page of the context."""
        for page in list(context.pages):
            await self.snapshot(page, reason)

    async def drain(self) -> None:
        """Wait for navigation snapshots that are still running."""
        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    def write_index(self) -> None:
        """Write `index.json`: the platform, the start URL, and the timeline so far."""
        _write_json_atomic(
            self.out_dir / "index.json",
            {
                "platform": self.platform,
                "start_url": redact_url(self.start_url),
                "written_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "redaction": "headers, query and body values, form values, inline scripts, emails, and phones",
                "events": self.events,
            },
        )

    async def run(self, context: BrowserContext, commands: asyncio.Queue[str]) -> Path:
        """Open the start URL and record until `q`, the queue's EOF (""), or the last page closing."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.attach(context)

        def on_close(_: Page) -> None:
            if not context.pages:
                commands.put_nowait("q")

        context.on("page", lambda new_page: new_page.on("close", on_close))
        page = await context.new_page()
        try:
            await page.goto(self.start_url)
        except PlaywrightError as exc:
            # A slow or challenged first load is not fatal: the user can navigate by hand.
            logger.warning("Opening the start URL failed ({}); navigate by hand", type(exc).__name__)
        while True:
            command = (await commands.get()).strip().lower()
            if command == "s":
                await self.snapshot_all(context, "manual")
            elif command in ("q", ""):
                break
            else:
                print("  type s + Enter for a snapshot, q + Enter to finish", flush=True)
        await self.drain()
        self._add("finished")
        self.write_index()
        return self.out_dir


# ── Browser and terminal ───────────────────────────────────────────────────


def auth_state_path(platform: Platform) -> Path:
    """Return the logged-in session file, kept apart from the scraping state."""
    return constants.BROWSER_STATE_DIR / f"{platform}_auth_state.json"


async def _new_context(browser: Browser, storage_state: Path | None) -> BrowserContext:
    """Create a context with the scrapers' browser settings and, optionally, the auth state."""
    options: dict[str, Any] = {
        "user_agent": constants.USER_AGENT,
        "viewport": {"width": constants.VIEWPORT_WIDTH, "height": constants.VIEWPORT_HEIGHT},
        "locale": constants.LOCALE,
        "timezone_id": constants.TIMEZONE_ID,
        "extra_http_headers": {"Accept-Language": constants.ACCEPT_LANGUAGE},
    }
    if storage_state is not None:
        options["storage_state"] = storage_state
    context = await browser.new_context(**options)
    context.set_default_timeout(constants.NAVIGATION_TIMEOUT_MS)
    return context


def _start_stdin_reader(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[str]) -> None:
    """Forward terminal lines to `queue` from a daemon thread ("" at EOF), so exit never waits on stdin."""

    def reader() -> None:
        while True:
            line = sys.stdin.readline()
            loop.call_soon_threadsafe(queue.put_nowait, line.strip() if line else "")
            if not line:
                return

    threading.Thread(target=reader, name="apply-probe-stdin", daemon=True).start()


def save_auth_state(state: dict[str, Any], path: Path) -> int:
    """Save a storage state readable only by the owner; return its cookie count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False)
    os.replace(tmp, path)
    return len(state.get("cookies") or [])


async def _login(platform: Platform) -> int:
    """Open the site in a visible browser, wait for a manual login, and save the session."""
    url = _BASE_URLS[platform]
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        try:
            context = await _new_context(browser, None)
            page = await context.new_page()
            await page.goto(url)
            print(f"\nLog in to {url} in the browser window (solve any challenge by hand).")
            print("When you see your account, come back here and press Enter.", flush=True)
            await asyncio.to_thread(sys.stdin.readline)
            count = save_auth_state(await context.storage_state(), auth_state_path(platform))
        finally:
            await browser.close()
    print(f"Saved the session to {auth_state_path(platform)} ({count} cookies).")
    return 0


async def _capture(platform: Platform, job_url: str) -> int:
    """Open a job with the saved session and record the manual application."""
    state = auth_state_path(platform)
    if not state.is_file():
        print(f"No saved session at {state}; run with --login first.", file=sys.stderr)
        return 1
    out_dir = constants.DEBUG_DIR / platform / _APPLY_DIR / _timestamp()
    capture = ApplyCapture(platform=platform, out_dir=out_dir, start_url=job_url)
    commands: asyncio.Queue[str] = asyncio.Queue()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        browser.on("disconnected", lambda _: commands.put_nowait("q"))
        try:
            context = await _new_context(browser, state)
            print(f"\nRecording to {out_dir}")
            print("Apply by hand in the browser. Type s + Enter for a snapshot at each step, q + Enter to finish.")
            _start_stdin_reader(asyncio.get_running_loop(), commands)
            await capture.run(context, commands)
        finally:
            if browser.is_connected():
                await browser.close()
    requests = sum(1 for event in capture.events if event["type"] == "request")
    snapshots = sum(1 for event in capture.events if event["type"] == "snapshot")
    print(f"Saved {requests} request(s) and {snapshots} snapshot(s) to {out_dir}")
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="python -m scripts.apply_probe",
        description="Log in by hand, then record a manual job application (never clicks or submits).",
    )
    parser.add_argument("--platform", required=True, choices=get_args(Platform))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--login", action="store_true", help="log in by hand and save the session")
    mode.add_argument("--job-url", metavar="URL", help="record a manual application starting at this job page")
    args = parser.parse_args(argv)
    if args.job_url is not None:
        host = (urlsplit(args.job_url).hostname or "").lower()
        site = _SITE_HOSTS[args.platform]
        if urlsplit(args.job_url).scheme != "https" or not (host == site or host.endswith(f".{site}")):
            parser.error(f"--job-url must be an https URL on {site}")
    return args


def main(argv: list[str] | None = None) -> int:
    """Run the chosen mode and return the process exit code."""
    args = _parse_args(argv)
    logger.remove()
    logger.add(sys.stderr, level="INFO", backtrace=False, diagnose=False)
    try:
        if args.login:
            return asyncio.run(_login(args.platform))
        return asyncio.run(_capture(args.platform, args.job_url))
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 1
    except PlaywrightError as exc:
        logger.error("Browser error: {}", type(exc).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
