"""Tests for the Jobinja parsers, run against trimmed copies of real captures."""

import re
from pathlib import Path

from src.models import WorkMode
from src.scrapers.jobinja import (
    _apply_detail,
    _canonical_job_url,
    _merge_work_modes,
    _parse_detail_salary,
    extract_external_id,
    parse_detail_html,
    parse_detail_work_mode,
    parse_search_html,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "jobinja"
_TAG_RE = re.compile(r"<[^>]+>")
_JSON_LD_RE = re.compile(r'<script type="application/ld\+json">.*?</script>', re.DOTALL)

_PART_TIME_ID = "tEuf"
_REMOTE_ID = "tNhZ"
_KARAJ_ID = "tuY0"

# S4: real job URLs from the captured search pages.
_S4_URLS = {
    (
        "https://jobinja.ir/companies/aloostad/jobs/tEuf/%D8%A7%D8%B3%D8%AA%D8%AE%D8%AF%D8%A7%D9%85-%D9%85%D8"
        "%AF%D8%B1%D8%B3-%D9%87%D9%88%D8%B4-%D9%85%D8%B5%D9%86%D9%88%D8%B9%DB%8C-ai-%D8%AF%D8%B1-%D8%A7%D9%84"
        "%D9%88-%D8%A7%D8%B3%D8%AA%D8%A7%D8%AF?_ref=16&_t=36352e3130382e3232372e313635"
    ): "tEuf",
    (
        "https://jobinja.ir/companies/setuptrack/jobs/tNhZ/%D8%A7%D8%B3%D8%AA%D8%AE%D8%AF%D8%A7%D9%85-%D9%85%"
        "D9%87%D9%86%D8%AF%D8%B3-%D9%87%D9%88%D8%B4-%D9%85%D8%B5%D9%86%D9%88%D8%B9%DB%8C-ai-engineer-llm-%D8%"
        "AF%D9%88%D8%B1%DA%A9%D8%A7%D8%B1%DB%8C-%D8%AF%D8%B1-%D8%B3%D8%AA%D8%A7%D9%BE-%D8%AA%D8%B1%DA%A9?_ref"
        "=16&_t=36352e3130382e3232372e313635"
    ): "tNhZ",
    (
        "https://jobinja.ir/companies/world-express/jobs/tjrl/%D8%A7%D8%B3%D8%AA%D8%AE%D8%AF%D8%A7%D9%85-remo"
        "te-senior-frontend-engineer-angular-%D8%AF%D8%B1-%D9%88%D8%B1%D8%AF-%D8%A7%DA%A9%D8%B3%D9%BE%D8%B1%D"
        "8%B3"
    ): "tjrl",
}


def _search_html() -> str:
    """Load the captured search page (3 cards)."""
    return (_FIXTURES / "search_page.html").read_text(encoding="utf-8")


def _detail_html() -> str:
    """Load the captured detail page of a remote job."""
    return (_FIXTURES / "detail.html").read_text(encoding="utf-8")


# ── extract_external_id ────────────────────────────────────────────────────


def test_extract_external_id_on_real_urls() -> None:
    """The job code is extracted from each S4 URL, with or without the tracking query."""
    for url, expected in _S4_URLS.items():
        assert extract_external_id(url) == expected
        assert extract_external_id(_canonical_job_url(url)) == expected


def test_extract_external_id_relative_url() -> None:
    """A site-relative job link matches too."""
    assert extract_external_id("/companies/setuptrack/jobs/tNhZ/slug") == "tNhZ"


def test_extract_external_id_rejects_unrelated_urls() -> None:
    """Non-job URLs, including a company's job list and other sites, return None."""
    assert extract_external_id("https://jobinja.ir/companies/setuptrack/jobs") is None
    assert extract_external_id("https://jobinja.ir/jobs?filters%5Bkeywords%5D%5B0%5D=AI&page=2") is None
    assert extract_external_id("https://jobvision.ir/jobs/1558759") is None
    assert extract_external_id("https://example.com/companies/x/jobs/tNhZ/slug") is None
    assert extract_external_id("") is None


# ── parse_search_html ──────────────────────────────────────────────────────


def test_search_html_parses_all_cards() -> None:
    """Every card becomes a JobPost with non-empty fields and an ID taken from its URL."""
    jobs = parse_search_html(_search_html())
    assert [job.external_id for job in jobs] == [_PART_TIME_ID, _REMOTE_ID, _KARAJ_ID]
    for job in jobs:
        assert job.platform == "jobinja"
        assert job.external_id and job.title and job.company and job.city and job.url
        assert job.external_id == extract_external_id(job.url)
        assert job.url.startswith("https://jobinja.ir/companies/")
        assert "?" not in job.url
        assert job.salary is None
        assert job.raw_description is None


def test_search_html_field_mapping() -> None:
    """Title, company, and city come from the card's title link and meta items."""
    jobs = {job.external_id: job for job in parse_search_html(_search_html())}
    remote = jobs[_REMOTE_ID]
    assert remote.title == "مهندس هوش مصنوعی AI Engineer (LLM-دورکاری)"
    assert remote.company == "ستاپ ترک | SetupTrack"
    assert remote.city == "تهران، تهران"
    assert jobs[_KARAJ_ID].city == "البرز، کرج"
    assert jobs[_PART_TIME_ID].title == "مدرس هوش مصنوعی (AI)"


def test_search_html_work_mode_from_badge() -> None:
    """«قرارداد دورکاری» maps to REMOTE; full-time and part-time badges are ONSITE."""
    jobs = {job.external_id: job for job in parse_search_html(_search_html())}
    assert jobs[_REMOTE_ID].work_mode == WorkMode.REMOTE
    assert jobs[_REMOTE_ID].is_remote
    assert jobs[_PART_TIME_ID].work_mode == WorkMode.ONSITE
    assert jobs[_KARAJ_ID].work_mode == WorkMode.ONSITE


def test_search_html_card_without_badge_is_onsite() -> None:
    """A card whose contract badge is missing still parses, as ONSITE."""
    jobs = parse_search_html(_search_html().replace("c-icon--resume", "c-icon--removed"))
    assert len(jobs) == 3
    assert all(job.work_mode == WorkMode.ONSITE for job in jobs)


def test_search_html_skips_card_without_link() -> None:
    """A card whose title link is removed is skipped; the others still parse."""
    html = _search_html()
    start = html.index('<a class="c-jobListView__titleLink"')
    end = html.index("</a>", start) + len("</a>")
    jobs = parse_search_html(html[:start] + html[end:])
    assert [job.external_id for job in jobs] == [_REMOTE_ID, _KARAJ_ID]


def test_search_html_skips_card_with_non_job_link() -> None:
    """A card linking somewhere other than a job page is skipped."""
    html = _search_html().replace("https://jobinja.ir/companies/setuptrack/jobs/tNhZ/", "https://jobinja.ir/blog/")
    assert [job.external_id for job in parse_search_html(html)] == [_PART_TIME_ID, _KARAJ_ID]


def test_search_html_empty_page() -> None:
    """The captured no-results page has no results container and yields no jobs."""
    html = (_FIXTURES / "empty_search.html").read_text(encoding="utf-8")
    assert parse_search_html(html) == []


def test_search_html_garbage() -> None:
    """Unrelated HTML yields no jobs instead of raising."""
    assert parse_search_html("") == []
    assert parse_search_html("<html><body><p>hello</p></body></html>") == []


# ── parse_detail_html ──────────────────────────────────────────────────────


def test_detail_html_returns_plain_text() -> None:
    """The description is non-empty plain text with its line structure kept."""
    text = parse_detail_html(_detail_html())
    assert text is not None
    assert not _TAG_RE.search(text)
    lines = text.splitlines()
    assert lines[0] == "استخدام Backend Developer (Python)"
    assert "توسعه و بهبود هسته اصلی محصول با Python" in lines
    assert "مشارکت در توسعه قابلیت‌های مبتنی بر AI و LLM" in lines
    assert all(line == line.strip() and line for line in lines)


def test_detail_html_dom_fallback_matches_json_ld() -> None:
    """Without JSON-LD, the description element gives the same text, minus the site's own link."""
    with_json_ld = parse_detail_html(_detail_html())
    without_json_ld = parse_detail_html(_JSON_LD_RE.sub("", _detail_html()))
    assert without_json_ld == with_json_ld
    assert without_json_ld is not None
    assert "ثبت آگهی استخدام در جابینجا" not in without_json_ld


def test_detail_html_without_description() -> None:
    """A page with neither JSON-LD nor a description element returns None."""
    assert parse_detail_html("<html><body><h1>title</h1></body></html>") is None


# ── parse_detail_work_mode and salary ──────────────────────────────────────


def test_detail_work_mode_remote() -> None:
    """The «نوع همکاری» field of the fixture («تمام وقت» + «دورکاری») is REMOTE."""
    assert parse_detail_work_mode(_detail_html()) == WorkMode.REMOTE


def test_detail_work_mode_onsite_without_remote_tag() -> None:
    """With only «تمام وقت» left in the field, the work mode is ONSITE."""
    html = _detail_html().replace('<span class="black">دورکاری</span>', "")
    assert parse_detail_work_mode(html) == WorkMode.ONSITE


def test_detail_work_mode_ignores_description() -> None:
    """Without the work-type field the result is None, even though the description says «ریموت»."""
    html = _detail_html().replace("نوع همکاری", "فیلد دیگر")
    assert "ریموت" in (parse_detail_html(html) or "")
    assert parse_detail_work_mode(html) is None


def test_detail_salary() -> None:
    """The salary comes from the «حقوق» field."""
    assert _parse_detail_salary(_detail_html()) == "توافقی"
    assert _parse_detail_salary("<html><body></body></html>") is None


# ── Card + detail merge ────────────────────────────────────────────────────


def test_merge_work_modes() -> None:
    """Either source saying REMOTE wins; a missing detail field keeps the card's mode."""
    assert _merge_work_modes(WorkMode.ONSITE, WorkMode.REMOTE) == WorkMode.REMOTE
    assert _merge_work_modes(WorkMode.REMOTE, WorkMode.ONSITE) == WorkMode.REMOTE
    assert _merge_work_modes(WorkMode.ONSITE, WorkMode.ONSITE) == WorkMode.ONSITE
    assert _merge_work_modes(WorkMode.ONSITE, WorkMode.HYBRID) == WorkMode.HYBRID
    assert _merge_work_modes(WorkMode.ONSITE, None) == WorkMode.ONSITE
    assert _merge_work_modes(WorkMode.REMOTE, None) == WorkMode.REMOTE


def test_apply_detail_full_time_card_remote_detail() -> None:
    """Mismatch case: a card showing only «قرارداد تمام‌وقت» whose detail field lists «دورکاری» becomes REMOTE."""
    jobs = {job.external_id: job for job in parse_search_html(_search_html())}
    card_job = jobs[_KARAJ_ID]
    assert "قرارداد\n              تمام‌وقت" in _search_html()
    assert card_job.work_mode == WorkMode.ONSITE
    merged = _apply_detail(card_job, _detail_html())
    assert merged.work_mode == WorkMode.REMOTE
    assert merged.is_remote
    assert merged.raw_description and merged.salary == "توافقی"
    assert merged.external_id == card_job.external_id and merged.title == card_job.title


def test_apply_detail_remote_card_full_time_detail() -> None:
    """A remote card stays REMOTE when the detail field lists only «تمام وقت»."""
    jobs = {job.external_id: job for job in parse_search_html(_search_html())}
    html = _detail_html().replace('<span class="black">دورکاری</span>', "")
    assert _apply_detail(jobs[_REMOTE_ID], html).work_mode == WorkMode.REMOTE


def test_apply_detail_onsite_on_both() -> None:
    """A full-time card with a full-time-only detail field stays ONSITE (the tuk9 capture)."""
    jobs = {job.external_id: job for job in parse_search_html(_search_html())}
    html = _detail_html().replace('<span class="black">دورکاری</span>', "")
    assert _apply_detail(jobs[_KARAJ_ID], html).work_mode == WorkMode.ONSITE


def test_apply_detail_without_work_type_field() -> None:
    """Without the «نوع همکاری» field, the card's work mode is kept."""
    jobs = {job.external_id: job for job in parse_search_html(_search_html())}
    html = _detail_html().replace("نوع همکاری", "فیلد دیگر")
    assert _apply_detail(jobs[_KARAJ_ID], html).work_mode == WorkMode.ONSITE
    assert _apply_detail(jobs[_REMOTE_ID], html).work_mode == WorkMode.REMOTE
