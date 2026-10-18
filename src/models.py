"""Data models: environment settings, YAML app configuration, and job posts."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from loguru import logger
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.utils import normalize_text

Platform = Literal["jobvision", "jobinja"]

_ALL_CITIES = "ALL"

_ACTIVE_HOURS_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")


class JobStatus(StrEnum):
    """Lifecycle state of a stored job."""

    NEW = "NEW"
    NOTIFIED = "NOTIFIED"
    TAILORED = "TAILORED"
    APPLIED = "APPLIED"
    IGNORED = "IGNORED"


class WorkMode(StrEnum):
    """Where the work is performed."""

    ONSITE = "ONSITE"
    HYBRID = "HYBRID"
    REMOTE = "REMOTE"


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

    @field_validator("TELEGRAM_PROXY_URL", mode="before")
    @classmethod
    def _blank_proxy_is_none(cls, value: Any) -> Any:
        """Treat an empty or whitespace-only proxy URL as "no proxy"."""
        if isinstance(value, str) and not value.strip():
            return None
        return value


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
    """A named set of keywords, exclusions, and location rules."""

    profile_name: str = Field(min_length=1)
    keywords: list[str] = Field(min_length=1)
    exclude_keywords: list[str] = Field(default_factory=list)
    location_rules: list[LocationRule] = Field(default_factory=list)

    @field_validator("keywords", "exclude_keywords")
    @classmethod
    def _no_blank_terms(cls, value: list[str]) -> list[str]:
        """Reject terms that are empty after normalization (they would match everything)."""
        blank = [term for term in value if not normalize_text(term)]
        if blank:
            raise ValueError(f"keywords must not be blank after normalization, got {blank!r}")
        return value

    @model_validator(mode="after")
    def _at_most_one_all_rule(self) -> SearchProfile:
        """Allow at most one "ALL" location rule per profile."""
        all_rules = sum(1 for rule in self.location_rules if rule.city == _ALL_CITIES)
        if all_rules > 1:
            raise ValueError(
                f"Profile {self.profile_name!r} has {all_rules} '{_ALL_CITIES}' location rules; at most one is allowed"
            )
        return self


class AppConfig(BaseModel):
    """The full application configuration loaded from `config/config.yaml`."""

    scheduler: SchedulerConfig
    scraping: ScrapingConfig
    city_aliases: dict[str, list[str]] = Field(default_factory=dict)
    search_profiles: list[SearchProfile] = Field(min_length=1)

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
    return config


class JobPost(BaseModel):
    """A single job listing as scraped from a platform."""

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
