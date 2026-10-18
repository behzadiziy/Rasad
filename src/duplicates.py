"""Probable-duplicate detection: one position posted on both boards, or reposted on one.

Two jobs are probable duplicates (`is_probable_duplicate`) when all three hold:
1. their company names share a key (`company_keys`); anonymous names never match,
2. both titles have at least 2 meaningful tokens (`title_tokens`), and the
   overlap coefficient |A ∩ B| / min(|A|, |B|) reaches
   `duplicates.title_overlap_threshold`; it tolerates bilingual titles, where
   one title also contains a translation of the other,
3. they were first seen within `duplicates.window_days` of each other.

`assign_duplicate_groups` links duplicates transitively (union-find) into
groups named after their earliest member, and `find_prior_activity` tells
whether the user already acted on another posting of a group. Duplicates are
labeled, never hidden.

All functions are pure: they read and change nothing. Matching uses
`normalize_text`; stored and displayed text keeps its original form.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from src.models import AppConfig, JobPost, JobStatus
from src.utils import normalize_text

_JobKey = tuple[str, str]

# Anything that is not a letter or a digit separates tokens (underscore included).
_TOKEN_SPLIT_RE = re.compile(r"[\W_]+")

# Jobinja lists companies as "Persian name | English name".
# Source: tests/fixtures/jobinja/search_page.html («ستاپ ترک | SetupTrack»).
_COMPANY_PART_SPLIT_RE = re.compile(r"[|｜]")

_GENERIC_COMPANY_WORDS: frozenset[str] = frozenset(
    normalize_text(word)
    for word in ("شرکت", "گروه", "هلدینگ", "موسسه", "سازمان", "co", "company", "group", "inc", "ltd")
)

# Employers that hide their name; matched at the start of a normalized company name.
_ANONYMOUS_COMPANY_RE = re.compile(
    r"^(?:"
    # Source: tests/fixtures/jobvision/search_response.json and search_page.html (job 1566821,
    # company id 0): «سازمانی فعال در حوزه تولیدی / صنعتی». data/jobs.db held no jobs when
    # Task 7b was built, so the fixtures are the only real sample.
    r"سازمانی فعال در حوزه"
    # VERIFY: assumed variants of the same wording («شرکتی» / «مجموعه‌ای», «زمینه»). Confirm with
    # `SELECT DISTINCT company FROM jobs WHERE platform = 'jobvision'` on the real database.
    r"|(?:سازمانی|شرکتی|مجموعه ای) فعال در (?:حوزه|زمینه)"
    r")"
)

# Tokens that say nothing about the position (city aliases from config are added per call).
# «دور کاری» with a ZWNJ normalizes to two tokens, hence «دور» and «کاری».
_TITLE_NOISE_WORDS: frozenset[str] = frozenset(
    normalize_text(word)
    for word in (
        "دورکاری", "دور", "کاری", "remote",
        "آقا", "خانم",
        "فوری", "استخدام", "تمام", "وقت", "پاره",
    )
)  # fmt: skip

_MIN_TITLE_TOKENS = 2
# Absorbs float rounding, so an overlap of exactly the threshold (e.g. 4/5 at 0.8) passes.
_OVERLAP_EPSILON = 1e-9

# Statuses that show the user acted on a job, least to most advanced.
_ACTIVITY_RANK: dict[JobStatus, int] = {
    JobStatus.TAILORED: 1,
    JobStatus.APPROVED: 2,
    JobStatus.APPLY_FAILED: 3,
    JobStatus.APPLIED: 4,
}


def _job_key(job: JobPost) -> _JobKey:
    """Return the `(platform, external_id)` key of a job."""
    return job.platform, job.external_id


def _tokens(text: str) -> list[str]:
    """Normalize `text` and split it on anything that is not a letter or a digit."""
    return [token for token in _TOKEN_SPLIT_RE.split(normalize_text(text)) if token]


def company_keys(company: str) -> frozenset[str]:
    """Return the comparison keys of a company name; two companies match if their keys intersect.

    Each `|`- or `｜`-separated part is normalized, generic words such as
    «شرکت» or "group" are removed, and the rest is joined without spaces or
    punctuation: «ستاپ ترک | SetupTrack» → {"ستاپترک", "setuptrack"}.
    Anonymous names (e.g. JobVision's «سازمانی فعال در حوزه …») and names
    made only of generic words return an empty set, which never matches.
    """
    keys: set[str] = set()
    for part in _COMPANY_PART_SPLIT_RE.split(company):
        if _ANONYMOUS_COMPANY_RE.match(normalize_text(part)):
            return frozenset()
        key = "".join(token for token in _tokens(part) if token not in _GENERIC_COMPANY_WORDS)
        if key:
            keys.add(key)
    return frozenset(keys)


def _title_noise(city_aliases: Mapping[str, list[str]]) -> frozenset[str]:
    """Return the fixed noise tokens plus the tokens of every city key and alias."""
    city_tokens = {
        token for city, aliases in city_aliases.items() for name in (city, *aliases) for token in _tokens(name)
    }
    return _TITLE_NOISE_WORDS | city_tokens


def _meaningful_tokens(title: str, noise: frozenset[str]) -> frozenset[str]:
    """Return the title's tokens without noise tokens and one-character tokens."""
    return frozenset(token for token in _tokens(title) if len(token) > 1 and token not in noise)


def title_tokens(title: str, *, city_aliases: Mapping[str, list[str]]) -> frozenset[str]:
    """Return the tokens of a title that describe the position.

    The title is normalized and split on anything that is not a letter or a
    digit. City names (the keys and aliases of `city_aliases`), remote-work
    words («دورکاری», «remote»), «آقا»/«خانم», words such as «فوری» and
    «تمام وقت», and one-character tokens are dropped.
    """
    return _meaningful_tokens(title, _title_noise(city_aliases))


@dataclass(frozen=True, slots=True)
class _Fingerprint:
    """What duplicate detection compares, computed once per job."""

    key: _JobKey
    companies: frozenset[str]
    tokens: frozenset[str]
    created_at: datetime


def _fingerprint(job: JobPost, noise: frozenset[str]) -> _Fingerprint:
    """Compute a job's company keys and title tokens."""
    return _Fingerprint(
        key=_job_key(job),
        companies=company_keys(job.company),
        tokens=_meaningful_tokens(job.title, noise),
        created_at=job.created_at,
    )


def _same_position(a: _Fingerprint, b: _Fingerprint, *, window: timedelta, threshold: float) -> bool:
    """Apply the three duplicate conditions to two different jobs."""
    if a.key == b.key or not (a.companies & b.companies):
        return False
    smaller = min(len(a.tokens), len(b.tokens))
    if smaller < _MIN_TITLE_TOKENS:
        return False
    if len(a.tokens & b.tokens) / smaller + _OVERLAP_EPSILON < threshold:
        return False
    return abs(a.created_at - b.created_at) <= window


def is_probable_duplicate(a: JobPost, b: JobPost, config: AppConfig) -> bool:
    """Return True if two different jobs are probably the same position.

    All three must hold: the company key sets intersect; both titles have at
    least 2 tokens and their overlap coefficient is at least
    `duplicates.title_overlap_threshold`; and their `created_at` values are
    within `duplicates.window_days` of each other. The platforms may differ
    (cross-posting) or be the same (a repost). A job is never a duplicate of itself.
    """
    noise = _title_noise(config.city_aliases)
    settings = config.duplicates
    return _same_position(
        _fingerprint(a, noise),
        _fingerprint(b, noise),
        window=timedelta(days=settings.window_days),
        threshold=settings.title_overlap_threshold,
    )


def _unique_jobs(jobs: Iterable[JobPost]) -> list[JobPost]:
    """Drop repeated `(platform, external_id)` keys, keeping the first occurrence."""
    unique: dict[_JobKey, JobPost] = {}
    for job in jobs:
        unique.setdefault(_job_key(job), job)
    return list(unique.values())


def _earliest_first(job: JobPost) -> tuple[datetime, str, str]:
    """Sort key: oldest `created_at` first, ties broken by platform and external ID."""
    return job.created_at, job.platform, job.external_id


def assign_duplicate_groups(
    candidates: Sequence[JobPost],
    existing: Sequence[JobPost],
    config: AppConfig,
) -> dict[tuple[str, str], str]:
    """Group probable duplicates among `candidates` and `existing`, and return the groups that changed.

    Pairs that pass `is_probable_duplicate` are linked transitively with
    union-find. Each group of two or more jobs is named
    `"{platform}:{external_id}"` after its earliest member (by `created_at`),
    so the name stays stable as later postings join. Only jobs whose
    `duplicate_group` differs from their new group are returned, keyed by
    `(platform, external_id)`. A job that ends up in no group is never
    returned: groups are only formed, extended, or merged here.

    A key that appears more than once is used once, first occurrence winning.
    Pass every member of any stored group the jobs belong to, or a group
    may get a different (later) earliest member.
    """
    jobs = _unique_jobs([*candidates, *existing])
    noise = _title_noise(config.city_aliases)
    prints = [_fingerprint(job, noise) for job in jobs]
    window = timedelta(days=config.duplicates.window_days)
    threshold = config.duplicates.title_overlap_threshold

    parent = list(range(len(jobs)))

    def find(index: int) -> int:
        """Return the root of `index`'s set, halving the path on the way."""
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    # Only jobs that share a company key can match, so compare within those buckets.
    by_company: dict[str, list[int]] = defaultdict(list)
    for index, fingerprint in enumerate(prints):
        for key in fingerprint.companies:
            by_company[key].append(index)
    for indices in by_company.values():
        for position, first in enumerate(indices):
            for second in indices[position + 1 :]:
                first_root, second_root = find(first), find(second)
                if first_root != second_root and _same_position(
                    prints[first], prints[second], window=window, threshold=threshold
                ):
                    parent[second_root] = first_root

    components: dict[int, list[JobPost]] = defaultdict(list)
    for index, job in enumerate(jobs):
        components[find(index)].append(job)

    changes: dict[tuple[str, str], str] = {}
    for members in components.values():
        if len(members) < 2:
            continue
        anchor = min(members, key=_earliest_first)
        group = f"{anchor.platform}:{anchor.external_id}"
        for member in members:
            if member.duplicate_group != group:
                changes[_job_key(member)] = group
    return changes


def find_prior_activity(job: JobPost, members: Sequence[JobPost]) -> JobPost | None:
    """Return the other group member the user acted on most, or None.

    Activity means `TAILORED`, `APPROVED`, `APPLY_FAILED`, or `APPLIED`, from
    least to most advanced. `job` itself (matched by platform and external ID)
    is skipped; among members with the same status, the earliest wins.
    """
    active = [
        member for member in members if _job_key(member) != _job_key(job) and member.status in _ACTIVITY_RANK
    ]
    if not active:
        return None
    return min(active, key=lambda member: (-_ACTIVITY_RANK[member.status], *_earliest_first(member)))
