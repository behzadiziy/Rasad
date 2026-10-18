"""Local, Persian-aware Rule Engine that decides which search profiles a job matches.

A profile matches only if all three checks pass, in order:
1. at least one keyword appears (as a whole word) in the title or description,
2. no exclusion keyword appears in the title,
3. the job's city and work mode satisfy the profile's location rules.

Job text and keywords are normalized with the same `normalize_text` function.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from loguru import logger

from src.models import AppConfig, JobPost, LocationRule, SearchProfile, WorkMode
from src.utils import normalize_text

_ALL_CITIES = "ALL"


def _keyword_regex(keyword: str) -> str:
    """Return the regex body for one keyword (without boundary assertions).

    Internal spaces match any whitespace run. Latin keywords ending in a letter
    may also match a trailing plural ``s`` (``LLM`` matches ``LLMs``).
    """
    normalized = normalize_text(keyword)
    body = r"\s+".join(re.escape(part) for part in normalized.split(" "))
    if normalized.isascii() and normalized[-1].isalpha():
        body += "s?"
    return body


def _compile_alternation(terms: Iterable[str]) -> re.Pattern[str] | None:
    """Compile terms into one word-bounded alternation, or return None when there are no terms."""
    bodies = [_keyword_regex(term) for term in terms if normalize_text(term)]
    if not bodies:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(bodies) + r")(?!\w)")


@dataclass(frozen=True, slots=True)
class _CompiledProfile:
    """A search profile with its patterns precompiled."""

    profile: SearchProfile
    keyword_re: re.Pattern[str] | None
    exclude_re: re.Pattern[str] | None
    rules: tuple[LocationRule, ...]


@dataclass(frozen=True, slots=True)
class _PreparedJob:
    """Normalized job fields and resolved canonical cities, computed once per job."""

    title: str
    description: str
    cities: frozenset[str]
    work_mode: WorkMode


def _compile_profile(profile: SearchProfile) -> _CompiledProfile:
    """Precompile the keyword and exclusion patterns of a profile."""
    return _CompiledProfile(
        profile=profile,
        keyword_re=_compile_alternation(profile.keywords),
        exclude_re=_compile_alternation(profile.exclude_keywords),
        rules=tuple(profile.location_rules),
    )


def _rule_accepts(rule: LocationRule, work_mode: WorkMode) -> bool:
    """Return True if a single location rule accepts the given work mode."""
    return not rule.remote_only or work_mode == WorkMode.REMOTE


class RuleEngine:
    """Matches jobs against the search profiles of an `AppConfig`."""

    def __init__(self, config: AppConfig) -> None:
        """Precompile every keyword, exclusion, and city-alias pattern in the config."""
        self._profiles: dict[str, _CompiledProfile] = {
            profile.profile_name: _compile_profile(profile) for profile in config.search_profiles
        }
        self._city_patterns: dict[str, re.Pattern[str]] = {}
        for city, aliases in config.city_aliases.items():
            pattern = _compile_alternation(aliases)
            if pattern is not None:
                self._city_patterns[city] = pattern
        logger.info(
            "RuleEngine ready: {} profile(s), {} city alias group(s)",
            len(self._profiles),
            len(self._city_patterns),
        )

    def matches(self, job: JobPost, profile: SearchProfile) -> bool:
        """Return True if the job passes the keyword, exclusion, and location checks of the profile."""
        return self._matches_prepared(self._prepare(job), self._compiled_for(profile), job)

    def match_profiles(self, job: JobPost) -> list[str]:
        """Return the names of all matching profiles, in config order."""
        prepared = self._prepare(job)
        matched = [
            name for name, compiled in self._profiles.items() if self._matches_prepared(prepared, compiled, job)
        ]
        logger.debug("Job {}:{} matched profiles {}", job.platform, job.external_id, matched)
        return matched

    def _compiled_for(self, profile: SearchProfile) -> _CompiledProfile:
        """Return the precompiled entry for a profile, compiling it if it is not part of the config."""
        compiled = self._profiles.get(profile.profile_name)
        if compiled is not None and compiled.profile == profile:
            return compiled
        logger.debug("Compiling profile {!r} on the fly (not in config)", profile.profile_name)
        return _compile_profile(profile)

    def _prepare(self, job: JobPost) -> _PreparedJob:
        """Normalize the job's text fields and resolve its canonical cities."""
        city_text = normalize_text(job.city)
        cities = frozenset(city for city, pattern in self._city_patterns.items() if pattern.search(city_text))
        return _PreparedJob(
            title=normalize_text(job.title),
            description=normalize_text(job.raw_description or ""),
            cities=cities,
            work_mode=job.work_mode,
        )

    def _matches_prepared(self, prepared: _PreparedJob, compiled: _CompiledProfile, job: JobPost) -> bool:
        """Run the three checks for one profile against a prepared job."""
        name = compiled.profile.profile_name
        job_ref = f"{job.platform}:{job.external_id}"

        keyword_re = compiled.keyword_re
        if keyword_re is None or not (
            keyword_re.search(prepared.title) or keyword_re.search(prepared.description)
        ):
            logger.debug("Job {} rejected by {!r}: no keyword match", job_ref, name)
            return False

        if compiled.exclude_re is not None and compiled.exclude_re.search(prepared.title):
            logger.debug("Job {} rejected by {!r}: exclusion keyword in title", job_ref, name)
            return False

        if not self._location_ok(compiled.rules, prepared):
            logger.debug(
                "Job {} rejected by {!r}: location (cities={}, work_mode={})",
                job_ref,
                name,
                sorted(prepared.cities),
                prepared.work_mode,
            )
            return False

        return True

    @staticmethod
    def _location_ok(rules: tuple[LocationRule, ...], prepared: _PreparedJob) -> bool:
        """Evaluate location rules: city-specific rules first, then the "ALL" fallback."""
        if not rules:
            return True

        specific = [rule for rule in rules if rule.city != _ALL_CITIES and rule.city in prepared.cities]
        if specific:
            return any(_rule_accepts(rule, prepared.work_mode) for rule in specific)

        fallback = next((rule for rule in rules if rule.city == _ALL_CITIES), None)
        if fallback is None:
            return False
        return _rule_accepts(fallback, prepared.work_mode)
