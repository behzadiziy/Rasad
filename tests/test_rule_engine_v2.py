"""Tests for the Task 7b matching rules: keyword synonym groups and `description_min_hits`.

Configs and jobs are built in memory; one test loads the shipped
`config/config.yaml` to check its profile.
"""

from typing import Any

import pytest
from pydantic import ValidationError

import main
from src.models import AppConfig, JobPost, SearchProfile, WorkMode, load_app_config
from src.rule_engine import RuleEngine

# The shipped profile's keyword groups (config/config.yaml).
_SHIPPED_KEYWORDS: list[Any] = [["AI", "هوش مصنوعی"], "n8n", "Agentic", "LLM", "RAG"]


def _profile(keywords: list[Any] | None = None, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "profile_name": "AI & Automation",
        "keywords": keywords if keywords is not None else _SHIPPED_KEYWORDS,
        "exclude_keywords": ["کارآموز", "Intern"],
        "description_min_hits": 2,
    }
    data.update(overrides)
    return data


def _config(*profiles: dict[str, Any]) -> AppConfig:
    return AppConfig.model_validate(
        {
            "scheduler": {"interval_minutes": 30, "active_hours": "08:00-23:00", "timezone": "Asia/Tehran"},
            "scraping": {"enabled_platforms": ["jobvision", "jobinja"], "max_pages_per_keyword": 2},
            "city_aliases": {"mashhad": ["mashhad", "مشهد"]},
            "search_profiles": list(profiles) or [_profile()],
        }
    )


def _job(title: str, description: str | None = None) -> JobPost:
    return JobPost(
        platform="jobinja",
        external_id="t1",
        title=title,
        company="شرکت نمونه",
        city="مشهد",
        work_mode=WorkMode.ONSITE,
        url="https://jobinja.ir/jobs/t1",
        raw_description=description,
    )


def _matches(job: JobPost, *profiles: dict[str, Any]) -> bool:
    config = _config(*profiles)
    return RuleEngine(config).matches(job, config.search_profiles[0])


# ── Matching ─────────────────────────────────────────────────────


def test_one_ai_mention_in_the_description_no_longer_matches() -> None:
    """The first run's noise: a WordPress support role that mentioned "AI" once."""
    job = _job("پشتیبان وردپرس", "آشنایی با ابزارهای AI مزیت محسوب می‌شود.")
    assert not _matches(job)


def test_two_distinct_groups_in_the_description_match() -> None:
    job = _job("Backend Developer", "تجربه کار با LLM و پیاده‌سازی RAG.")
    assert _matches(job)


@pytest.mark.parametrize(
    "description",
    [
        "AI, AI and more AI.",  # one term repeated
        "AI و هوش مصنوعی در محصولات ما",  # two synonyms of one group
        "AIs everywhere; artificial intelligence (هوش مصنوعی).",  # plural form and a synonym
    ],
)
def test_one_group_counts_once(description: str) -> None:
    assert not _matches(_job("C#/.NET Backend Developer", description))


def test_synonym_and_another_group_reach_the_minimum() -> None:
    assert _matches(_job("Backend Developer", "تجربه در هوش مصنوعی و ساخت workflow با n8n"))


@pytest.mark.parametrize("title", ["AI Engineer", "مهندس هوش مصنوعی", "LLM Developer", "متخصص n8n"])
def test_a_title_keyword_bypasses_the_minimum(title: str) -> None:
    assert _matches(_job(title))
    assert _matches(_job(title, "بدون کلیدواژه"))


def test_exclusions_still_apply_to_title_matches() -> None:
    assert not _matches(_job("AI Intern"))
    assert not _matches(_job("کارآموز هوش مصنوعی", "LLM و RAG"))


def test_location_rules_still_apply() -> None:
    profile = _profile(location_rules=[{"city": "mashhad", "remote_only": False}, {"city": "ALL", "remote_only": True}])
    job = _job("AI Engineer").model_copy(update={"city": "Tehran"})
    assert not _matches(job, profile)
    assert _matches(job.model_copy(update={"work_mode": WorkMode.REMOTE}), profile)


def test_default_minimum_keeps_the_phase1_behavior() -> None:
    plain = _profile(keywords=["AI", "هوش مصنوعی", "n8n"], description_min_hits=1)
    implicit = {key: value for key, value in plain.items() if key != "description_min_hits"}
    job = _job("Backend Developer", "Familiarity with AI tools")
    assert _matches(job, plain)
    assert _matches(job, implicit)
    assert not _matches(_job("Backend Developer", "Python and SQL"), implicit)


def test_minimum_equal_to_the_number_of_groups() -> None:
    profile = _profile(keywords=[["AI", "هوش مصنوعی"], "LLM"], description_min_hits=2)
    assert _matches(_job("Developer", "هوش مصنوعی و LLM"), profile)
    assert not _matches(_job("Developer", "هوش مصنوعی و AI"), profile)


def test_match_profiles_with_groups_in_config_order() -> None:
    config = _config(
        _profile(),
        _profile(profile_name="Automation", keywords=[["n8n", "Zapier"], ["workflow", "اتوماسیون"]]),
    )
    job = _job("Developer", "اتوماسیون با Zapier و LLM و RAG")
    assert RuleEngine(config).match_profiles(job) == ["AI & Automation", "Automation"]


def test_normalized_synonyms_match_persian_variants() -> None:
    # An Arabic Yeh (U+064A) and a ZWNJ in the description still match «هوش مصنوعی».
    job = _job("Developer", "هوش‌مصنوعي و LLM")
    assert _matches(job)


# ── Validation ───────────────────────────────────────────────────


def test_plain_string_keywords_stay_valid() -> None:
    profile = SearchProfile.model_validate({"profile_name": "P", "keywords": ["AI", "LLM"]})
    assert profile.keywords == ["AI", "LLM"]
    assert profile.description_min_hits == 1


def test_groups_keep_their_shape() -> None:
    profile = SearchProfile.model_validate(_profile())
    assert profile.keywords == _SHIPPED_KEYWORDS
    assert profile.model_dump()["keywords"] == _SHIPPED_KEYWORDS


@pytest.mark.parametrize("value", [0, -1])
def test_min_hits_must_be_at_least_one(value: int) -> None:
    with pytest.raises(ValidationError, match="description_min_hits"):
        SearchProfile.model_validate(_profile(description_min_hits=value))


def test_min_hits_cannot_exceed_the_number_of_groups() -> None:
    with pytest.raises(ValidationError, match="description_min_hits is 3, but the profile has only 2 keyword group"):
        SearchProfile.model_validate(_profile(keywords=[["AI", "هوش مصنوعی"], "LLM"], description_min_hits=3))


@pytest.mark.parametrize(
    ("keywords", "message"),
    [
        ([[], "LLM"], "keyword group 1 is empty"),
        ([["AI", "  "], "LLM"], "must not be blank"),
        (["AI", "‌"], "must not be blank"),
        ([["AI", "هوش مصنوعی"], "ai"], "listed in groups 1 and 2"),
        (["LLM", ["RAG", "llm"]], "listed in groups 1 and 2"),
        ([["AI", ["nested"]]], "keywords"),
        ([], "at least 1 item"),
    ],
)
def test_invalid_keyword_groups_are_rejected(keywords: list[Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        SearchProfile.model_validate(_profile(keywords=keywords, description_min_hits=1))


def test_shipped_config_uses_groups_and_a_minimum_of_two() -> None:
    config = load_app_config("config/config.yaml")
    profile = config.search_profiles[0]
    assert profile.keywords == _SHIPPED_KEYWORDS
    assert profile.description_min_hits == 2
    assert profile.exclude_keywords == ["کارآموز", "Intern"]
    assert (config.duplicates.window_days, config.duplicates.title_overlap_threshold) == (45, 0.8)


# ── Search keywords ──────────────────────────────────────────────


def test_search_keywords_are_flattened_and_deduplicated() -> None:
    config = _config(
        _profile(),
        _profile(profile_name="Other", keywords=[["ai", "Python"], " RAG ", "SQL"], description_min_hits=1),
    )
    assert main._collect_keywords(config) == ["AI", "هوش مصنوعی", "n8n", "Agentic", "LLM", "RAG", "Python", "SQL"]


def test_shipped_search_keywords_are_unchanged() -> None:
    assert main._collect_keywords(load_app_config("config/config.yaml")) == [
        "AI",
        "هوش مصنوعی",
        "n8n",
        "Agentic",
        "LLM",
        "RAG",
    ]
