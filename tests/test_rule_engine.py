"""Tests for the Rule Engine and the config validation it depends on.

All configs and jobs are built in memory; `config/config.yaml` is never read.
"""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.models import AppConfig, JobPost, SearchProfile, WorkMode, load_app_config
from src.rule_engine import RuleEngine

DEFAULT_LOCATION_RULES: list[dict[str, Any]] = [
    {"city": "mashhad", "remote_only": False},
    {"city": "ALL", "remote_only": True},
]


def _profile(
    name: str = "P",
    keywords: list[str] | None = None,
    exclude: list[str] | None = None,
    rules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "profile_name": name,
        "keywords": keywords if keywords is not None else ["AI"],
        "exclude_keywords": exclude or [],
        "location_rules": rules if rules is not None else [],
    }


def _config_data(*profiles: dict[str, Any]) -> dict[str, Any]:
    return {
        "scheduler": {"interval_minutes": 30, "active_hours": "08:00-23:00", "timezone": "Asia/Tehran"},
        "scraping": {"enabled_platforms": ["jobvision", "jobinja"], "max_pages_per_keyword": 2},
        "city_aliases": {"mashhad": ["mashhad", "مشهد"], "tehran": ["tehran", "تهران"]},
        "search_profiles": list(profiles) or [_profile()],
    }


def _config(*profiles: dict[str, Any]) -> AppConfig:
    return AppConfig.model_validate(_config_data(*profiles))


def _job(
    title: str = "AI Engineer",
    city: str = "مشهد",
    work_mode: WorkMode = WorkMode.ONSITE,
    description: str | None = None,
) -> JobPost:
    return JobPost(
        platform="jobvision",
        external_id="123",
        title=title,
        company="Acme",
        city=city,
        work_mode=work_mode,
        url="https://example.com/jobs/123",
        raw_description=description,
    )


def _matches(config: AppConfig, job: JobPost) -> bool:
    return RuleEngine(config).matches(job, config.search_profiles[0])


# ── Keyword matching ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("keyword", "title", "expected"),
    [
        ("AI", "Senior AI Engineer", True),
        ("AI", "Machine Learning (AI) Developer", True),
        ("AI", "Maintenance Manager", False),
        ("AI", "Email Marketing Specialist", False),
        ("RAG", "Storage Engineer", False),
        ("RAG", "RAG Pipeline Developer", True),
        ("LLM", "Engineer working with LLMs", True),
        ("LLM", "LLM Engineer", True),
        ("n8n", "n8n Automation Expert", True),
    ],
)
def test_keyword_word_boundary(keyword: str, title: str, expected: bool) -> None:
    config = _config(_profile(keywords=[keyword]))
    assert _matches(config, _job(title=title)) is expected


def test_keyword_in_description_only_matches() -> None:
    config = _config(_profile(keywords=["RAG"]))
    job = _job(title="Backend Developer", description="Experience building RAG systems is a plus.")
    assert _matches(config, job) is True


def test_keyword_absent_everywhere_rejects() -> None:
    config = _config(_profile(keywords=["RAG"]))
    job = _job(title="Backend Developer", description="Object storage and caching.")
    assert _matches(config, job) is False


def test_multiword_keyword_spans_any_whitespace() -> None:
    config = _config(_profile(keywords=["Machine Learning"]))
    assert _matches(config, _job(title="Senior Machine   Learning Engineer")) is True


@pytest.mark.parametrize(
    ("keyword", "title"),
    [
        ("هوش مصنوعی", "کارشناس هوش‌مصنوعي"),  # ZWNJ + Arabic Yeh in the title
        ("هوش‌مصنوعي", "کارشناس هوش مصنوعی"),  # ZWNJ + Arabic Yeh in the keyword
        ("هوش مصنوعی", "كارشناس هوش  مصنوعى"),  # Arabic Kaf + Alef Maksura, double space
    ],
)
def test_persian_keyword_across_variants(keyword: str, title: str) -> None:
    config = _config(_profile(keywords=[keyword]))
    assert _matches(config, _job(title=title)) is True


def test_persian_keyword_respects_word_boundary() -> None:
    config = _config(_profile(keywords=["هوش"]))
    assert _matches(config, _job(title="باهوش ترین تیم")) is False


# ── Exclusions ───────────────────────────────────────────────────


@pytest.mark.parametrize("title", ["AI Intern", "کارآموز هوش مصنوعی", "Interns – AI team"])
def test_exclusion_in_title_rejects(title: str) -> None:
    config = _config(_profile(keywords=["AI", "هوش مصنوعی"], exclude=["کارآموز", "Intern"]))
    assert _matches(config, _job(title=title)) is False


def test_exclusion_in_description_only_does_not_reject() -> None:
    config = _config(_profile(keywords=["AI"], exclude=["Intern"]))
    job = _job(title="Senior AI Engineer", description="You will mentor an Intern.")
    assert _matches(config, job) is True


def test_exclusion_uses_word_boundary() -> None:
    config = _config(_profile(keywords=["AI"], exclude=["Intern"]))
    assert _matches(config, _job(title="AI Engineer – International team")) is True


# ── Location rules ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("city", "work_mode", "expected"),
    [
        ("مشهد", WorkMode.ONSITE, True),
        ("Mashhad", WorkMode.HYBRID, True),
        ("تهران", WorkMode.ONSITE, False),
        ("تهران", WorkMode.HYBRID, False),
        ("تهران", WorkMode.REMOTE, True),
        ("", WorkMode.REMOTE, True),
        ("", WorkMode.ONSITE, False),
        ("تهران، مشهد", WorkMode.ONSITE, True),
    ],
)
def test_location_table(city: str, work_mode: WorkMode, expected: bool) -> None:
    config = _config(_profile(rules=DEFAULT_LOCATION_RULES))
    assert _matches(config, _job(city=city, work_mode=work_mode)) is expected


def test_location_city_with_province_resolves() -> None:
    config = _config(_profile(rules=DEFAULT_LOCATION_RULES))
    assert _matches(config, _job(city="مشهد، خراسان رضوی", work_mode=WorkMode.ONSITE)) is True


def test_empty_location_rules_accept_any_location() -> None:
    config = _config(_profile(rules=[]))
    assert _matches(config, _job(city="اصفهان", work_mode=WorkMode.ONSITE)) is True


def test_unknown_city_without_all_rule_rejects() -> None:
    config = _config(_profile(rules=[{"city": "mashhad", "remote_only": False}]))
    assert _matches(config, _job(city="اصفهان", work_mode=WorkMode.REMOTE)) is False


def test_remote_only_city_rule_overrides_permissive_all() -> None:
    rules = [{"city": "tehran", "remote_only": True}, {"city": "ALL", "remote_only": False}]
    config = _config(_profile(rules=rules))
    assert _matches(config, _job(city="تهران", work_mode=WorkMode.ONSITE)) is False
    assert _matches(config, _job(city="شیراز", work_mode=WorkMode.ONSITE)) is True


# ── Multiple profiles ────────────────────────────────────────────


def test_match_profiles_returns_all_matches_in_config_order() -> None:
    config = _config(
        _profile(name="Automation", keywords=["n8n"]),
        _profile(name="Data", keywords=["SQL"]),
        _profile(name="AI", keywords=["AI"]),
    )
    job = _job(title="AI Engineer (n8n)")
    assert RuleEngine(config).match_profiles(job) == ["Automation", "AI"]


def test_matches_works_with_profile_outside_config() -> None:
    config = _config(_profile(name="AI", keywords=["AI"]))
    other = SearchProfile.model_validate(_profile(name="Other", keywords=["RAG"]))
    engine = RuleEngine(config)
    assert engine.matches(_job(title="RAG Engineer"), other) is True
    assert engine.matches(_job(title="AI Engineer"), other) is False


# ── Config validation ────────────────────────────────────────────


def test_unknown_location_city_is_rejected() -> None:
    data = _config_data(_profile(rules=[{"city": "isfahan", "remote_only": False}]))
    with pytest.raises(ValidationError, match="isfahan"):
        AppConfig.model_validate(data)


def test_two_all_rules_are_rejected() -> None:
    rules = [{"city": "ALL", "remote_only": True}, {"city": "ALL", "remote_only": False}]
    with pytest.raises(ValidationError, match="at most one"):
        AppConfig.model_validate(_config_data(_profile(rules=rules)))


@pytest.mark.parametrize("hours", ["8:00-23:00", "08:00-24:00", "08:00", "08:00-08:00", "abc"])
def test_invalid_active_hours_are_rejected(hours: str) -> None:
    data = _config_data()
    data["scheduler"]["active_hours"] = hours
    with pytest.raises(ValidationError, match="active_hours"):
        AppConfig.model_validate(data)


def test_active_hours_crossing_midnight_are_valid() -> None:
    data = _config_data()
    data["scheduler"]["active_hours"] = "22:00-02:00"
    assert AppConfig.model_validate(data).scheduler.active_hours == "22:00-02:00"


def test_invalid_timezone_is_rejected() -> None:
    data = _config_data()
    data["scheduler"]["timezone"] = "Mars/Olympus"
    with pytest.raises(ValidationError, match="timezone"):
        AppConfig.model_validate(data)


@pytest.mark.parametrize("platforms", [[], ["linkedin"], ["jobvision", "jobvision"]])
def test_invalid_platforms_are_rejected(platforms: list[str]) -> None:
    data = _config_data()
    data["scraping"]["enabled_platforms"] = platforms
    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


@pytest.mark.parametrize(("section", "field"), [("scheduler", "interval_minutes"), ("scraping", "max_pages_per_keyword")])
def test_minimum_counts_are_enforced(section: str, field: str) -> None:
    data = _config_data()
    data[section][field] = 0
    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


def test_load_app_config_error_includes_path(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        "scheduler: {interval_minutes: 30, active_hours: '08:00-23:00', timezone: 'Asia/Tehran'}\n"
        "scraping: {enabled_platforms: [jobvision], max_pages_per_keyword: 1}\n"
        "city_aliases: {mashhad: [mashhad]}\n"
        "search_profiles:\n"
        "  - profile_name: P\n"
        "    keywords: [AI]\n"
        "    location_rules: [{city: isfahan, remote_only: false}]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="isfahan") as exc_info:
        load_app_config(path)
    assert str(path) in str(exc_info.value)


def test_load_app_config_reads_valid_file(tmp_path: Path) -> None:
    path = tmp_path / "ok.yaml"
    path.write_text(
        "scheduler: {interval_minutes: 30, active_hours: '08:00-23:00', timezone: 'Asia/Tehran'}\n"
        "scraping: {enabled_platforms: [jobinja], max_pages_per_keyword: 1}\n"
        "search_profiles: [{profile_name: P, keywords: [AI]}]\n",
        encoding="utf-8",
    )
    config = load_app_config(path)
    assert config.search_profiles[0].profile_name == "P"
