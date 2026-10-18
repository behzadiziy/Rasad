"""Data models: environment settings, YAML app configuration, job posts, and their status machine.

Phase 2 and 3 add the LLM, tailoring, bot, and apply config sections, the
extended `JobStatus` state machine (`can_transition`), and the records stored
for tailored resumes and application attempts. Task 7b adds keyword synonym
groups with `description_min_hits`, the `duplicates` config section, and
`JobPost.duplicate_group`.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from loguru import logger
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.utils import normalize_text

Platform = Literal["jobvision", "jobinja"]

Language = Literal["fa", "en"]

_ALL_CITIES = "ALL"

_ACTIVE_HOURS_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")

# ASCII digits only: `str.isdigit` and `int` would also accept Persian or Arabic digits.
_POSITIVE_INT_RE = re.compile(r"^[0-9]+$")

# Gemini model codes such as "gemini-3.8-flash"; "/" and ":" leave room for
# OpenRouter-style names such as "google/gemini-3.8-flash".
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")


class JobStatus(StrEnum):
    """Lifecycle state of a stored job; `can_transition` defines the allowed moves."""

    NEW = "NEW"
    NOTIFIED = "NOTIFIED"
    TAILORED = "TAILORED"  # review package sent; awaiting the user's decision
    APPROVED = "APPROVED"  # the user approved a tailored resume
    APPLIED = "APPLIED"
    APPLY_FAILED = "APPLY_FAILED"
    SKIPPED = "SKIPPED"  # the user rejected the job
    IGNORED = "IGNORED"


# The state machine table in CLAUDE.md, one (from, to) pair per row.
_ALLOWED_TRANSITIONS: frozenset[tuple[JobStatus, JobStatus]] = frozenset(
    {
        (JobStatus.NEW, JobStatus.NOTIFIED),  # notification sent
        (JobStatus.IGNORED, JobStatus.NEW),  # --reevaluate: matches now
        (JobStatus.NEW, JobStatus.IGNORED),  # --reevaluate: no longer matches
        (JobStatus.NOTIFIED, JobStatus.TAILORED),  # review package sent
        (JobStatus.TAILORED, JobStatus.TAILORED),  # regenerate
        (JobStatus.TAILORED, JobStatus.APPROVED),  # approve
        (JobStatus.TAILORED, JobStatus.SKIPPED),  # reject
        (JobStatus.APPROVED, JobStatus.APPLIED),  # submission confirmed
        (JobStatus.APPROVED, JobStatus.APPLY_FAILED),  # submission failed
        (JobStatus.APPLY_FAILED, JobStatus.APPROVED),  # retry
        (JobStatus.APPLY_FAILED, JobStatus.SKIPPED),  # reject after a failure
    }
)


def can_transition(old: JobStatus, new: JobStatus) -> bool:
    """Return True if the state machine allows a job to move from `old` to `new`."""
    return (JobStatus(old), JobStatus(new)) in _ALLOWED_TRANSITIONS


class WorkMode(StrEnum):
    """Where the work is performed."""

    ONSITE = "ONSITE"
    HYBRID = "HYBRID"
    REMOTE = "REMOTE"


def _parse_user_ids(value: str) -> tuple[int, ...]:
    """Parse comma-separated positive integer Telegram user IDs, keeping their order.

    A blank string means "no IDs". Error messages name the item's position,
    never its value.

    Raises:
        ValueError: If any item is not a positive integer written in ASCII digits.
    """
    if not value.strip():
        return ()
    ids: list[int] = []
    for position, item in enumerate(value.split(","), start=1):
        item = item.strip()
        if not _POSITIVE_INT_RE.fullmatch(item) or int(item) == 0:
            raise ValueError(
                f"item {position} is not a positive integer Telegram user ID; "
                "expected comma-separated numbers such as '123456789,987654321'"
            )
        ids.append(int(item))
    return tuple(ids)


class Settings(BaseSettings):
    """Secrets and environment-specific settings, loaded from `config/.env`."""

    model_config = SettingsConfigDict(
        env_file="config/.env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    TELEGRAM_BOT_TOKEN: str
    TELEGRAM_CHAT_ID: str
    TELEGRAM_PROXY_URL: str | None = None
    DB_PATH: str = "data/jobs.db"
    HEADLESS: bool = True

    # Phase 2 and 3. All optional: Phase 1 runs without them.
    GEMINI_API_KEY: str | None = Field(default=None, repr=False)
    LLM_PROXY_URL: str | None = None
    # A plain string on purpose: pydantic-settings JSON-decodes list fields read
    # from the environment and would reject "123,456". Parsed by `allowed_user_ids`.
    TELEGRAM_ALLOWED_USER_IDS: str = ""
    TELEGRAM_REVIEW_CHAT_ID: str | None = None

    @field_validator("TELEGRAM_PROXY_URL", "LLM_PROXY_URL", mode="before")
    @classmethod
    def _blank_proxy_is_none(cls, value: Any) -> Any:
        """Treat an empty or whitespace-only proxy URL as "no proxy"."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("GEMINI_API_KEY", "TELEGRAM_REVIEW_CHAT_ID", mode="before")
    @classmethod
    def _blank_optional_is_none(cls, value: Any) -> Any:
        """Treat an empty or whitespace-only value (e.g. `GEMINI_API_KEY=` in .env) as unset."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("TELEGRAM_ALLOWED_USER_IDS")
    @classmethod
    def _validate_allowed_user_ids(cls, value: str) -> str:
        """Require every comma-separated item to be a positive integer user ID."""
        _parse_user_ids(value)
        return value.strip()

    @field_validator("TELEGRAM_REVIEW_CHAT_ID")
    @classmethod
    def _validate_review_chat_id(cls, value: str | None, info: ValidationInfo) -> str | None:
        """Require a private chat (a positive numeric user ID) that is not the notification chat.

        This is a field validator, not a model validator, on purpose: a
        model-level error would carry every input, including the bot token,
        into the `ValidationError` text that callers log.
        """
        if value is None:
            return None
        value = value.strip()
        if not _POSITIVE_INT_RE.fullmatch(value) or int(value) == 0:
            raise ValueError(
                "must be the positive numeric ID of a private chat with the bot (a Telegram user ID); "
                "@usernames, channels, and groups are not allowed"
            )
        notification_chat = info.data.get("TELEGRAM_CHAT_ID")
        if isinstance(notification_chat, str) and value == notification_chat.strip():
            raise ValueError("must differ from TELEGRAM_CHAT_ID: resumes never go to the notification chat")
        return value

    @property
    def allowed_user_ids(self) -> frozenset[int]:
        """Telegram user IDs allowed to use the bot; empty when none are configured."""
        return frozenset(_parse_user_ids(self.TELEGRAM_ALLOWED_USER_IDS))

    @property
    def review_chat_id(self) -> str | None:
        """Private chat for resumes: `TELEGRAM_REVIEW_CHAT_ID`, else the first allowed user ID, else None."""
        if self.TELEGRAM_REVIEW_CHAT_ID is not None:
            return self.TELEGRAM_REVIEW_CHAT_ID
        ids = _parse_user_ids(self.TELEGRAM_ALLOWED_USER_IDS)
        return str(ids[0]) if ids else None


class SchedulerConfig(BaseModel):
    """When and how often the scraping cycle runs."""

    interval_minutes: int = Field(ge=1)
    active_hours: str
    timezone: str

    @field_validator("active_hours")
    @classmethod
    def _validate_active_hours(cls, value: str) -> str:
        """Require `HH:MM-HH:MM`; windows crossing midnight are allowed."""
        match = _ACTIVE_HOURS_RE.match(value)
        if match is None:
            raise ValueError(f"active_hours must match HH:MM-HH:MM (e.g. '08:00-23:00'), got {value!r}")
        start_h, start_m, end_h, end_m = match.groups()
        if (start_h, start_m) == (end_h, end_m):
            raise ValueError(f"active_hours start and end must differ, got {value!r}")
        return value

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        """Require a valid IANA time zone name."""
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"timezone must be a valid IANA time zone (e.g. 'Asia/Tehran'), got {value!r}") from exc
        return value


class ScrapingConfig(BaseModel):
    """Which platforms to scrape and how deep."""

    enabled_platforms: list[Platform] = Field(min_length=1)
    max_pages_per_keyword: int = Field(ge=1)

    @field_validator("enabled_platforms")
    @classmethod
    def _no_duplicate_platforms(cls, value: list[Platform]) -> list[Platform]:
        """Reject a platform listed more than once."""
        if len(set(value)) != len(value):
            raise ValueError(f"enabled_platforms contains duplicates: {value}")
        return value


class LocationRule(BaseModel):
    """A location filter: a canonical city key (or "ALL") and whether only remote jobs pass."""

    city: str = Field(min_length=1)
    remote_only: bool


class SearchProfile(BaseModel):
    """A named set of keywords, exclusions, and location rules.

    Each item of `keywords` is a synonym group: a nested list such as
    `["AI", "هوش مصنوعی"]`, or a plain string, which is a group of one. A
    keyword from any group in the title passes the keyword check. Otherwise
    the description must contain terms from at least `description_min_hits`
    distinct groups; the default of 1 means any keyword anywhere.
    """

    profile_name: str = Field(min_length=1)
    keywords: list[str | list[str]] = Field(min_length=1)
    exclude_keywords: list[str] = Field(default_factory=list)
    location_rules: list[LocationRule] = Field(default_factory=list)
    description_min_hits: int = Field(default=1, ge=1)

    @field_validator("keywords")
    @classmethod
    def _validate_keyword_groups(cls, value: list[str | list[str]]) -> list[str | list[str]]:
        """Reject empty groups, blank terms, and a term listed in more than one group.

        A term shared by two groups would count as two distinct groups and
        defeat `description_min_hits`, so synonyms must share one group.
        """
        first_group: dict[str, int] = {}
        for position, group in enumerate(value, start=1):
            terms = [group] if isinstance(group, str) else group
            if not terms:
                raise ValueError(f"keyword group {position} is empty; list at least one term")
            blank = [term for term in terms if not normalize_text(term)]
            if blank:
                raise ValueError(f"keywords must not be blank after normalization, got {blank!r}")
            for term in terms:
                seen_in = first_group.setdefault(normalize_text(term), position)
                if seen_in != position:
                    raise ValueError(
                        f"keyword {term!r} is listed in groups {seen_in} and {position}; "
                        "a term may belong to one group only, so put synonyms in one nested list"
                    )
        return value

    @field_validator("exclude_keywords")
    @classmethod
    def _no_blank_terms(cls, value: list[str]) -> list[str]:
        """Reject terms that are empty after normalization (they would match everything)."""
        blank = [term for term in value if not normalize_text(term)]
        if blank:
            raise ValueError(f"keywords must not be blank after normalization, got {blank!r}")
        return value

    @model_validator(mode="after")
    def _min_hits_within_groups(self) -> SearchProfile:
        """Require `description_min_hits` to be at most the number of keyword groups."""
        if self.description_min_hits > len(self.keywords):
            raise ValueError(
                f"Profile {self.profile_name!r}: description_min_hits is {self.description_min_hits}, "
                f"but the profile has only {len(self.keywords)} keyword group(s)"
            )
        return self

    @model_validator(mode="after")
    def _at_most_one_all_rule(self) -> SearchProfile:
        """Allow at most one "ALL" location rule per profile."""
        all_rules = sum(1 for rule in self.location_rules if rule.city == _ALL_CITIES)
        if all_rules > 1:
            raise ValueError(
                f"Profile {self.profile_name!r} has {all_rules} '{_ALL_CITIES}' location rules; at most one is allowed"
            )
        return self


def _reject_blank_path(value: Any) -> Any:
    """Reject a blank path string, which `Path` would silently turn into "."."""
    if isinstance(value, str) and not value.strip():
        raise ValueError("path must not be blank")
    return value


_NonBlankPath = Annotated[Path, BeforeValidator(_reject_blank_path)]


class LLMConfig(BaseModel):
    """Which LLM tailors resumes, and how it is called (the `llm` section)."""

    model_config = ConfigDict(extra="forbid")

    # Only Gemini is implemented. OpenAI and OpenRouter will be added here, as
    # Literal["gemini", "openai", "openrouter"], behind the same LLMClient interface.
    provider: Literal["gemini"]
    model: str
    temperature: float = Field(default=0.3, ge=0, le=2)
    max_output_tokens: int = Field(default=8192, ge=1)
    timeout_seconds: float = Field(default=120, gt=0)

    @field_validator("model")
    @classmethod
    def _validate_model(cls, value: str) -> str:
        """Require a model code such as "gemini-3.8-flash" (it becomes part of the API URL)."""
        value = value.strip()
        if not _MODEL_NAME_RE.fullmatch(value):
            raise ValueError(
                "model must be a model code such as 'gemini-3.8-flash' "
                f"(letters, digits, '.', '_', '-', ':', '/'), got {value!r}"
            )
        return value


class TailoringConfig(BaseModel):
    """Resume tailoring inputs, outputs, and limits (the `tailoring` section)."""

    model_config = ConfigDict(extra="forbid")

    master_resume_path: _NonBlankPath = Path("config/master_resume.yaml")
    output_dir: _NonBlankPath = Path("data/resumes")
    cover_letter: bool = True
    max_versions_per_job: int = Field(default=3, ge=1)
    persian_ratio_threshold: float = Field(default=0.3, gt=0, lt=1)


class BotConfig(BaseModel):
    """Two-way Telegram bot settings (the `bot` section)."""

    model_config = ConfigDict(extra="forbid")

    poll_timeout_seconds: int = Field(default=30, ge=1)
    state_path: _NonBlankPath = Path("data/bot_state.json")


class ApplyConfig(BaseModel):
    """Application submission settings (the `apply` section); disabled by default."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    daily_limit: int = Field(default=10, ge=1)
    confirm_before_submit: bool = False
    confirm_timeout_minutes: int = Field(default=30, ge=1)
    screenshots_dir: _NonBlankPath = Path("data/applications")


class DuplicatesConfig(BaseModel):
    """Probable-duplicate detection settings (the `duplicates` section); see `src/duplicates.py`.

    Two postings are probable duplicates only if they were first seen within
    `window_days` of each other and their title token sets overlap by at least
    `title_overlap_threshold` (overlap coefficient), among other checks.
    """

    model_config = ConfigDict(extra="forbid")

    window_days: int = Field(default=45, ge=1)
    title_overlap_threshold: float = Field(default=0.8, gt=0, le=1)


class AppConfig(BaseModel):
    """The full application configuration loaded from `config/config.yaml`.

    `llm` is optional: without it, Phase 2 features (resume tailoring and the
    review bot) are disabled. The other Phase 2 and 3 sections, and the
    `duplicates` section, have defaults.
    """

    scheduler: SchedulerConfig
    scraping: ScrapingConfig
    city_aliases: dict[str, list[str]] = Field(default_factory=dict)
    search_profiles: list[SearchProfile] = Field(min_length=1)
    llm: LLMConfig | None = None
    tailoring: TailoringConfig = Field(default_factory=TailoringConfig)
    bot: BotConfig = Field(default_factory=BotConfig)
    apply: ApplyConfig = Field(default_factory=ApplyConfig)
    duplicates: DuplicatesConfig = Field(default_factory=DuplicatesConfig)

    @field_validator("city_aliases")
    @classmethod
    def _validate_city_aliases(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        """Require non-empty alias lists and reserve the "ALL" key."""
        if _ALL_CITIES in value:
            raise ValueError(f"'{_ALL_CITIES}' is reserved and cannot be a city_aliases key")
        for city, aliases in value.items():
            if not aliases:
                raise ValueError(f"city_aliases[{city!r}] must list at least one alias")
            blank = [alias for alias in aliases if not normalize_text(alias)]
            if blank:
                raise ValueError(f"city_aliases[{city!r}] contains blank aliases: {blank!r}")
        return value

    @model_validator(mode="after")
    def _validate_profiles(self) -> AppConfig:
        """Require unique profile names and location cities that are "ALL" or a city_aliases key."""
        names = [profile.profile_name for profile in self.search_profiles]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"profile_name values must be unique; duplicates: {duplicates}")

        known = sorted(self.city_aliases)
        for profile in self.search_profiles:
            for rule in profile.location_rules:
                if rule.city != _ALL_CITIES and rule.city not in self.city_aliases:
                    raise ValueError(
                        f"Profile {profile.profile_name!r}: location rule city {rule.city!r} is not "
                        f"'{_ALL_CITIES}' or a key of city_aliases (known keys: {known})"
                    )
        return self


def load_app_config(path: str | Path = "config/config.yaml") -> AppConfig:
    """Read and validate the YAML application config.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the YAML is malformed or fails validation; the message includes the path.
    """
    config_path = Path(path)
    text = config_path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid config file '{config_path}': malformed YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"Invalid config file '{config_path}': top level must be a mapping, got {type(data).__name__}"
        )

    try:
        config = AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"Invalid config file '{config_path}':\n{exc}") from exc

    logger.info(
        "Loaded config from {}: {} profile(s), platforms={}",
        config_path,
        len(config.search_profiles),
        config.scraping.enabled_platforms,
    )
    if config.llm is None:
        logger.info(
            "No 'llm' section in {}: Phase 2 features (resume tailoring and the review bot) are disabled",
            config_path,
        )
    return config


class JobPost(BaseModel):
    """A single job listing as scraped from a platform.

    `duplicate_group` links probable duplicates (the same position posted on
    both boards, or reposted): every member stores the `"{platform}:{external_id}"`
    of the group's earliest member. None means the job is in no group.
    """

    platform: Platform
    external_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    company: str
    city: str
    work_mode: WorkMode
    salary: str | None = None
    url: str = Field(min_length=1)
    raw_description: str | None = None
    matched_profiles: list[str] = Field(default_factory=list)
    status: JobStatus = JobStatus.NEW
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    duplicate_group: str | None = Field(default=None, min_length=1)

    @field_validator("created_at")
    @classmethod
    def _created_at_utc(cls, value: datetime) -> datetime:
        """Store `created_at` in UTC; naive datetimes are assumed to already be UTC."""
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @property
    def is_remote(self) -> bool:
        """Return True when the job is fully remote."""
        return self.work_mode == WorkMode.REMOTE


def _as_utc(value: datetime) -> datetime:
    """Return a datetime in UTC; naive values are assumed to already be UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _require_json_object(value: str) -> str:
    """Require a JSON object; the message never echoes the text, which holds resume content."""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        raise ValueError("must be valid JSON") from None
    if not isinstance(parsed, dict):
        raise ValueError(f"must be a JSON object, got a JSON {type(parsed).__name__}")
    return value


class TailoredResumeRecord(BaseModel):
    """One stored version of a tailored resume for a job (table `tailored_resumes`).

    `plan_json` is the LLM's `TailoringPlan` and `changes_json` the `ChangeSet`
    computed from it, both serialized as JSON objects. They hold resume
    content, so validation errors never echo the input.
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    id: int | None = None
    platform: Platform
    external_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    language: Language
    llm_model: str = Field(min_length=1)
    plan_json: str
    changes_json: str
    clean_pdf_path: str | None = None
    review_pdf_path: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    approved_at: datetime | None = None

    @field_validator("plan_json", "changes_json")
    @classmethod
    def _json_objects(cls, value: str) -> str:
        """Require both JSON columns to hold a JSON object."""
        return _require_json_object(value)

    @field_validator("created_at", "approved_at")
    @classmethod
    def _datetimes_utc(cls, value: datetime | None) -> datetime | None:
        """Store datetimes in UTC; naive datetimes are assumed to already be UTC."""
        return None if value is None else _as_utc(value)


class ApplicationRecord(BaseModel):
    """One application attempt for a job with a specific tailored resume (table `applications`).

    `submitted` is True only when the board confirmed the submission; `error`
    describes why an attempt failed.
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    id: int | None = None
    platform: Platform
    external_id: str = Field(min_length=1)
    resume_id: int = Field(ge=1)
    attempted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    submitted: bool
    receipt_path: str | None = None
    error: str | None = None

    @field_validator("attempted_at")
    @classmethod
    def _attempted_at_utc(cls, value: datetime) -> datetime:
        """Store `attempted_at` in UTC; naive datetimes are assumed to already be UTC."""
        return _as_utc(value)
