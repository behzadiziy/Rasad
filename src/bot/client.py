"""Telegram Bot API client for the two-way bot: long polling, callback answers, messages, and documents.

Same approach as the notifier: one `httpx.AsyncClient`, optionally through
`TELEGRAM_PROXY_URL` (`trust_env=False`), retries with backoff for network
errors, 5xx, and 429 (honoring `retry_after`), and at least 1 s between sends.
Request URLs contain the bot token, so URLs and httpx exception texts are
never logged.

Bot API details (Bot API 10.3, https://core.telegram.org/bots/api; checked on
2026-10-19 against the machine-readable copy of those docs at
github.com/PaulSonOfLars/telegram-bot-api-spec, because core.telegram.org is
unreachable from this network):
- Responses are `{"ok", "result" | "description", "error_code", "parameters": {"retry_after"}}`.
- `getUpdates(offset, timeout, allowed_updates)`; HTTP 409 when another
  getUpdates poller or a webhook is active (messages from the official server,
  github.com/tdlib/telegram-bot-api, telegram-bot-api/Client.cpp).
- `answerCallbackQuery` text: 0-200 characters.
- `sendMessage` text: 1-4096 characters; `sendDocument` caption: 0-1024
  characters; documents are uploaded as multipart/form-data, where
  `reply_markup` is a JSON-serialized string.
- `editMessageReplyMarkup(chat_id, message_id, reply_markup)`; a 400 "message is
  not modified" means the keyboard was already the requested one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from loguru import logger
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from src.models import Settings

# httpx/httpcore log request URLs at INFO, and Bot API URLs contain the token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_API_BASE = "https://api.telegram.org"
_MAX_ATTEMPTS = 4
_BACKOFF = wait_exponential_jitter(max=30, jitter=1)
# A 429 asking for a longer wait is not retried.
_MAX_RETRY_AFTER_S = 60.0
_DEFAULT_TIMEOUT = httpx.Timeout(30.0)
# getUpdates holds the request open for the poll timeout; the read timeout must exceed it.
_POLL_READ_MARGIN_S = 15.0
_MAX_ANSWER_CHARS = 200
_NOT_MODIFIED = "message is not modified"
# Tenacity's sleep between attempts; tests replace it to avoid waiting.
_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
_CONFLICT_HINT = (
    "another bot instance is polling getUpdates, or a webhook is set. Stop the other instance "
    "(e.g. a second `main.py --bot` or `--daemon`), or delete the webhook with deleteWebhook"
)


class BotApiError(Exception):
    """A Bot API call that Telegram rejected (not retried), e.g. 400 "chat not found"."""

    def __init__(self, method: str, error_code: int, description: str) -> None:
        """Store the method, Telegram's error code, and its description."""
        super().__init__(f"{method} failed: {error_code} {description}")
        self.method = method
        self.error_code = error_code
        self.description = description


class BotConflictError(BotApiError):
    """HTTP 409 from getUpdates: another poller or a webhook is active."""


class _TransientError(Exception):
    """A network error or HTTP 5xx, worth retrying."""


class _RateLimitedError(_TransientError):
    """HTTP 429 with Telegram's `retry_after`."""

    def __init__(self, message: str, retry_after: float | None) -> None:
        """Store the server-requested wait in seconds (None if absent)."""
        super().__init__(message)
        self.retry_after = retry_after


def _wait(retry_state: RetryCallState) -> float:
    """Wait `retry_after` after a 429; otherwise back off exponentially with jitter."""
    outcome = retry_state.outcome
    exc = outcome.exception() if outcome is not None else None
    if isinstance(exc, _RateLimitedError) and exc.retry_after is not None:
        return exc.retry_after
    return _BACKOFF(retry_state)


def _log_retry(retry_state: RetryCallState) -> None:
    """Log a WARNING before each retry sleep (never the URL)."""
    outcome = retry_state.outcome
    exc = outcome.exception() if outcome is not None else None
    delay = retry_state.next_action.sleep if retry_state.next_action is not None else 0.0
    logger.warning(
        "Telegram call attempt {}/{} failed ({}); retrying in {:.1f}s",
        retry_state.attempt_number,
        _MAX_ATTEMPTS,
        exc,
        delay,
    )


class TelegramBotClient:
    """Bot API client; use as an async context manager.

    Example:
        async with TelegramBotClient(settings, poll_timeout_seconds=30) as client:
            updates = await client.get_updates(offset=None, allowed_updates=["message"])
    """

    def __init__(
        self,
        settings: Settings,
        *,
        poll_timeout_seconds: int = 30,
        min_send_interval: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Store the settings; the HTTP client opens in `__aenter__`.

        Args:
            settings: Provides the bot token and `TELEGRAM_PROXY_URL`.
            poll_timeout_seconds: The long-polling timeout for `get_updates`.
            min_send_interval: Minimum seconds between `send_message`/`send_document` calls.
            transport: Optional httpx transport; tests pass an `httpx.MockTransport`.
        """
        self._settings = settings
        self._base_url = f"{_API_BASE}/bot{settings.TELEGRAM_BOT_TOKEN}"
        self._poll_timeout = poll_timeout_seconds
        self._min_send_interval = min_send_interval
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._send_lock = asyncio.Lock()
        self._last_sent_at: float | None = None

    async def __aenter__(self) -> TelegramBotClient:
        """Open the HTTP client, through `TELEGRAM_PROXY_URL` when it is set."""
        proxy = self._settings.TELEGRAM_PROXY_URL
        scheme = urlsplit(proxy).scheme if proxy else ""
        try:
            self._client = httpx.AsyncClient(
                proxy=proxy, transport=self._transport, timeout=_DEFAULT_TIMEOUT, trust_env=False
            )
        except ValueError:
            # httpx's message would include the proxy URL, which may contain credentials.
            raise ValueError(
                f"Unsupported TELEGRAM_PROXY_URL scheme {scheme!r}; use http, https, socks5 or socks5h"
            ) from None
        logger.info("Telegram bot client ready ({})", f"via {scheme} proxy" if proxy else "direct connection")
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── Public methods ──────────────────────────────────────────────────────

    async def get_updates(self, offset: int | None, allowed_updates: Sequence[str]) -> list[dict[str, Any]]:
        """Long-poll for updates starting at `offset` (one call, no retries; the poll loop backs off).

        Raises:
            BotConflictError: If another poller or a webhook is active (HTTP 409).
            BotApiError: If Telegram rejects the call, or on a network error, 5xx, or 429.
        """
        payload: dict[str, Any] = {"timeout": self._poll_timeout, "allowed_updates": list(allowed_updates)}
        if offset is not None:
            payload["offset"] = offset
        timeout = httpx.Timeout(30.0, read=self._poll_timeout + _POLL_READ_MARGIN_S)
        try:
            result = await self._request("getUpdates", json_body=payload, timeout=timeout)
        except _TransientError as exc:
            raise BotApiError("getUpdates", 0, str(exc)) from None
        if not isinstance(result, list):
            raise BotApiError("getUpdates", 0, "result is not a list")
        return [update for update in result if isinstance(update, dict)]

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        """Answer a callback query, showing `text` (at most 200 characters) as a toast."""
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:_MAX_ANSWER_CHARS]
        await self._call("answerCallbackQuery", json_body=payload)

    async def send_message(
        self, chat_id: int | str, text: str, *, reply_markup: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send an HTML message (link previews off) and return the sent Message."""
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        async with self._send_lock:
            await self._pace()
            try:
                result = await self._call("sendMessage", json_body=payload)
            finally:
                self._last_sent_at = time.monotonic()
        return result if isinstance(result, dict) else {}

    async def send_document(
        self,
        chat_id: int | str,
        path: Path,
        *,
        filename: str | None = None,
        caption: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Upload a file (multipart/form-data) with an optional HTML caption; return the sent Message."""
        content = path.read_bytes()
        form: dict[str, str] = {"chat_id": str(chat_id)}
        if caption:
            form["caption"] = caption
            form["parse_mode"] = "HTML"
        if reply_markup is not None:
            form["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        files = {"document": (filename or path.name, content, "application/pdf")}
        async with self._send_lock:
            await self._pace()
            try:
                result = await self._call("sendDocument", form=form, files=files)
            finally:
                self._last_sent_at = time.monotonic()
        logger.debug("Sent document to chat {} ({} bytes)", chat_id, len(content))
        return result if isinstance(result, dict) else {}

    async def edit_message_reply_markup(
        self, chat_id: int | str, message_id: int, reply_markup: dict[str, Any] | None
    ) -> None:
        """Replace a message's inline keyboard (None removes it); "not modified" counts as success."""
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
        payload["reply_markup"] = reply_markup if reply_markup is not None else {"inline_keyboard": []}
        try:
            await self._call("editMessageReplyMarkup", json_body=payload)
        except BotApiError as exc:
            if _NOT_MODIFIED in exc.description:
                logger.debug("Keyboard of message {} in chat {} was already up to date", message_id, chat_id)
                return
            raise

    # ── Internals ───────────────────────────────────────────────────────────

    async def _pace(self) -> None:
        """Sleep until `min_send_interval` has passed since the previous send."""
        if self._last_sent_at is None:
            return
        remaining = self._min_send_interval - (time.monotonic() - self._last_sent_at)
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def _call(
        self,
        method: str,
        *,
        json_body: dict[str, Any] | None = None,
        form: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> Any:
        """Call a method with retries for network errors, 5xx, and 429."""
        retrying = AsyncRetrying(
            stop=stop_after_attempt(_MAX_ATTEMPTS),
            wait=_wait,
            retry=retry_if_exception_type(_TransientError),
            before_sleep=_log_retry,
            sleep=_sleep,
            reraise=True,
        )
        try:
            return await retrying(self._request, method, json_body=json_body, form=form, files=files)
        except _TransientError as exc:
            raise BotApiError(method, 0, f"gave up after {_MAX_ATTEMPTS} attempts ({exc})") from None

    async def _request(
        self,
        method: str,
        *,
        json_body: dict[str, Any] | None = None,
        form: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> Any:
        """Make one request and return Telegram's `result`, or raise a classified error."""
        if self._client is None:
            raise RuntimeError("TelegramBotClient is not open; use 'async with TelegramBotClient(...)'")
        url = f"{self._base_url}/{method}"
        try:
            if files is not None:
                response = await self._client.post(
                    url, data=form, files=files, timeout=timeout or _DEFAULT_TIMEOUT
                )
            else:
                response = await self._client.post(url, json=json_body, timeout=timeout or _DEFAULT_TIMEOUT)
        except httpx.TransportError as exc:
            # The exception text may include the request URL, which holds the token.
            raise _TransientError(f"{method}: network error ({type(exc).__name__})") from None

        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            body = {}
        description = str(body.get("description") or response.reason_phrase or "unknown error")
        status = response.status_code

        if status == 409 and method == "getUpdates":
            raise BotConflictError(method, status, f"{description} ({_CONFLICT_HINT})")
        if status == 429:
            parameters = body.get("parameters")
            raw = parameters.get("retry_after") if isinstance(parameters, dict) else None
            retry_after = float(raw) if isinstance(raw, int | float) else None
            if retry_after is not None and retry_after > _MAX_RETRY_AFTER_S:
                raise BotApiError(method, status, f"{description} (retry_after={retry_after:.0f}s exceeds the cap)")
            raise _RateLimitedError(f"{method}: 429 {description}", retry_after)
        if status >= 500:
            raise _TransientError(f"{method}: {status} {description}")
        if status >= 400:
            raise BotApiError(method, status, description)
        if body.get("ok") is not True:
            raise BotApiError(method, status, f"unexpected response: {description}")
        return body.get("result")
