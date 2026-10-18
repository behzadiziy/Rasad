"""Tests for probable-duplicate detection (`src/duplicates.py`) and the duplicates line in notifications.

The pairs come from the first real run (public job postings). The «ستاپ
ترک» pair is parsed from the captured search fixtures; the others are built
from the titles and company names the task lists. Nothing touches the network.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.duplicates import (
    assign_duplicate_groups,
    company_keys,
    find_prior_activity,
    is_probable_duplicate,
    title_tokens,
)
from src.models import AppConfig, JobPost, JobStatus, Settings, WorkMode
from src.notifier import telegram
from src.notifier.telegram import TelegramNotifier
from src.scrapers.jobinja import parse_search_html
from src.scrapers.jobvision import parse_search_payload

_FIXTURES = Path(__file__).parent / "fixtures"
_ALIASES = {"mashhad": ["mashhad", "مشهد"], "tehran": ["tehran", "تهران"]}
_T0 = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
_ANONYMOUS = "سازمانی فعال در حوزه تولیدی / صنعتی"  # tests/fixtures/jobvision (job 1566821)


def _config(**duplicates: Any) -> AppConfig:
    return AppConfig.model_validate(
        {
            "scheduler": {"interval_minutes": 30, "active_hours": "08:00-23:00", "timezone": "Asia/Tehran"},
            "scraping": {"enabled_platforms": ["jobvision", "jobinja"], "max_pages_per_keyword": 1},
            "city_aliases": _ALIASES,
            "search_profiles": [{"profile_name": "AI", "keywords": ["AI"]}],
            "duplicates": duplicates,
        }
    )


def _job(platform: str, external_id: str, title: str, company: str, *, days: float = 0, **overrides: Any) -> JobPost:
    data: dict[str, Any] = {
        "platform": platform,
        "external_id": external_id,
        "title": title,
        "company": company,
        "city": "مشهد",
        "work_mode": WorkMode.ONSITE,
        "url": f"https://{platform}.ir/jobs/{external_id}",
        "created_at": _T0 + timedelta(days=days),
    }
    data.update(overrides)
    return JobPost(**data)


def _fixture_jobs() -> dict[str, JobPost]:
    """Parse both captured search samples, keyed by external ID."""
    jobinja = parse_search_html((_FIXTURES / "jobinja" / "search_page.html").read_text(encoding="utf-8"))
    record = json.loads((_FIXTURES / "jobvision" / "search_response.json").read_text(encoding="utf-8"))
    jobvision = parse_search_payload(record["body"])
    return {job.external_id: job for job in [*jobinja, *jobvision]}


def _asia_law_pair() -> tuple[JobPost, JobPost]:
    return (
        _job("jobvision", "v-asia", "AI Engineer (LLM & RAG)", "گروه حقوق آسیا"),
        _job("jobinja", "j-asia", "LLM/RAG) AI Engineer-مشهد)", "گروه حقوق آسیا | Asia Law Group", days=1),
    )


# ── company_keys ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("company", "keys"),
    [
        ("ستاپ ترک | SetupTrack", {"ستاپترک", "setuptrack"}),
        ("ستاپ ترک", {"ستاپترک"}),
        ("گروه حقوق آسیا | Asia Law Group", {"حقوقآسیا", "asialaw"}),
        ("شرکت آریا اندیش سورنا", {"آریااندیشسورنا"}),
        ("پانافر｜Panafor", {"پانافر", "panafor"}),
        ("Acme Co. | ACME Ltd", {"acme"}),
        ("هلدینگ موسسه سازمان مثال | Example Group Inc", {"مثال", "example"}),
        ("شركت ستاپ ترك", {"ستاپترک"}),  # Arabic Kaf normalizes to Keheh
        ("آریا‌اندیش", {"آریااندیش"}),
    ],
)
def test_company_keys(company: str, keys: set[str]) -> None:
    assert company_keys(company) == frozenset(keys)


@pytest.mark.parametrize(
    "company",
    [
        _ANONYMOUS,
        "سازمانی فعال در حوزه فناوری اطلاعات | سازمانی فعال در حوزه فناوری اطلاعات",
        "شرکتی فعال در زمینه بازرگانی",
        "",
        "   ",
        "شرکت",
        "Group Inc | گروه",
    ],
)
def test_anonymous_or_generic_names_have_no_keys(company: str) -> None:
    assert company_keys(company) == frozenset()


def test_the_fixture_anonymous_employer_is_detected() -> None:
    assert _fixture_jobs()["1566821"].company == _ANONYMOUS
    assert company_keys(_fixture_jobs()["1566821"].company) == frozenset()


# ── title_tokens ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("title", "tokens"),
    [
        ("AI Engineer (LLM)", {"ai", "engineer", "llm"}),
        ("مهندس هوش مصنوعی AI Engineer (LLM-دورکاری)", {"مهندس", "هوش", "مصنوعی", "ai", "engineer", "llm"}),
        ("LLM/RAG) AI Engineer-مشهد)", {"llm", "rag", "ai", "engineer"}),
        ("استخدام فوری کارشناس فروش (خانم - تمام وقت - تهران)", {"کارشناس", "فروش"}),
        ("برنامه نویس پاره وقت (آقا) Remote", {"برنامه", "نویس"}),
        ("توسعه‌دهنده دور‌کاری", {"توسعه", "دهنده"}),
        ("C# / .NET Developer", {"net", "developer"}),
        ("Python_Developer ۲", {"python", "developer"}),
    ],
)
def test_title_tokens(title: str, tokens: set[str]) -> None:
    assert title_tokens(title, city_aliases=_ALIASES) == frozenset(tokens)


def test_title_tokens_drop_only_configured_cities() -> None:
    assert title_tokens("Data Engineer Tehran", city_aliases={}) == {"data", "engineer", "tehran"}
    assert title_tokens("Data Engineer Tehran", city_aliases=_ALIASES) == {"data", "engineer"}
    assert title_tokens("Data Engineer Bandar Abbas", city_aliases={"ba": ["bandar abbas"]}) == {"data", "engineer"}


# ── is_probable_duplicate ────────────────────────────────────────


def test_setuptrack_postings_on_both_boards_match() -> None:
    jobs = _fixture_jobs()
    jobvision, jobinja = jobs["1535897"], jobs["tNhZ"]
    assert (jobvision.title, jobvision.company) == ("AI Engineer (LLM)", "ستاپ ترک")
    assert (jobinja.title, jobinja.company) == ("مهندس هوش مصنوعی AI Engineer (LLM-دورکاری)", "ستاپ ترک | SetupTrack")
    assert is_probable_duplicate(jobvision, jobinja, _config())
    assert is_probable_duplicate(jobinja, jobvision, _config())


def test_asia_law_postings_on_both_boards_match() -> None:
    jobvision, jobinja = _asia_law_pair()
    assert is_probable_duplicate(jobvision, jobinja, _config())
    assert is_probable_duplicate(jobinja, jobvision, _config())


def test_other_fixture_postings_do_not_match() -> None:
    jobs = list(_fixture_jobs().values())
    matches = {
        frozenset((a.external_id, b.external_id))
        for a in jobs
        for b in jobs
        if a is not b and is_probable_duplicate(a, b, _config())
    }
    assert matches == {frozenset(("1535897", "tNhZ"))}


@pytest.mark.parametrize(
    ("a", "b"),
    [
        pytest.param(
            _job("jobvision", "1", "Data Engineer", "گروه حقوق آسیا"),
            _job("jobinja", "2", "AI Engineer (LLM & RAG)", "گروه حقوق آسیا | Asia Law Group"),
            id="different-position-same-company",
        ),
        pytest.param(
            _job("jobvision", "1", "AI Engineer", "شرکت آریا اندیش سورنا"),
            _job("jobinja", "2", "مهندس ارشد نرم‌افزار", "شرکت آریا اندیش سورنا"),
            id="no-shared-title-tokens",
        ),
        pytest.param(
            _job("jobvision", "1", "کارشناس فروش و بازاریابی", _ANONYMOUS),
            _job("jobvision", "2", "کارشناس فروش و بازاریابی", _ANONYMOUS),
            id="anonymous-company",
        ),
        pytest.param(
            _job("jobvision", "1", "Developer", "ستاپ ترک"),
            _job("jobinja", "2", "Developer", "ستاپ ترک | SetupTrack"),
            id="one-token-title",
        ),
        pytest.param(
            _job("jobvision", "1", "AI (دورکاری)", "ستاپ ترک"),
            _job("jobinja", "2", "AI Engineer - تهران", "ستاپ ترک | SetupTrack"),
            id="one-token-after-noise",
        ),
        pytest.param(
            _job("jobvision", "1", "AI Engineer (LLM)", "ستاپ ترک"),
            _job("jobinja", "2", "AI Engineer (LLM)", "ستاپ ترک | SetupTrack", days=46),
            id="outside-the-time-window",
        ),
        pytest.param(
            _job("jobvision", "1", "AI Engineer (LLM)", "ستاپ ترک"),
            _job("jobinja", "2", "AI Engineer (LLM)", "پانافر | Panafor"),
            id="different-company",
        ),
    ],
)
def test_must_not_match(a: JobPost, b: JobPost) -> None:
    assert not is_probable_duplicate(a, b, _config())
    assert not is_probable_duplicate(b, a, _config())


def test_time_window_boundary_and_setting() -> None:
    first = _job("jobvision", "1", "AI Engineer (LLM)", "ستاپ ترک")
    assert is_probable_duplicate(first, _job("jobinja", "2", "AI Engineer (LLM)", "ستاپ ترک", days=45), _config())
    assert not is_probable_duplicate(first, _job("jobinja", "2", "AI Engineer", "ستاپ ترک", days=46), _config())
    assert not is_probable_duplicate(
        first, _job("jobinja", "2", "AI Engineer (LLM)", "ستاپ ترک", days=11), _config(window_days=10)
    )


def test_title_overlap_threshold() -> None:
    a = _job("jobvision", "1", "AI Engineer (LLM & RAG)", "ستاپ ترک")
    b = _job("jobinja", "2", "AI Engineer Python Django", "ستاپ ترک")  # 2 of 4 shared: 0.5
    c = _job("jobinja", "3", "AI Engineer LLM Python Django", "ستاپ ترک")  # 3 of 4 shared: 0.75
    d = _job("jobinja", "4", "AI Engineer LLM RAG Python", "ستاپ ترک")  # 4 of 4 shared: 1.0
    assert not is_probable_duplicate(a, b, _config())
    assert not is_probable_duplicate(a, c, _config())
    assert is_probable_duplicate(a, d, _config())
    assert is_probable_duplicate(a, c, _config(title_overlap_threshold=0.75))
    assert is_probable_duplicate(a, b, _config(title_overlap_threshold=0.5))


def test_repost_on_the_same_platform_matches_but_a_job_never_matches_itself() -> None:
    first = _job("jobinja", "1", "AI Engineer (LLM)", "ستاپ ترک | SetupTrack")
    repost = _job("jobinja", "2", "AI Engineer (LLM)", "ستاپ ترک | SetupTrack", days=20)
    assert is_probable_duplicate(first, repost, _config())
    assert not is_probable_duplicate(first, first, _config())
    assert not is_probable_duplicate(first, first.model_copy(update={"title": "AI Engineer LLM"}), _config())


# ── assign_duplicate_groups ──────────────────────────────────────


def test_pair_is_grouped_under_the_earliest_member() -> None:
    jobvision, jobinja = _asia_law_pair()
    assert assign_duplicate_groups([jobinja], [jobvision], _config()) == {
        ("jobvision", "v-asia"): "jobvision:v-asia",
        ("jobinja", "j-asia"): "jobvision:v-asia",
    }


def test_groups_are_transitive() -> None:
    # Each posting is within 45 days of the next, but the first and last are 80 days apart.
    jobs = [_job("jobinja", str(day), "AI Engineer (LLM)", "ستاپ ترک", days=day) for day in (0, 40, 80)]
    assert not is_probable_duplicate(jobs[0], jobs[2], _config())
    assert assign_duplicate_groups(jobs, [], _config()) == {
        ("jobinja", "0"): "jobinja:0",
        ("jobinja", "40"): "jobinja:0",
        ("jobinja", "80"): "jobinja:0",
    }


def test_a_new_posting_joins_a_stored_group_and_only_it_is_returned() -> None:
    stored = [
        _job("jobvision", "1", "AI Engineer (LLM)", "ستاپ ترک", duplicate_group="jobvision:1"),
        _job("jobinja", "2", "AI Engineer (LLM)", "ستاپ ترک | SetupTrack", days=1, duplicate_group="jobvision:1"),
        _job("jobinja", "9", "Accountant Senior", "ستاپ ترک", days=2),
    ]
    new = _job("jobinja", "3", "AI Engineer LLM", "SetupTrack", days=30)
    assert assign_duplicate_groups([new], stored, _config()) == {("jobinja", "3"): "jobvision:1"}
    assert assign_duplicate_groups([], stored, _config()) == {}


def test_a_new_posting_can_merge_two_groups() -> None:
    llm = [
        _job("jobvision", "a", "AI Engineer (LLM)", "ستاپ ترک", duplicate_group="jobvision:a"),
        _job("jobinja", "a2", "AI Engineer LLM", "ستاپ ترک", days=1, duplicate_group="jobvision:a"),
    ]
    ml = [
        _job("jobvision", "b", "Machine Learning Engineer RAG", "ستاپ ترک", days=5, duplicate_group="jobvision:b"),
        _job("jobinja", "b2", "Machine Learning Engineer (RAG)", "ستاپ ترک", days=6, duplicate_group="jobvision:b"),
    ]
    assert not is_probable_duplicate(llm[0], ml[0], _config())
    bridge = _job("jobinja", "c", "AI Machine Learning Engineer LLM RAG", "ستاپ ترک", days=10)

    assert assign_duplicate_groups([bridge], [*llm, *ml], _config()) == {
        ("jobvision", "b"): "jobvision:a",
        ("jobinja", "b2"): "jobvision:a",
        ("jobinja", "c"): "jobvision:a",
    }


def test_earliest_member_ties_are_broken_by_platform_and_id() -> None:
    jobs = [
        _job("jobvision", "2", "AI Engineer (LLM)", "ستاپ ترک"),
        _job("jobvision", "1", "AI Engineer (LLM)", "ستاپ ترک"),
        _job("jobinja", "9", "AI Engineer (LLM)", "ستاپ ترک"),
    ]
    assert set(assign_duplicate_groups(jobs, [], _config()).values()) == {"jobinja:9"}
    assert set(assign_duplicate_groups(list(reversed(jobs)), [], _config()).values()) == {"jobinja:9"}


def test_singletons_and_repeated_keys_are_not_returned() -> None:
    lonely = _job("jobinja", "1", "AI Engineer (LLM)", "ستاپ ترک", duplicate_group="jobinja:1")
    other = _job("jobinja", "2", "Accountant", "ستاپ ترک")
    # A stale group of one is left as it is: groups are never dissolved here.
    assert assign_duplicate_groups([lonely, other], [lonely, other], _config()) == {}


def test_anonymous_postings_are_never_grouped() -> None:
    jobs = [_job("jobvision", str(index), "کارشناس فروش و بازاریابی", _ANONYMOUS, days=index) for index in range(3)]
    assert assign_duplicate_groups(jobs, [], _config()) == {}


# ── find_prior_activity ──────────────────────────────────────────


def _member(external_id: str, status: JobStatus, *, days: float = 0) -> JobPost:
    return _job("jobinja", external_id, "AI Engineer (LLM)", "ستاپ ترک", days=days, status=status)


def test_prior_activity_returns_the_most_advanced_other_member() -> None:
    job = _member("me", JobStatus.NOTIFIED)
    members = [job, _member("1", JobStatus.NOTIFIED), _member("2", JobStatus.TAILORED), _member("3", JobStatus.SKIPPED)]
    assert find_prior_activity(job, members) == members[2]

    ranked = [
        _member("t", JobStatus.TAILORED),
        _member("a", JobStatus.APPROVED),
        _member("f", JobStatus.APPLY_FAILED),
        _member("p", JobStatus.APPLIED),
    ]
    for count in range(1, len(ranked) + 1):
        assert find_prior_activity(job, ranked[:count]) == ranked[count - 1]


def test_prior_activity_ignores_the_job_itself_and_inactive_statuses() -> None:
    job = _member("me", JobStatus.APPLIED)
    inactive = [_member(status.value, status) for status in (JobStatus.NEW, JobStatus.NOTIFIED, JobStatus.SKIPPED)]
    assert find_prior_activity(job, [job, *inactive]) is None
    assert find_prior_activity(job, [job.model_copy(update={"title": "copy"})]) is None
    assert find_prior_activity(job, []) is None


def test_prior_activity_tie_prefers_the_earliest_member() -> None:
    job = _member("me", JobStatus.NOTIFIED)
    later, earlier = _member("2", JobStatus.TAILORED, days=5), _member("1", JobStatus.TAILORED, days=1)
    assert find_prior_activity(job, [later, earlier]) == earlier


# ── Notification text ────────────────────────────────────────────


def test_message_without_duplicates_is_unchanged() -> None:
    job = _asia_law_pair()[0]
    assert telegram._render_message(job, []) == telegram._render_message(job)
    assert "🔁" not in telegram._render_message(job)
    assert "🔁" not in telegram._render_message(job, [job])


def test_message_links_to_the_other_postings_by_platform_name() -> None:
    jobvision, jobinja = _asia_law_pair()
    repost = _job("jobinja", "j-asia-2", "AI Engineer (LLM & RAG)", "گروه حقوق آسیا", days=10)
    text = telegram._render_message(jobvision, [jobvision, jobinja, repost, jobinja])

    last_line = text.splitlines()[-1]
    assert last_line == (
        "🔁 احتمالاً تکراری: "
        '<a href="https://jobinja.ir/jobs/j-asia">Jobinja</a>، '
        '<a href="https://jobinja.ir/jobs/j-asia-2">Jobinja</a>'
    )
    assert "https://jobvision.ir/jobs/v-asia" not in text  # the job itself is not listed


def test_duplicate_urls_are_escaped_for_the_html_attribute() -> None:
    job = _asia_law_pair()[0]
    tricky = _job("jobinja", "x", "AI Engineer", "گروه حقوق آسیا", url='https://jobinja.ir/jobs/x?a=1&b="2"<script>\'')
    last_line = telegram._render_message(job, [tricky]).splitlines()[-1]
    assert 'href="https://jobinja.ir/jobs/x?a=1&amp;b=&quot;2&quot;&lt;script&gt;&#x27;"' in last_line
    assert "<script>" not in last_line


def test_non_http_urls_are_not_linked() -> None:
    job = _asia_law_pair()[0]
    odd = _job("jobvision", "x", "AI Engineer", "گروه حقوق آسیا", url="javascript:alert(1)")
    assert telegram._render_message(job, [odd]).splitlines()[-1] == "🔁 احتمالاً تکراری: JobVision"


def test_duplicates_line_never_exceeds_the_message_limit() -> None:
    job = _job("jobinja", "big", "A" * 500, "B" * 500, salary="&" * 500, matched_profiles=["P" * 500])
    many = [
        _job("jobvision", str(index), "AI Engineer", "B", url=f"https://jobvision.ir/jobs/{index}?q=" + "&" * 150)
        for index in range(40)
    ]
    text = telegram._render_message(job, many)
    assert len(text) <= telegram._MAX_MESSAGE_CHARS
    last_line = text.splitlines()[-1]
    assert last_line.startswith("🔁 احتمالاً تکراری:")
    assert last_line.endswith("آگهی دیگر")
    shown = last_line.count("<a href=")
    assert 0 < shown < len(many)
    assert f" و {len(many) - shown} آگهی دیگر" in last_line


def test_duplicates_summary_when_no_link_fits() -> None:
    job = _asia_law_pair()[0]
    huge = _job("jobvision", "x", "AI Engineer", "B", url="https://jobvision.ir/jobs/x?q=" + "a" * 5000)
    assert telegram._render_message(job, [huge]).splitlines()[-1] == "🔁 احتمالاً تکراری: 1 آگهی"


def test_send_job_puts_the_duplicates_in_the_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads: list[dict[str, Any]] = []

    async def capture(self: TelegramNotifier, payload: dict[str, Any]) -> None:
        payloads.append(payload)

    monkeypatch.setattr(TelegramNotifier, "_send_with_retries", capture)
    monkeypatch.setattr(telegram, "_MIN_SEND_INTERVAL_S", 0.0)
    settings = Settings(_env_file=None, TELEGRAM_BOT_TOKEN="123:test-token", TELEGRAM_CHAT_ID="@test_channel")
    jobvision, jobinja = _asia_law_pair()

    async def scenario() -> tuple[bool, bool]:
        async with TelegramNotifier(settings) as notifier:
            return await notifier.send_job(jobvision, duplicates=[jobinja]), await notifier.send_job(jobinja)

    assert asyncio.run(scenario()) == (True, True)
    assert payloads[0]["text"].endswith('🔁 احتمالاً تکراری: <a href="https://jobinja.ir/jobs/j-asia">Jobinja</a>')
    assert payloads[0]["parse_mode"] == "HTML"
    assert "🔁" not in payloads[1]["text"]
