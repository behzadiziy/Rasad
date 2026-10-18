"""Tests for the Task 7 model additions: settings, config sections, the status machine, and records.

Settings are built with `_env_file=None`, so `config/.env` is never read.
"""

import itertools
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.models import (
    AppConfig,
    ApplicationRecord,
    ApplyConfig,
    BotConfig,
    JobStatus,
    LLMConfig,
    Settings,
    TailoredResumeRecord,
    TailoringConfig,
    can_transition,
    load_app_config,
)

_SETTINGS_ENV_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_PROXY_URL",
    "DB_PATH",
    "HEADLESS",
    "GEMINI_API_KEY",
    "LLM_PROXY_URL",
    "TELEGRAM_ALLOWED_USER_IDS",
    "TELEGRAM_REVIEW_CHAT_ID",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep variables exported in the developer's shell out of the settings under test."""
    for name in _SETTINGS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _settings(**overrides: Any) -> Settings:
    """Build settings without reading config/.env; keyword arguments override the defaults."""
    values: dict[str, Any] = {"TELEGRAM_BOT_TOKEN": "123:test-token", "TELEGRAM_CHAT_ID": "@test_channel"}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _config_data(**sections: Any) -> dict[str, Any]:
    """Return a minimal valid Phase 1 config, plus any extra sections."""
    data: dict[str, Any] = {
        "scheduler": {"interval_minutes": 30, "active_hours": "08:00-23:00", "timezone": "Asia/Tehran"},
        "scraping": {"enabled_platforms": ["jobvision", "jobinja"], "max_pages_per_keyword": 2},
        "city_aliases": {"mashhad": ["mashhad", "مشهد"]},
        "search_profiles": [{"profile_name": "P", "keywords": ["AI"]}],
    }
    data.update(sections)
    return data


def _llm(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"provider": "gemini", "model": "gemini-test-flash"}
    data.update(overrides)
    return data


# ── Settings: allowed user IDs ───────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("123456789", frozenset({123456789})),
        ("111,222", frozenset({111, 222})),
        ("  111 ,  222  ", frozenset({111, 222})),
        ("111, 111", frozenset({111})),
        ("007", frozenset({7})),
        ("", frozenset()),
        ("   ", frozenset()),
    ],
)
def test_allowed_user_ids_valid(raw: str, expected: frozenset[int]) -> None:
    assert _settings(TELEGRAM_ALLOWED_USER_IDS=raw).allowed_user_ids == expected


@pytest.mark.parametrize(
    "raw",
    ["abc", "111,abc", "0", "-5", "+5", "1.5", "111,,222", "111,", ",111", "۱۲۳", "@someone", "[111, 222]"],
)
def test_allowed_user_ids_invalid(raw: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        _settings(TELEGRAM_ALLOWED_USER_IDS=raw)
    (error,) = exc_info.value.errors()
    assert error["loc"] == ("TELEGRAM_ALLOWED_USER_IDS",)
    assert "positive integer" in error["msg"]


def test_allowed_user_ids_default_is_empty() -> None:
    settings = _settings()
    assert settings.TELEGRAM_ALLOWED_USER_IDS == ""
    assert settings.allowed_user_ids == frozenset()


def test_allowed_user_ids_from_environment_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    # A list[int] field would be JSON-decoded by pydantic-settings and reject this value.
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "111, 222")
    assert _settings().allowed_user_ids == frozenset({111, 222})


def test_allowed_user_ids_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=123:test\nTELEGRAM_CHAT_ID=@c\nTELEGRAM_ALLOWED_USER_IDS=333,444\n"
        "TELEGRAM_REVIEW_CHAT_ID=\nGEMINI_API_KEY=\nLLM_PROXY_URL=\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=env_file)
    assert settings.allowed_user_ids == frozenset({333, 444})
    assert settings.review_chat_id == "333"
    assert settings.GEMINI_API_KEY is None
    assert settings.LLM_PROXY_URL is None


def test_allowed_user_ids_error_does_not_echo_value() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _settings(TELEGRAM_ALLOWED_USER_IDS="111,secret-looking-value")
    assert "secret-looking-value" not in exc_info.value.errors()[0]["msg"]


# ── Settings: review chat and the other new fields ───────────────


def test_review_chat_id_explicit_wins() -> None:
    settings = _settings(TELEGRAM_ALLOWED_USER_IDS="111,222", TELEGRAM_REVIEW_CHAT_ID=" 999 ")
    assert settings.review_chat_id == "999"


def test_review_chat_id_falls_back_to_first_allowed_id_in_order() -> None:
    assert _settings(TELEGRAM_ALLOWED_USER_IDS="222, 111").review_chat_id == "222"


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_review_chat_id_is_unset(blank: str) -> None:
    settings = _settings(TELEGRAM_ALLOWED_USER_IDS="555", TELEGRAM_REVIEW_CHAT_ID=blank)
    assert settings.TELEGRAM_REVIEW_CHAT_ID is None
    assert settings.review_chat_id == "555"


def test_review_chat_id_none_without_any_ids() -> None:
    assert _settings().review_chat_id is None


@pytest.mark.parametrize("raw", ["@my_channel", "-1001234567890", "0", "abc", "12 34"])
def test_review_chat_id_must_be_a_private_user_chat(raw: str) -> None:
    with pytest.raises(ValidationError, match="TELEGRAM_REVIEW_CHAT_ID"):
        _settings(TELEGRAM_REVIEW_CHAT_ID=raw)


def test_review_chat_id_must_differ_from_notification_chat() -> None:
    with pytest.raises(ValidationError, match="must differ from TELEGRAM_CHAT_ID") as exc_info:
        _settings(TELEGRAM_CHAT_ID="424242", TELEGRAM_REVIEW_CHAT_ID="424242")
    # The error text (which the notifier logs) must never include the bot token.
    assert "test-token" not in str(exc_info.value)


def test_new_settings_defaults() -> None:
    settings = _settings()
    assert settings.GEMINI_API_KEY is None
    assert settings.LLM_PROXY_URL is None
    assert settings.TELEGRAM_REVIEW_CHAT_ID is None


@pytest.mark.parametrize("blank", ["", "  "])
def test_blank_llm_proxy_and_api_key_become_none(blank: str) -> None:
    settings = _settings(LLM_PROXY_URL=blank, GEMINI_API_KEY=blank, TELEGRAM_PROXY_URL=blank)
    assert settings.LLM_PROXY_URL is None
    assert settings.GEMINI_API_KEY is None
    assert settings.TELEGRAM_PROXY_URL is None


def test_llm_proxy_and_api_key_kept() -> None:
    settings = _settings(LLM_PROXY_URL="socks5://127.0.0.1:1080", GEMINI_API_KEY="test-key-value")
    assert settings.LLM_PROXY_URL == "socks5://127.0.0.1:1080"
    assert settings.GEMINI_API_KEY == "test-key-value"
    assert "test-key-value" not in repr(settings)


# ── Config sections ──────────────────────────────────────────────


def test_phase2_sections_default_when_missing() -> None:
    config = AppConfig.model_validate(_config_data())
    assert config.llm is None
    assert config.tailoring == TailoringConfig(
        master_resume_path=Path("config/master_resume.yaml"),
        output_dir=Path("data/resumes"),
        cover_letter=True,
        max_versions_per_job=3,
        persian_ratio_threshold=0.3,
    )
    assert config.bot == BotConfig(poll_timeout_seconds=30, state_path=Path("data/bot_state.json"))
    assert config.apply == ApplyConfig(
        enabled=False,
        daily_limit=10,
        confirm_before_submit=False,
        confirm_timeout_minutes=30,
        screenshots_dir=Path("data/applications"),
    )


def test_llm_section_parsed_with_defaults() -> None:
    config = AppConfig.model_validate(_config_data(llm=_llm()))
    assert config.llm == LLMConfig(
        provider="gemini", model="gemini-test-flash", temperature=0.3, max_output_tokens=8192, timeout_seconds=120
    )


def test_partial_sections_keep_other_defaults() -> None:
    config = AppConfig.model_validate(
        _config_data(tailoring={"cover_letter": False}, apply={"enabled": True, "daily_limit": 2})
    )
    assert config.tailoring.cover_letter is False
    assert config.tailoring.max_versions_per_job == 3
    assert config.apply.enabled is True
    assert config.apply.daily_limit == 2
    assert config.apply.confirm_timeout_minutes == 30


@pytest.mark.parametrize("temperature", [0, 0.3, 2])
def test_temperature_bounds_accepted(temperature: float) -> None:
    assert LLMConfig.model_validate(_llm(temperature=temperature)).temperature == temperature


@pytest.mark.parametrize(
    ("section", "values"),
    [
        ("llm", _llm(provider="openai")),
        ("llm", _llm(model="")),
        ("llm", _llm(model="gemini flash")),
        ("llm", _llm(temperature=-0.1)),
        ("llm", _llm(temperature=2.1)),
        ("llm", _llm(max_output_tokens=0)),
        ("llm", _llm(timeout_seconds=0)),
        ("llm", _llm(timeout_seconds=-5)),
        ("llm", _llm(unknown_key=1)),
        ("llm", {"model": "gemini-test-flash"}),
        ("llm", {"provider": "gemini"}),
        ("tailoring", {"max_versions_per_job": 0}),
        ("tailoring", {"persian_ratio_threshold": 0}),
        ("tailoring", {"persian_ratio_threshold": 1}),
        ("tailoring", {"persian_ratio_threshold": 1.5}),
        ("tailoring", {"output_dir": "  "}),
        ("tailoring", {"master_resume_path": ""}),
        ("tailoring", {"max_version_per_job": 3}),
        ("bot", {"poll_timeout_seconds": 0}),
        ("bot", {"state_path": ""}),
        ("apply", {"daily_limit": 0}),
        ("apply", {"daily_limit": -1}),
        ("apply", {"confirm_timeout_minutes": 0}),
        ("apply", {"screenshots_dir": ""}),
        ("apply", {"dailylimit": 5}),
    ],
)
def test_section_validation_errors(section: str, values: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as exc_info:
        AppConfig.model_validate(_config_data(**{section: values}))
    assert exc_info.value.errors()[0]["loc"][0] == section


def test_load_app_config_without_llm_disables_phase2(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "scheduler: {interval_minutes: 30, active_hours: '08:00-23:00', timezone: 'Asia/Tehran'}\n"
        "scraping: {enabled_platforms: [jobinja], max_pages_per_keyword: 1}\n"
        "search_profiles: [{profile_name: P, keywords: [AI]}]\n",
        encoding="utf-8",
    )
    config = load_app_config(path)
    assert config.llm is None
    assert config.apply.enabled is False


def test_load_app_config_reports_invalid_section(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "scheduler: {interval_minutes: 30, active_hours: '08:00-23:00', timezone: 'Asia/Tehran'}\n"
        "scraping: {enabled_platforms: [jobinja], max_pages_per_keyword: 1}\n"
        "search_profiles: [{profile_name: P, keywords: [AI]}]\n"
        "llm: {provider: gemini, model: m, temperature: 3}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="temperature"):
        load_app_config(path)


def test_shipped_config_is_valid() -> None:
    config = load_app_config("config/config.yaml")
    assert config.llm is not None
    assert config.llm.provider == "gemini"
    assert config.apply.enabled is False


# ── Status machine ───────────────────────────────────────────────

# The table in CLAUDE.md, written out independently of src/models.py.
_EXPECTED_TRANSITIONS = {
    ("NEW", "NOTIFIED"),
    ("IGNORED", "NEW"),
    ("NEW", "IGNORED"),
    ("NOTIFIED", "TAILORED"),
    ("TAILORED", "TAILORED"),
    ("TAILORED", "APPROVED"),
    ("TAILORED", "SKIPPED"),
    ("APPROVED", "APPLIED"),
    ("APPROVED", "APPLY_FAILED"),
    ("APPLY_FAILED", "APPROVED"),
    ("APPLY_FAILED", "SKIPPED"),
}


def test_job_status_members() -> None:
    assert [status.value for status in JobStatus] == [
        "NEW",
        "NOTIFIED",
        "TAILORED",
        "APPROVED",
        "APPLIED",
        "APPLY_FAILED",
        "SKIPPED",
        "IGNORED",
    ]


@pytest.mark.parametrize(("old", "new"), list(itertools.product(JobStatus, JobStatus)))
def test_can_transition_every_pair(old: JobStatus, new: JobStatus) -> None:
    assert can_transition(old, new) is ((old.value, new.value) in _EXPECTED_TRANSITIONS)


def test_terminal_statuses_have_no_exits() -> None:
    for terminal in (JobStatus.APPLIED, JobStatus.SKIPPED):
        assert not any(can_transition(terminal, target) for target in JobStatus)


# ── Records ──────────────────────────────────────────────────────


def _resume(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "platform": "jobinja",
        "external_id": "1001",
        "version": 1,
        "language": "fa",
        "llm_model": "gemini-test-flash",
        "plan_json": '{"summary": "x"}',
        "changes_json": '{"items": []}',
    }
    data.update(overrides)
    return data


def test_tailored_resume_record_defaults_and_utc() -> None:
    tehran = timezone(timedelta(hours=3, minutes=30))
    record = TailoredResumeRecord.model_validate(
        _resume(created_at=datetime(2026, 5, 1, 12, 0, tzinfo=tehran), approved_at=datetime(2026, 5, 1, 9, 0))
    )
    assert record.id is None
    assert record.clean_pdf_path is None
    assert record.review_pdf_path is None
    assert record.created_at == datetime(2026, 5, 1, 8, 30, tzinfo=UTC)
    assert record.created_at.utcoffset() == timedelta(0)
    assert record.approved_at == datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
    assert TailoredResumeRecord.model_validate(_resume()).created_at.tzinfo is not None


@pytest.mark.parametrize(
    "overrides",
    [
        {"version": 0},
        {"language": "de"},
        {"platform": "linkedin"},
        {"external_id": ""},
        {"llm_model": ""},
        {"plan_json": "not json"},
        {"changes_json": "[1, 2]"},
    ],
)
def test_tailored_resume_record_validation(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        TailoredResumeRecord.model_validate(_resume(**overrides))


def test_tailored_resume_record_errors_hide_resume_content() -> None:
    with pytest.raises(ValidationError) as exc_info:
        TailoredResumeRecord.model_validate(_resume(plan_json="Fictional Person, Senior Engineer at Example Co"))
    assert "Fictional Person" not in str(exc_info.value)


def test_application_record() -> None:
    record = ApplicationRecord(
        platform="jobvision", external_id="42", resume_id=3, submitted=True, attempted_at=datetime(2026, 5, 1)
    )
    assert record.attempted_at == datetime(2026, 5, 1, tzinfo=UTC)
    assert record.receipt_path is None
    assert record.error is None
    with pytest.raises(ValidationError):
        ApplicationRecord(platform="jobvision", external_id="42", resume_id=0, submitted=False)
