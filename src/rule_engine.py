"""Local, Persian-aware Rule Engine that decides which search profiles a job matches.

A profile matches only if all three checks pass, in order:
1. keywords (matched as whole words): a keyword from any synonym group
   appears in the title, or the description contains terms from at least
   `description_min_hits` distinct groups; repeating a term, or using two
   synonyms of one group, counts once,
2. no exclusion keyword appears in the title,
3. the job's city and work mode satisfy the profile's location rules.

Job text and keywords are normalized with the same `normalize_text` function.
DEBUG logs name the keyword groups that matched, which helps when tuning
`exclude_keywords` and `description_min_hits`.
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
    """A search profile with its patterns precompiled.

    `keyword_re` matches any term of any group (the title check); `group_res`
    holds one pattern per synonym group (the description check), and
    `group_labels` names each group in debug logs.
    """

    profile: SearchProfile
    keyword_re: re.Pattern[str] | None
    group_res: tuple[re.Pattern[str], ...]
    group_labels: tuple[str, ...]
    exclude_re: re.Pattern[str] | None
    rules: tuple[LocationRule, ...]


@dataclass(frozen=True, slots=True)
class _PreparedJob:
    """Normalized job fields and resolved canonical cities, computed once per job."""

    title: str
    description: str
    cities: frozenset[str]
    work_mode: WorkMode


def _keyword_groups(profile: SearchProfile) -> tuple[tuple[str, ...], ...]:
    """Return the profile's keyword synonym groups; a plain string is a group of one."""
    return tuple((group,) if isinstance(group, str) else tuple(group) for group in profile.keywords)


def _compile_profile(profile: SearchProfile) -> _CompiledProfile:
    """Precompile the keyword, synonym-group, and exclusion patterns of a profile."""
    groups = _keyword_groups(profile)
    group_res: list[re.Pattern[str]] = []
    group_labels: list[str] = []
    for terms in groups:
        pattern = _compile_alternation(terms)
        if pattern is not None:
            group_res.append(pattern)
            group_labels.append("|".join(terms))
    return _CompiledProfile(
        profile=profile,
        keyword_re=_compile_alternation(term for terms in groups for term in terms),
        group_res=tuple(group_res),
        group_labels=tuple(group_labels),
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

        if not self._keywords_ok(compiled, prepared, job_ref):
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
    def _keywords_ok(compiled: _CompiledProfile, prepared: _PreparedJob, job_ref: str) -> bool:
        """Run the keyword check: any keyword in the title, else enough distinct groups in the description."""
        name = compiled.profile.profile_name
        if compiled.keyword_re is not None and (title_hit := compiled.keyword_re.search(prepared.title)):
            logger.debug("Job {} keyword check of {!r}: title keyword {!r}", job_ref, name, title_hit.group(0))
            return True

        hits = [
            label
            for label, pattern in zip(compiled.group_labels, compiled.group_res, strict=True)
            if pattern.search(prepared.description)
        ]
        needed = compiled.profile.description_min_hits
        if len(hits) < needed:
            logger.debug(
                "Job {} rejected by {!r}: no keyword in the title, and {} of the {} required keyword "
                "group(s) in the description {}",
                job_ref,
                name,
                len(hits),
                needed,
                hits,
            )
            return False
        logger.debug(
            "Job {} keyword check of {!r}: {} description keyword group(s) {} (at least {} required)",
            job_ref,
            name,
            len(hits),
            hits,
            needed,
        )
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
