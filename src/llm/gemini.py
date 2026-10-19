"""Google Gemini client: structured JSON generation over the REST API with httpx.

API details and where they were verified (2026-10-19). The ai.google.dev pages
answered HTTP 403 from this network, so each detail was confirmed against
Google's official API definitions in github.com/googleapis/googleapis
(google/ai/generativelanguage/v1beta/generative_service.proto and content.proto)
and the official SDK github.com/googleapis/python-genai (google/genai/_api_client.py):

- Endpoint: `POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`
  https://ai.google.dev/api/generate-content
- API key: only in the `x-goog-api-key` header, never in the URL.
  https://ai.google.dev/api#authentication
- JSON Schema output: `generationConfig.responseJsonSchema` together with
  `responseMimeType: "application/json"`. Only a subset of JSON Schema is
  supported, and a `$ref` sub-schema may carry no other keywords (see
  `to_gemini_json_schema`). https://ai.google.dev/gemini-api/docs/structured-output
- Thought parts: `Part.thought == true`; they are skipped.
  https://ai.google.dev/gemini-api/docs/thinking
- Finish reasons: `Candidate.finishReason` (STOP, MAX_TOKENS, SAFETY, RECITATION,
  LANGUAGE, OTHER, BLOCKLIST, PROHIBITED_CONTENT, SPII, ...). A blocked prompt has
  `promptFeedback.blockReason` and no candidates.
- Usage: `usageMetadata.promptTokenCount`, `candidatesTokenCount`,
  `thoughtsTokenCount`, `totalTokenCount`.
- Errors: `{"error": {"code", "message", "status", "details": [...]}}` (AIP-193,
  https://google.aip.dev/193). A 429 may carry a `type.googleapis.com/google.rpc.RetryInfo`
  detail whose `retryDelay` is a protobuf Duration string such as "36s".
  https://ai.google.dev/gemini-api/docs/troubleshooting and
  https://ai.google.dev/gemini-api/docs/rate-limits

Prompts, responses, and the API key are never logged; INFO lines carry only the
model, latency, and token counts.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
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

from src.llm.base import LLMClient, LLMError, LLMResult, LLMUsage, T
from src.models import LLMConfig

# Request URLs are harmless (the key is in a header), but keep the transport quiet.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
_MAX_ATTEMPTS = 4
_BACKOFF = wait_exponential_jitter(multiplier=2, max=30, jitter=2)
# A 429 that asks for a longer wait (e.g. an exhausted daily quota) fails at once.
_MAX_RETRY_DELAY_S = 90.0
_MAX_ERROR_MESSAGE_CHARS = 500
_RETRY_INFO_TYPE = "type.googleapis.com/google.rpc.RetryInfo"
_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)s\s*$")
# Tenacity's sleep between attempts; tests replace it to record delays without waiting.
_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

# The JSON Schema keywords that responseJsonSchema supports, per
# GenerationConfig.response_json_schema in generative_service.proto
# (https://ai.google.dev/gemini-api/docs/structured-output). `$id`, `$defs`,
# `$ref`, and `$anchor` are also supported, but references are inlined here,
# and `title` is dropped because Pydantic's generated titles only add tokens.
_SUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "type",
        "format",
        "description",
        "enum",
        "items",
        "prefixItems",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "anyOf",
        "oneOf",
        "properties",
        "additionalProperties",
        "required",
        "propertyOrdering",
    }
)
_SCHEMA_LIST_KEYS = ("prefixItems", "anyOf", "oneOf")


class _TransientError(LLMError):
    """A failure worth retrying: a network error or an HTTP 5xx."""


class _RateLimitedError(_TransientError):
    """HTTP 429, with the server's requested delay when the error details include one."""

    def __init__(self, message: str, retry_delay: float | None) -> None:
        """Store the message and the requested delay in seconds (None if absent)."""
        super().__init__(message)
        self.retry_delay = retry_delay


def to_gemini_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Adapt a Pydantic `model_json_schema()` to the JSON Schema subset Gemini supports.

    - `$ref`s are replaced by their `$defs` entries, because Gemini forbids other
      keywords next to `$ref` and Pydantic puts field descriptions there.
    - `const` becomes a one-value `enum`.
    - Unsupported keywords (`default`, `title`, `maxLength`, `pattern`, ...) are dropped.
    - Every object gets `propertyOrdering` in declaration order, so the model
      writes fields in the order the schema lists them.

    Raises:
        ValueError: If the schema has a cyclic or unknown `$ref`.
    """
    definitions: dict[str, Any] = schema.get("$defs", {})

    def resolve(node: Any, stack: tuple[str, ...]) -> Any:
        """Return a cleaned copy of one schema node."""
        if isinstance(node, list):
            return [resolve(item, stack) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            ref = node["$ref"]
            name = ref.rsplit("/", 1)[-1]
            if not ref.startswith("#/$defs/") or name not in definitions:
                raise ValueError(f"Unsupported JSON Schema reference {ref!r}")
            if name in stack:
                raise ValueError(f"Cyclic JSON Schema reference {ref!r} cannot be inlined")
            merged = dict(definitions[name])
            # Keep sibling annotations such as the field description.
            merged.update({key: value for key, value in node.items() if key != "$ref"})
            return resolve(merged, (*stack, name))

        cleaned: dict[str, Any] = {}
        for key, value in node.items():
            if key == "const":
                cleaned["enum"] = [value]
            elif key in ("properties",):
                cleaned[key] = {prop: resolve(sub, stack) for prop, sub in value.items()}
            elif key in ("items", "additionalProperties"):
                cleaned[key] = resolve(value, stack) if isinstance(value, dict) else value
            elif key in _SCHEMA_LIST_KEYS:
                cleaned[key] = [resolve(sub, stack) for sub in value]
            elif key in _SUPPORTED_SCHEMA_KEYS:
                cleaned[key] = value
        if "properties" in cleaned and "propertyOrdering" not in cleaned:
            cleaned["propertyOrdering"] = list(cleaned["properties"])
        return cleaned

    root = {key: value for key, value in schema.items() if key != "$defs"}
    result = resolve(root, ())
    if not isinstance(result, dict):
        raise ValueError("A JSON Schema must be an object")
    return result


def _parse_duration(value: Any) -> float | None:
    """Parse a protobuf Duration JSON string such as "36s" or "1.5s" into seconds."""
    if not isinstance(value, str):
        return None
    match = _DURATION_RE.match(value)
    return float(match.group(1)) if match else None


def _retry_delay(error: dict[str, Any]) -> float | None:
    """Return the `RetryInfo.retryDelay` of an API error in seconds, or None."""
    details = error.get("details")
    if not isinstance(details, list):
        return None
    for detail in details:
        if isinstance(detail, dict) and detail.get("@type") == _RETRY_INFO_TYPE:
            return _parse_duration(detail.get("retryDelay"))
    return None


def _api_error(response: httpx.Response) -> tuple[dict[str, Any], str]:
    """Return the `error` object of an error response and a short description of it.

    The description holds the HTTP code, the gRPC status, and the API's own
    message (truncated); never the request.
    """
    try:
        body = response.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return {}, f"HTTP {response.status_code} (no JSON error body)"
    status = error.get("status") or "UNKNOWN"
    message = str(error.get("message") or "").strip()
    if len(message) > _MAX_ERROR_MESSAGE_CHARS:
        message = message[: _MAX_ERROR_MESSAGE_CHARS - 1] + "…"
    return error, f"HTTP {response.status_code} {status}: {message or 'no message'}"


def _wait(retry_state: RetryCallState) -> float:
    """Wait the server's `retryDelay` after a 429 when it sent one; otherwise back off with jitter."""
    outcome = retry_state.outcome
    exc = outcome.exception() if outcome is not None else None
    if isinstance(exc, _RateLimitedError) and exc.retry_delay is not None:
        return exc.retry_delay
    return _BACKOFF(retry_state)


def _log_retry(retry_state: RetryCallState) -> None:
    """Log a WARNING before each retry sleep, naming only the failure type."""
    outcome = retry_state.outcome
    exc = outcome.exception() if outcome is not None else None
    delay = retry_state.next_action.sleep if retry_state.next_action is not None else 0.0
    logger.warning(
        "Gemini attempt {}/{} failed ({}); retrying in {:.1f}s",
        retry_state.attempt_number,
        _MAX_ATTEMPTS,
        exc,
        delay,
    )


def _usage(metadata: Any) -> LLMUsage:
    """Convert `usageMetadata` to `LLMUsage`; thinking tokens count as output tokens."""
    if not isinstance(metadata, dict):
        return LLMUsage()
    prompt = int(metadata.get("promptTokenCount") or 0)
    output = int(metadata.get("candidatesTokenCount") or 0) + int(metadata.get("thoughtsTokenCount") or 0)
    total = int(metadata.get("totalTokenCount") or 0) or prompt + output
    return LLMUsage(prompt_tokens=prompt, output_tokens=output, total_tokens=total)


def _answer_text(body: dict[str, Any]) -> str:
    """Return the joined text of the first candidate's non-thought parts.

    Raises:
        LLMError: If the prompt was blocked, there is no candidate, the
            candidate did not finish with STOP, or it has no text.
    """
    candidates = body.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        feedback = body.get("promptFeedback")
        reason = feedback.get("blockReason") if isinstance(feedback, dict) else None
        if reason:
            raise LLMError(f"Gemini blocked the prompt (blockReason={reason})")
        raise LLMError("Gemini returned no candidates")

    candidate = candidates[0] if isinstance(candidates[0], dict) else {}
    content = candidate.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    texts = [
        part["text"]
        for part in parts or []
        if isinstance(part, dict) and not part.get("thought") and isinstance(part.get("text"), str)
    ]
    text = "".join(texts)

    finish_reason = candidate.get("finishReason")
    if finish_reason != "STOP":
        raise LLMError(f"Gemini stopped with finishReason={finish_reason or 'MISSING'} instead of STOP")
    if not text.strip():
        raise LLMError("Gemini returned an empty answer")
    return text


class GeminiClient(LLMClient):
    """`LLMClient` for the Google Gemini REST API.

    Example:
        async with GeminiClient(config.llm, api_key=settings.GEMINI_API_KEY) as llm:
            result = await llm.generate_structured(system=..., user=..., schema=MyModel)
    """

    def __init__(
        self,
        config: LLMConfig,
        *,
        api_key: str,
        proxy_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Store the call settings; the HTTP client opens in `__aenter__`.

        Args:
            config: The `llm` config section (model, temperature, limits, timeout).
            api_key: The Gemini API key, sent only in the `x-goog-api-key` header.
            proxy_url: Optional proxy for API calls (`LLM_PROXY_URL`).
            transport: Optional httpx transport; tests pass an `httpx.MockTransport`.

        Raises:
            ValueError: If the API key is blank.
        """
        if not api_key or not api_key.strip():
            raise ValueError("GEMINI_API_KEY is empty")
        self._config = config
        self._api_key = api_key.strip()
        self._proxy_url = proxy_url
        self._transport = transport
        self._model = config.model.removeprefix("models/")
        self._client: httpx.AsyncClient | None = None

    async def _open(self) -> None:
        """Open the HTTP client, through the proxy when one is set."""
        scheme = urlsplit(self._proxy_url).scheme if self._proxy_url else ""
        try:
            # trust_env=False: LLM_PROXY_URL is the only proxy source.
            self._client = httpx.AsyncClient(
                proxy=self._proxy_url,
                transport=self._transport,
                trust_env=False,
                timeout=self._config.timeout_seconds,
                headers={"x-goog-api-key": self._api_key},
            )
        except ValueError:
            # httpx's message would include the proxy URL, which may contain credentials.
            raise ValueError(
                f"Unsupported LLM_PROXY_URL scheme {scheme!r}; use http, https, socks5 or socks5h"
            ) from None
        logger.info(
            "Gemini client ready: model {} ({})",
            self._model,
            f"via {scheme} proxy" if self._proxy_url else "direct connection",
        )

    async def _close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _request_body(self, system: str, user: str, schema: type[T]) -> dict[str, Any]:
        """Build the `generateContent` request body."""
        return {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": self._config.temperature,
                "maxOutputTokens": self._config.max_output_tokens,
                "responseMimeType": "application/json",
                "responseJsonSchema": to_gemini_json_schema(schema.model_json_schema()),
            },
        }

    async def _post_once(self, body: dict[str, Any]) -> dict[str, Any]:
        """Send one request and return the parsed JSON body of a 200 response.

        Raises:
            _TransientError: On a network error or an HTTP 5xx (retried).
            _RateLimitedError: On HTTP 429 (retried unless the delay is too long).
            LLMError: On any other 4xx, or a 200 response that is not JSON.
        """
        if self._client is None:
            raise RuntimeError("GeminiClient is not open; use 'async with GeminiClient(...) as llm'")
        url = f"{_API_BASE}/models/{self._model}:generateContent"
        try:
            response = await self._client.post(url, json=body)
        except httpx.TransportError as exc:
            # The exception text may include the proxy URL, so only its type is reported.
            raise _TransientError(f"network error ({type(exc).__name__})") from None

        if response.status_code == 200:
            try:
                parsed = response.json()
            except ValueError:
                raise LLMError("Gemini returned a 200 response that is not JSON") from None
            if not isinstance(parsed, dict):
                raise LLMError("Gemini returned a JSON response that is not an object")
            return parsed

        error, description = _api_error(response)
        if response.status_code == 429:
            delay = _retry_delay(error)
            if delay is not None and delay > _MAX_RETRY_DELAY_S:
                raise LLMError(f"Gemini rate limit: {description} (retry in {delay:.0f}s; not waiting)")
            raise _RateLimitedError(f"Gemini rate limit: {description}", delay)
        if response.status_code >= 500:
            raise _TransientError(f"Gemini server error: {description}")
        raise LLMError(f"Gemini API error: {description}")

    async def generate_structured(self, *, system: str, user: str, schema: type[T]) -> LLMResult[T]:
        """Generate JSON matching `schema` with Gemini and return it validated.

        Network errors, HTTP 5xx, and HTTP 429 are retried (at most 4 attempts);
        after a 429 the client waits the server's `retryDelay` when present.
        Other 4xx errors fail at once.

        Raises:
            LLMError: On API or network failure, a blocked or truncated answer
                (finishReason other than STOP), or JSON that does not match `schema`.
        """
        body = self._request_body(system, user, schema)
        started = time.monotonic()
        retrying = AsyncRetrying(
            stop=stop_after_attempt(_MAX_ATTEMPTS),
            wait=_wait,
            retry=retry_if_exception_type(_TransientError),
            before_sleep=_log_retry,
            sleep=_sleep,
            reraise=True,
        )
        response_body = await retrying(self._post_once, body)
        latency = time.monotonic() - started

        usage = _usage(response_body.get("usageMetadata"))
        model = str(response_body.get("modelVersion") or self._model)
        text = _answer_text(response_body)
        try:
            value = schema.model_validate_json(text)
        except ValidationError as exc:
            problems = sorted(
                {f"{error['type']} at {'.'.join(str(part) for part in error['loc']) or '<root>'}" for error in exc.errors()}
            )
            raise LLMError(
                f"Gemini's answer does not match {schema.__name__} ({exc.error_count()} problem(s): "
                f"{'; '.join(problems[:5])})"
            ) from None

        logger.info(
            "Gemini {}: {:.1f}s, tokens prompt={} output={} total={}",
            model,
            latency,
            usage.prompt_tokens,
            usage.output_tokens,
            usage.total_tokens,
        )
        return LLMResult[schema](value=value, model=model, usage=usage, latency_seconds=latency)
