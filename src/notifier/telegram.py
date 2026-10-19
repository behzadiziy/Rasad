"""Telegram notifier: sends one HTML-formatted message per job via the Bot API.

Delivery goes through a single `httpx.AsyncClient`, optionally via a proxy
(`TELEGRAM_PROXY_URL`, e.g. `socks5://127.0.0.1:1080`, which Iranian servers
need). Network errors and 5xx responses are retried with exponential backoff,
HTTP 429 waits for Telegram's `retry_after`, and other 4xx errors fail at once.
`send_job` never raises for API or network failures; it returns False instead.
A job with probable duplicates gets a «🔁 احتمالاً تکراری» line that links to
the other postings.

Smoke test (sends a real message): `uv run python -m src.notifier.telegram`
"""

from __future__ import annotations

import asyncio
import html
import logging
import sys
import time
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx
from loguru import logger
from pydantic import ValidationError
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from src.bot.callbacks import encode_callback, job_ref
from src.bot.messages import TAILOR_BUTTON_TEXT
from src.models import JobPost, Platform, Settings, WorkMode

# httpx/httpcore log each request URL at INFO, and Telegram URLs contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_API_BASE = "https://api.telegram.org"
_MAX_ATTEMPTS = 5
_MIN_SEND_INTERVAL_S = 1.0
# A 429 asking for a longer wait is not retried; the job stays NEW for the next run.
_MAX_RETRY_AFTER_S = 300.0
_BACKOFF = wait_exponential_jitter(initial=1, max=30, jitter=1)

# Telegram's limit is 4096 characters per message. Fields are truncated *before*
# escaping and rendering, so the HTML is never cut. Worst case: the caps below sum
# to 540 characters; html.escape expands one character to at most 6 (`&quot;`),
# giving 3240, plus under 300 characters of fixed template text: < 4096. The
# duplicates line comes last and only gets the space that is left.
_MAX_MESSAGE_CHARS = 4096
_TITLE_MAX = 200
_COMPANY_MAX = 100
_CITY_MAX = 60
_SALARY_MAX = 60
_PROFILES_MAX = 120

_UNKNOWN = "نامشخص"
_ELLIPSIS = "…"

_WORK_MODE_BADGES: dict[WorkMode, str] = {
    WorkMode.REMOTE: "🏠 دورکاری",
    WorkMode.ONSITE: "🏢 حضوری",
    WorkMode.HYBRID: "🔀 هیبریدی",
}

_PLATFORM_NAMES: dict[Platform, str] = {
    "jobvision": "JobVision",
    "jobinja": "Jobinja",
}

_VIEW_BUTTON_TEXT = "🔗 مشاهده آگهی در سایت"

_DUPLICATES_LABEL = "🔁 احتمالاً تکراری"
_DUPLICATES_SEPARATOR = "، "
# Other URLs (never expected from the scrapers) are shown as plain text, not as links.
_LINK_SCHEMES = frozenset({"http", "https"})


class _TelegramError(Exception):
    """Base class for Telegram Bot API failures."""


class _RateLimitedError(_TelegramError):
    """HTTP 429: Telegram asks to wait `retry_after` seconds before retrying."""

    def __init__(self, retry_after: float | None, description: str) -> None:
        """Store the server-requested wait (None if the response did not include one)."""
        super().__init__(f"429 Too Many Requests (retry_after={retry_after}): {description}")
        self.retry_after = retry_after


class _ServerError(_TelegramError):
    """HTTP 5xx: a transient server-side failure worth retrying."""

    def __init__(self, status_code: int, description: str) -> None:
        """Store the status code and Telegram's description."""
        super().__init__(f"HTTP {status_code}: {description}")
        self.status_code = status_code


class _ApiError(_TelegramError):
    """A non-retryable API error, such as 400 (bad chat ID) or 401/403."""

    def __init__(self, status_code: int, description: str) -> None:
        """Store the status code and Telegram's description."""
        super().__init__(f"HTTP {status_code}: {description}")
        self.status_code = status_code


_RETRYABLE = (httpx.TransportError, _ServerError, _RateLimitedError)


def _wait(retry_state: RetryCallState) -> float:
    """Wait `retry_after` seconds after a 429, and use exponential backoff with jitter otherwise."""
    outcome = retry_state.outcome
    exc = outcome.exception() if outcome is not None else None
    if isinstance(exc, _RateLimitedError) and exc.retry_after is not None:
        return exc.retry_after
    return _BACKOFF(retry_state)


def _log_retry(retry_state: RetryCallState) -> None:
    """Log a WARNING before each retry sleep (never includes the request URL)."""
    outcome = retry_state.outcome
    exc = outcome.exception() if outcome is not None else None
    delay = retry_state.next_action.sleep if retry_state.next_action is not None else 0.0
    logger.warning(
        "Telegram send attempt {}/{} failed ({}: {}); retrying in {:.1f}s",
        retry_state.attempt_number,
        _MAX_ATTEMPTS,
        type(exc).__name__,
        exc,
        delay,
    )


def _truncate(text: str, limit: int) -> str:
    """Shorten `text` to at most `limit` characters, ending with an ellipsis when cut."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + _ELLIPSIS


def _field(text: str | None, limit: int, *, default: str = _UNKNOWN) -> str:
    """Truncate a raw field, then HTML-escape it; blank values become `default`."""
    raw = (text or "").strip()
    return html.escape(_truncate(raw, limit) if raw else default)


def _duplicate_link(job: JobPost) -> str:
    """Render a link to a posting, labeled with its platform name; plain text if the URL is not http(s)."""
    name = html.escape(_PLATFORM_NAMES[job.platform])
    url = job.url.strip()
    if urlsplit(url).scheme.lower() not in _LINK_SCHEMES:
        return name
    return f'<a href="{html.escape(url, quote=True)}">{name}</a>'


def _more_postings(count: int, *, after_links: bool) -> str:
    """Render the summary of postings left out for lack of space ("" when none are left out)."""
    if not count:
        return ""
    return f" و {count} آگهی دیگر" if after_links else f" {count} آگهی"


def _render_duplicates(job: JobPost, duplicates: Sequence[JobPost], budget: int) -> str:
    """Render the «🔁 احتمالاً تکراری» line with links to the other postings, in at most `budget` characters.

    The job itself and repeated postings are skipped; "" means no other
    posting is left. Links that do not fit are counted instead («و 2 آگهی
    دیگر»), so the message never exceeds Telegram's limit.
    """
    seen = {(job.platform, job.external_id)}
    others: list[JobPost] = []
    for other in duplicates:
        key = (other.platform, other.external_id)
        if key not in seen:
            seen.add(key)
            others.append(other)
    if not others:
        return ""

    line = f"{_DUPLICATES_LABEL}:"
    if len(line) + len(_more_postings(len(others), after_links=False)) > budget:
        logger.warning("No room for the duplicates line of job {}:{}", job.platform, job.external_id)
        return ""
    # Invariant: `line` plus the summary of the postings not yet shown fits in `budget`.
    for shown, other in enumerate(others):
        extended = f"{line}{_DUPLICATES_SEPARATOR if shown else ' '}{_duplicate_link(other)}"
        left = len(others) - shown - 1
        if len(extended) + len(_more_postings(left, after_links=True)) > budget:
            return line + _more_postings(len(others) - shown, after_links=bool(shown))
        line = extended
    return line


def _render_message(job: JobPost, duplicates: Sequence[JobPost] = ()) -> str:
    """Render the HTML message body for a job (always within Telegram's 4096-character limit).

    When `duplicates` lists other postings of the same position, a last line
    «🔁 احتمالاً تکراری: JobVision، Jobinja» links to each of them.
    """
    lines = [
        f"🆕 <b>{_field(job.title, _TITLE_MAX)}</b>",
        f"🏛 {_field(job.company, _COMPANY_MAX)}",
        f"📍 {_field(job.city, _CITY_MAX)}",
        _WORK_MODE_BADGES[job.work_mode],
    ]
    if job.salary and job.salary.strip():
        lines.append(f"💰 {_field(job.salary, _SALARY_MAX)}")
    profiles = "، ".join(job.matched_profiles)
    lines.append(f"🎯 پروفایل‌ها: {_field(profiles, _PROFILES_MAX, default='—')}")
    lines.append(f"🌐 منبع: {html.escape(_PLATFORM_NAMES[job.platform])}")
    text = "\n".join(lines)
    if duplicates:
        duplicates_line = _render_duplicates(job, duplicates, _MAX_MESSAGE_CHARS - len(text) - 1)
        if duplicates_line:
            text = f"{text}\n{duplicates_line}"
    return text


def _build_keyboard(job: JobPost, *, tailor_button: bool = False) -> dict[str, Any]:
    """Build the inline keyboard: the URL button, plus «✍️ تنظیم رزومه» next to it when `tailor_button`."""
    row: list[dict[str, str]] = [{"text": _VIEW_BUTTON_TEXT, "url": job.url}]

    # The review bot (src/bot/) handles the `t:{jv|jb}:{external_id}` callback;
    # src/bot/messages.py rebuilds this same keyboard to show progress on it.
    if tailor_button:
        try:
            data = encode_callback("t", job_ref(job.platform, job.external_id))
        except ValueError:
            logger.warning("No tailor button for job {}:{}: callback data too long", job.platform, job.external_id)
        else:
            row.append({"text": TAILOR_BUTTON_TEXT, "callback_data": data})

    return {"inline_keyboard": [row]}


def _describe(response: httpx.Response) -> tuple[dict[str, Any], str]:
    """Return the response's JSON body (or {}) and a human-readable error description."""
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return {}, response.reason_phrase or "non-JSON response"
    return body, str(body.get("description") or response.reason_phrase or "unknown error")


class TelegramNotifier:
    """Sends job notifications to a Telegram chat; use as an async context manager.

    Example:
        async with TelegramNotifier(settings) as notifier:
            delivered = await notifier.send_job(job)
    """

    def __init__(self, settings: Settings) -> None:
        """Store settings; the HTTP client is created in `__aenter__`."""
        self._settings = settings
        self._send_url = f"{_API_BASE}/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._last_sent_at: float | None = None

    async def __aenter__(self) -> TelegramNotifier:
        """Open the HTTP client, through `TELEGRAM_PROXY_URL` when it is set."""
        proxy = self._settings.TELEGRAM_PROXY_URL
        scheme = urlsplit(proxy).scheme if proxy else ""
        try:
            # trust_env=False: TELEGRAM_PROXY_URL is the only proxy source, so an empty value means a
            # direct connection and desktop variables such as ALL_PROXY=socks://... cannot interfere.
            self._client = httpx.AsyncClient(proxy=proxy, timeout=httpx.Timeout(30.0), trust_env=False)
        except ValueError:
            # httpx's message would include the proxy URL, which may contain credentials.
            raise ValueError(
                f"Unsupported TELEGRAM_PROXY_URL scheme {scheme!r}; use http, https, socks5 or socks5h"
            ) from None
        if proxy:
            logger.info("Telegram notifier ready (via {} proxy)", scheme or "unknown")
        else:
            logger.info("Telegram notifier ready (direct connection)")
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.debug("Telegram notifier closed")

    async def send_job(self, job: JobPost, *, duplicates: Sequence[JobPost] = ()) -> bool:
        """Send one job notification.

        Consecutive sends are spaced at least 1.0 s apart, so callers can loop
        without sleeping.

        Args:
            job: The job to announce.
            duplicates: Other postings of the same position (any status);
                when not empty, the message links to them under «🔁 احتمالاً تکراری».

        Returns:
            True if Telegram accepted the message, False after any API or
            network failure (logged once at ERROR).
        """
        if self._client is None:
            raise RuntimeError("TelegramNotifier is not open; use 'async with TelegramNotifier(...)'")

        payload = {
            "chat_id": self._settings.TELEGRAM_CHAT_ID,
            "text": _render_message(job, duplicates),
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
            "reply_markup": _build_keyboard(job, tailor_button=bool(self._settings.allowed_user_ids)),
        }
        job_key = f"{job.platform}:{job.external_id}"

        async with self._lock:
            await self._wait_for_pacing()
            try:
                await self._send_with_retries(payload)
            except _ApiError as exc:
                logger.error("Telegram rejected job {} (not retried): {}", job_key, exc)
                return False
            except _RETRYABLE as exc:
                logger.error(
                    "Telegram send failed for job {} after {} attempts: {}: {}",
                    job_key,
                    _MAX_ATTEMPTS,
                    type(exc).__name__,
                    exc,
                )
                return False
            except httpx.HTTPError as exc:
                logger.error("Telegram send failed for job {}: {}: {}", job_key, type(exc).__name__, exc)
                return False
            finally:
                self._last_sent_at = time.monotonic()

        if duplicates:
            logger.info(
                "Sent Telegram notification for job {} (with {} probable duplicate(s))", job_key, len(duplicates)
            )
        else:
            logger.info("Sent Telegram notification for job {}", job_key)
        return True

    async def _wait_for_pacing(self) -> None:
        """Sleep until at least `_MIN_SEND_INTERVAL_S` has passed since the previous send."""
        if self._last_sent_at is None:
            return
        remaining = _MIN_SEND_INTERVAL_S - (time.monotonic() - self._last_sent_at)
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def _send_with_retries(self, payload: dict[str, Any]) -> None:
        """POST `sendMessage`, retrying network errors, 5xx, and 429 up to `_MAX_ATTEMPTS` in total."""
        retrying = AsyncRetrying(
            stop=stop_after_attempt(_MAX_ATTEMPTS),
            wait=_wait,
            retry=retry_if_exception_type(_RETRYABLE),
            before_sleep=_log_retry,
            reraise=True,
        )
        async for attempt in retrying:
            with attempt:
                await self._post_send_message(payload)

    async def _post_send_message(self, payload: dict[str, Any]) -> None:
        """Make one `sendMessage` request and raise a classified error on failure."""
        if self._client is None:
            raise RuntimeError("TelegramNotifier is not open")
        response = await self._client.post(self._send_url, json=payload)
        body, description = _describe(response)
        status = response.status_code

        if status == 429:
            parameters = body.get("parameters")
            retry_after_raw = parameters.get("retry_after") if isinstance(parameters, dict) else None
            retry_after = float(retry_after_raw) if isinstance(retry_after_raw, int | float) else None
            if retry_after is not None and retry_after > _MAX_RETRY_AFTER_S:
                raise _ApiError(status, f"{description} (retry_after={retry_after:.0f}s exceeds the cap)")
            raise _RateLimitedError(retry_after, description)
        if status >= 500:
            raise _ServerError(status, description)
        if status >= 400:
            raise _ApiError(status, description)
        if body.get("ok") is not True:
            raise _ApiError(status, f"unexpected response: {description}")


async def _main() -> int:
    """Send one sample job to the configured chat; return the process exit code."""
    try:
        settings = Settings()
    except ValidationError as exc:
        logger.error("Cannot load settings from config/.env: {}", exc)
        return 1

    job = JobPost(
        platform="jobinja",
        external_id="smoke-test-123",
        title="Senior AI & ML Engineer <Test>",
        company="Job Hunter Agent",
        city="مشهد",
        work_mode=WorkMode.REMOTE,
        salary="توافقی",
        url="https://example.com/job/123",
        matched_profiles=["AI & Automation"],
    )

    try:
        async with TelegramNotifier(settings) as notifier:
            delivered = await notifier.send_job(job)
    except ValueError as exc:
        logger.error("Cannot start the Telegram notifier: {}", exc)
        return 1

    if delivered:
        logger.success("Smoke test passed: the test message was delivered")
        return 0
    logger.error("Smoke test failed: the test message was not delivered")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
