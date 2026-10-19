"""Tests for the Gemini client, the JSON Schema adapter, and the LLM factory (no network: httpx.MockTransport)."""

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from loguru import logger
from pydantic import BaseModel

from src.llm import gemini
from src.llm.base import LLMError, LLMUsage
from src.llm.factory import create_llm_client
from src.llm.gemini import GeminiClient, to_gemini_json_schema
from src.models import AppConfig, LLMConfig, Settings
from src.resume.models import TailoringPlan

_API_KEY = "test-key-123"
_SYSTEM = "SYSTEM-PROMPT-MARKER"
_USER = "USER-PROMPT-MARKER"

Handler = Callable[[httpx.Request], httpx.Response]


class _Answer(BaseModel):
    """A small structured answer."""

    answer: str
    score: int


def _config(**overrides: Any) -> LLMConfig:
    """Build an `llm` config section."""
    data: dict[str, Any] = {"provider": "gemini", "model": "gemini-test-flash", "timeout_seconds": 5}
    data.update(overrides)
    return LLMConfig.model_validate(data)


def _ok_body(parts: list[dict[str, Any]], *, finish_reason: str | None = "STOP", **extra: Any) -> dict[str, Any]:
    """Build a 200 response body with one candidate."""
    candidate: dict[str, Any] = {"content": {"role": "model", "parts": parts}}
    if finish_reason is not None:
        candidate["finishReason"] = finish_reason
    body: dict[str, Any] = {
        "candidates": [candidate],
        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20, "thoughtsTokenCount": 30, "totalTokenCount": 150},
        "modelVersion": "gemini-test-flash-001",
    }
    body.update(extra)
    return body


_GOOD_PARTS = [{"text": '{"answer": "ok", "score": 3}'}]


def _error_body(code: int, status: str, message: str, details: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build a Google API error body (AIP-193)."""
    error: dict[str, Any] = {"code": code, "message": message, "status": status}
    if details is not None:
        error["details"] = details
    return {"error": error}


def _run(handler: Handler, *, config: LLMConfig | None = None) -> Any:
    """Call `generate_structured` once against a mock transport and return the result."""

    async def scenario() -> Any:
        transport = httpx.MockTransport(handler)
        async with GeminiClient(config or _config(), api_key=_API_KEY, transport=transport) as client:
            return await client.generate_structured(system=_SYSTEM, user=_USER, schema=_Answer)

    return asyncio.run(scenario())


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record retry delays instead of sleeping."""
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(gemini, "_sleep", fake_sleep)
    return recorded


# ── Request ────────────────────────────────────────────────────────────────


def test_key_is_sent_in_header_not_url_and_request_is_well_formed() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_ok_body(_GOOD_PARTS))

    result = _run(handler, config=_config(temperature=0.4, max_output_tokens=1234))

    (request,) = seen
    assert request.method == "POST"
    assert request.headers["x-goog-api-key"] == _API_KEY
    assert _API_KEY not in str(request.url)
    assert str(request.url) == (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-test-flash:generateContent"
    )
    body = json.loads(request.content)
    assert body["systemInstruction"] == {"parts": [{"text": _SYSTEM}]}
    assert body["contents"] == [{"role": "user", "parts": [{"text": _USER}]}]
    generation = body["generationConfig"]
    assert generation["temperature"] == 0.4
    assert generation["maxOutputTokens"] == 1234
    assert generation["responseMimeType"] == "application/json"
    assert generation["responseJsonSchema"]["properties"]["score"] == {"type": "integer"}
    assert generation["responseJsonSchema"]["required"] == ["answer", "score"]
    assert result.value == _Answer(answer="ok", score=3)


def test_models_prefix_is_not_duplicated_in_url() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_ok_body(_GOOD_PARTS))

    _run(handler, config=_config(model="models/gemini-test-flash"))
    assert seen[0].url.path == "/v1beta/models/gemini-test-flash:generateContent"


# ── Response ───────────────────────────────────────────────────────────────


def test_thought_parts_are_skipped_and_text_parts_joined() -> None:
    parts = [
        {"text": "Let me think about the answer...", "thought": True},
        {"text": '{"answer": "joined",'},
        {"text": "not text", "thought": True},
        {"text": ' "score": 7}'},
    ]
    result = _run(lambda request: httpx.Response(200, json=_ok_body(parts)))
    assert result.value == _Answer(answer="joined", score=7)


def test_usage_model_version_and_latency() -> None:
    result = _run(lambda request: httpx.Response(200, json=_ok_body(_GOOD_PARTS)))
    assert result.usage == LLMUsage(prompt_tokens=100, output_tokens=50, total_tokens=150)
    assert result.model == "gemini-test-flash-001"
    assert result.latency_seconds >= 0


def test_missing_usage_and_model_version_fall_back() -> None:
    body = {"candidates": [{"content": {"parts": _GOOD_PARTS}, "finishReason": "STOP"}]}
    result = _run(lambda request: httpx.Response(200, json=body))
    assert result.usage == LLMUsage()
    assert result.model == "gemini-test-flash"


@pytest.mark.parametrize(
    "finish_reason",
    [
        "MAX_TOKENS",
        "SAFETY",
        "RECITATION",
        "LANGUAGE",
        "OTHER",
        "BLOCKLIST",
        "PROHIBITED_CONTENT",
        "SPII",
        "MALFORMED_FUNCTION_CALL",
        "FINISH_REASON_UNSPECIFIED",
        "UNEXPECTED_TOOL_CALL",
    ],
)
def test_finish_reason_other_than_stop_raises(finish_reason: str) -> None:
    with pytest.raises(LLMError, match=finish_reason):
        _run(lambda request: httpx.Response(200, json=_ok_body(_GOOD_PARTS, finish_reason=finish_reason)))


def test_missing_finish_reason_raises() -> None:
    with pytest.raises(LLMError, match="MISSING"):
        _run(lambda request: httpx.Response(200, json=_ok_body(_GOOD_PARTS, finish_reason=None)))


def test_blocked_prompt_raises_with_block_reason() -> None:
    body = {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}, "usageMetadata": {"promptTokenCount": 5}}
    with pytest.raises(LLMError, match="PROHIBITED_CONTENT"):
        _run(lambda request: httpx.Response(200, json=body))


def test_no_candidates_raises() -> None:
    with pytest.raises(LLMError, match="no candidates"):
        _run(lambda request: httpx.Response(200, json={"candidates": []}))


def test_only_thought_parts_raise_empty_answer() -> None:
    parts = [{"text": "thinking only", "thought": True}]
    with pytest.raises(LLMError, match="empty answer"):
        _run(lambda request: httpx.Response(200, json=_ok_body(parts)))


def test_non_json_answer_raises_without_echoing_it() -> None:
    parts = [{"text": "SECRET-ANSWER this is not JSON"}]
    with pytest.raises(LLMError) as excinfo:
        _run(lambda request: httpx.Response(200, json=_ok_body(parts)))
    assert "SECRET-ANSWER" not in str(excinfo.value)
    assert "json_invalid" in str(excinfo.value)


def test_json_that_does_not_match_the_schema_raises() -> None:
    parts = [{"text": '{"answer": "SECRET-VALUE", "score": "many"}'}]
    with pytest.raises(LLMError) as excinfo:
        _run(lambda request: httpx.Response(200, json=_ok_body(parts)))
    assert "_Answer" in str(excinfo.value)
    assert "SECRET-VALUE" not in str(excinfo.value)


def test_non_json_http_body_raises() -> None:
    with pytest.raises(LLMError, match="not JSON"):
        _run(lambda request: httpx.Response(200, text="<html>proxy page</html>"))


# ── Errors and retries ─────────────────────────────────────────────────────


def test_429_waits_for_retry_delay_from_error_details(sleeps: list[float]) -> None:
    responses = [
        httpx.Response(
            429,
            json=_error_body(
                429,
                "RESOURCE_EXHAUSTED",
                "Quota exceeded",
                details=[
                    {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": []},
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"},
                ],
            ),
        ),
        httpx.Response(429, json=_error_body(429, "RESOURCE_EXHAUSTED", "Quota exceeded", details=[
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "1.5s"},
        ])),
        httpx.Response(200, json=_ok_body(_GOOD_PARTS)),
    ]
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return responses[len(calls) - 1]

    result = _run(handler)
    assert result.value.answer == "ok"
    assert len(calls) == 3
    assert sleeps == [7.0, 1.5]


def test_429_without_retry_info_backs_off(sleeps: list[float]) -> None:
    responses = [
        httpx.Response(429, json=_error_body(429, "RESOURCE_EXHAUSTED", "Slow down")),
        httpx.Response(200, json=_ok_body(_GOOD_PARTS)),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    _run(handler)
    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= 30 + 2


def test_429_with_long_retry_delay_fails_at_once(sleeps: list[float]) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        details = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "3600s"}]
        return httpx.Response(429, json=_error_body(429, "RESOURCE_EXHAUSTED", "Daily quota exceeded", details))

    with pytest.raises(LLMError, match="Daily quota exceeded"):
        _run(handler)
    assert len(calls) == 1
    assert sleeps == []


def test_5xx_is_retried_up_to_four_attempts(sleeps: list[float]) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, json=_error_body(503, "UNAVAILABLE", "The model is overloaded"))

    with pytest.raises(LLMError, match="503 UNAVAILABLE: The model is overloaded"):
        _run(handler)
    assert len(calls) == 4
    assert len(sleeps) == 3


def test_network_error_is_retried_then_succeeds(sleeps: list[float]) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectError("connection refused via socks5://user:secret@proxy")
        return httpx.Response(200, json=_ok_body(_GOOD_PARTS))

    result = _run(handler)
    assert result.value.score == 3
    assert len(calls) == 3
    assert len(sleeps) == 2


def test_network_error_message_hides_proxy_details(sleeps: list[float]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused via socks5://user:secret@proxy")

    with pytest.raises(LLMError) as excinfo:
        _run(handler)
    assert "secret" not in str(excinfo.value)
    assert "ConnectError" in str(excinfo.value)


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_other_4xx_fail_at_once_with_api_message(status: int, sleeps: list[float]) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status, json=_error_body(status, "INVALID_ARGUMENT", "API key not valid"))

    with pytest.raises(LLMError) as excinfo:
        _run(handler)
    message = str(excinfo.value)
    assert f"HTTP {status} INVALID_ARGUMENT: API key not valid" in message
    assert _SYSTEM not in message and _USER not in message and _API_KEY not in message
    assert len(calls) == 1
    assert sleeps == []


def test_4xx_without_json_body() -> None:
    with pytest.raises(LLMError, match="HTTP 404 \\(no JSON error body\\)"):
        _run(lambda request: httpx.Response(404, text="Not Found"))


def test_prompts_and_answers_are_never_logged(sleeps: list[float]) -> None:
    lines: list[str] = []
    sink_id = logger.add(lines.append, level="DEBUG", format="{message}")
    responses = [
        httpx.Response(503, json=_error_body(503, "UNAVAILABLE", "busy")),
        httpx.Response(200, json=_ok_body([{"text": '{"answer": "ANSWER-MARKER", "score": 1}'}])),
    ]
    try:
        _run(lambda request: responses.pop(0))
    finally:
        logger.remove(sink_id)
    logged = "".join(lines)
    assert "gemini-test-flash-001" in logged
    for secret in (_SYSTEM, _USER, "ANSWER-MARKER", _API_KEY):
        assert secret not in logged


# ── Client setup ───────────────────────────────────────────────────────────


def test_client_requires_context_manager() -> None:
    client = GeminiClient(_config(), api_key=_API_KEY)
    with pytest.raises(RuntimeError, match="not open"):
        asyncio.run(client.generate_structured(system="s", user="u", schema=_Answer))


def test_blank_api_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        GeminiClient(_config(), api_key="  ")


def test_unsupported_proxy_scheme_hides_proxy_url() -> None:
    async def scenario() -> None:
        async with GeminiClient(_config(), api_key=_API_KEY, proxy_url="ftp://user:secret@proxy:21"):
            pass

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(scenario())
    assert "'ftp'" in str(excinfo.value)
    assert "secret" not in str(excinfo.value)


# ── JSON Schema adapter ────────────────────────────────────────────────────


def _all_keys(node: Any) -> set[str]:
    """Return every dict key anywhere in a JSON structure."""
    if isinstance(node, dict):
        return set(node) | set().union(*(_all_keys(value) for value in node.values()))
    if isinstance(node, list):
        return set().union(*(_all_keys(item) for item in node))
    return set()


def test_tailoring_plan_schema_uses_only_supported_keywords() -> None:
    schema = to_gemini_json_schema(TailoringPlan.model_json_schema())
    keys = _all_keys(schema)
    for unsupported in ("$ref", "$defs", "default", "title", "const"):
        assert unsupported not in keys
    properties = schema["properties"]
    assert schema["propertyOrdering"] == list(properties)
    assert properties["language"]["enum"] == ["fa", "en"]
    bullets = properties["experiences"]["items"]["properties"]["bullets"]
    assert bullets["maxItems"] == 5
    assert bullets["items"]["properties"]["bullet_id"]["type"] == "string"
    assert "description" in bullets["items"]["properties"]["text"]
    assert properties["cover_letter"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert set(schema["required"]) == set(properties)


def test_schema_adapter_inlines_refs_keeps_descriptions_and_converts_const() -> None:
    schema = {
        "$defs": {"Inner": {"title": "Inner", "type": "object", "properties": {"x": {"const": "a", "type": "string"}}}},
        "type": "object",
        "title": "Outer",
        "properties": {
            "inner": {"$ref": "#/$defs/Inner", "description": "The inner object"},
            "name": {"type": "string", "default": "n", "maxLength": 9, "pattern": "^a"},
        },
    }
    adapted = to_gemini_json_schema(schema)
    assert adapted == {
        "type": "object",
        "properties": {
            "inner": {
                "type": "object",
                "properties": {"x": {"enum": ["a"], "type": "string"}},
                "description": "The inner object",
                "propertyOrdering": ["x"],
            },
            "name": {"type": "string"},
        },
        "propertyOrdering": ["inner", "name"],
    }


def test_schema_adapter_rejects_cycles_and_unknown_refs() -> None:
    cyclic = {
        "$defs": {"Node": {"type": "object", "properties": {"next": {"$ref": "#/$defs/Node"}}}},
        "$ref": "#/$defs/Node",
    }
    with pytest.raises(ValueError, match="Cyclic"):
        to_gemini_json_schema(cyclic)
    with pytest.raises(ValueError, match="Unsupported"):
        to_gemini_json_schema({"type": "object", "properties": {"a": {"$ref": "#/$defs/Missing"}}})


# ── Factory ────────────────────────────────────────────────────────────────


def _app_config(*, with_llm: bool) -> AppConfig:
    """Build a minimal app config, optionally with an `llm` section."""
    data: dict[str, Any] = {
        "scheduler": {"interval_minutes": 30, "active_hours": "08:00-23:00", "timezone": "Asia/Tehran"},
        "scraping": {"enabled_platforms": ["jobinja"], "max_pages_per_keyword": 1},
        "search_profiles": [{"profile_name": "P", "keywords": ["AI"]}],
    }
    if with_llm:
        data["llm"] = {"provider": "gemini", "model": "gemini-test-flash"}
    return AppConfig.model_validate(data)


def _settings(**overrides: Any) -> Settings:
    """Build settings without reading config/.env."""
    data: dict[str, Any] = {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "@channel", "GEMINI_API_KEY": None}
    data.update(overrides)
    return Settings(_env_file=None, **data)  # type: ignore[call-arg]


def test_factory_returns_gemini_client() -> None:
    client = create_llm_client(_app_config(with_llm=True), _settings(GEMINI_API_KEY="key"))
    assert isinstance(client, GeminiClient)


def test_factory_requires_llm_section() -> None:
    with pytest.raises(ValueError, match="no 'llm' section"):
        create_llm_client(_app_config(with_llm=False), _settings(GEMINI_API_KEY="key"))


def test_factory_requires_api_key() -> None:
    with pytest.raises(ValueError, match="GEMINI_API_KEY is not set"):
        create_llm_client(_app_config(with_llm=True), _settings())
