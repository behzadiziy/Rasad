"""Tests for the JobVision parsers, run against trimmed copies of real captures."""

import copy
import json
import re
from pathlib import Path
from typing import Any

from src.models import WorkMode
from src.scrapers.jobvision import (
    _API_ROUTE_RE,
    _parse_search_dom,
    _requested_page,
    parse_detail_html,
    parse_search_payload,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "jobvision"
_TAG_RE = re.compile(r"<[^>]+>")

_ONSITE_ID = "1411778"
_REMOTE_ID = "1535897"
_ANONYMOUS_ID = "1566821"


def _record() -> dict[str, Any]:
    """Load the captured API record (request metadata plus response body)."""
    return json.loads((_FIXTURES / "search_response.json").read_text(encoding="utf-8"))


def _detail_html() -> str:
    """Load the captured detail page of a remote job."""
    return (_FIXTURES / "detail.html").read_text(encoding="utf-8")


# ── parse_search_payload ───────────────────────────────────────────────────


def test_search_payload_parses_all_items() -> None:
    """Every item in the fixture becomes a JobPost with the required fields."""
    jobs = parse_search_payload(_record()["body"])
    assert [job.external_id for job in jobs] == [_ONSITE_ID, _REMOTE_ID, _ANONYMOUS_ID]
    for job in jobs:
        assert job.platform == "jobvision"
        assert job.external_id and job.title and job.url
        assert job.url == f"https://jobvision.ir/jobs/{job.external_id}"
        assert job.raw_description is None


def test_search_payload_field_mapping() -> None:
    """Title, company, city, and salary come from the documented fields."""
    jobs = {job.external_id: job for job in parse_search_payload(_record()["body"])}
    onsite = jobs[_ONSITE_ID]
    assert onsite.title == "AI Artist"
    assert onsite.company == "گروه ایران نوین"
    assert onsite.city == "تهران، امانیه"
    assert onsite.salary is None
    assert jobs[_REMOTE_ID].salary == "50 - 100 میلیون تومان"
    assert jobs[_ANONYMOUS_ID].company == "سازمانی فعال در حوزه تولیدی / صنعتی"


def test_search_payload_work_mode_from_remote_flag() -> None:
    """The `isRemote` flag maps to REMOTE; the other items are ONSITE."""
    jobs = {job.external_id: job for job in parse_search_payload(_record()["body"])}
    assert jobs[_REMOTE_ID].work_mode == WorkMode.REMOTE
    assert jobs[_REMOTE_ID].is_remote
    assert jobs[_ONSITE_ID].work_mode == WorkMode.ONSITE
    assert jobs[_ANONYMOUS_ID].work_mode == WorkMode.ONSITE


def test_search_payload_labels_used_without_flag() -> None:
    """Without the remote flag, the short badge labels decide the work mode."""
    body = _record()["body"]
    item = copy.deepcopy(body["data"]["jobPosts"][1])
    del item["properties"]["isRemote"]
    body["data"]["jobPosts"] = [item]
    assert item["labels"] == ["امکان دورکاری"]
    assert parse_search_payload(body)[0].work_mode == WorkMode.REMOTE


def test_search_payload_skips_malformed_items() -> None:
    """Malformed items are skipped and the valid ones still parse."""
    body = _record()["body"]
    valid = body["data"]["jobPosts"]
    no_title = copy.deepcopy(valid[0]) | {"title": "  "}
    no_id = {key: value for key, value in valid[0].items() if key != "id"}
    bool_id = copy.deepcopy(valid[0]) | {"id": True}
    broken_nested = copy.deepcopy(valid[0]) | {"company": "not an object", "location": [1, 2]}
    body["data"]["jobPosts"] = [None, "text", 42, no_title, no_id, bool_id, *valid, broken_nested]
    jobs = parse_search_payload(body)
    assert [job.external_id for job in jobs] == [_ONSITE_ID, _REMOTE_ID, _ANONYMOUS_ID, _ONSITE_ID]
    assert jobs[-1].company == "" and jobs[-1].city == ""


def test_search_payload_without_job_list() -> None:
    """Payloads without a job list yield no jobs instead of raising."""
    assert parse_search_payload(None) == []
    assert parse_search_payload([]) == []
    assert parse_search_payload({"isSuccess": False, "data": None}) == []
    assert parse_search_payload({"data": {"jobPosts": {}}}) == []
    assert parse_search_payload({"data": {"jobPosts": []}}) == []


def test_api_route_pattern_matches_capture() -> None:
    """The route pattern matches the captured job-list URL and nothing else on the API host."""
    assert _API_ROUTE_RE.search(_record()["request_url"])
    assert not _API_ROUTE_RE.search(
        "https://candidateapi.jobvision.ir/api/v1/JobPost/Recommendation/Similar?jobPostId=1558759"
    )
    assert not _API_ROUTE_RE.search("https://candidateapi.jobvision.ir/api/v1/RelatedSearch/GetRelatedSearches")
    assert not _API_ROUTE_RE.search("https://candidateapi.jobvision.ir/api/v1/JobPost/ListOther")


def test_requested_page_from_capture() -> None:
    """The page number is read from the captured POST body; bad bodies give None."""
    assert _requested_page(_record()["request_post_data"]) == 1
    assert _requested_page('{"requestedPage": 2}') == 2
    assert _requested_page(None) is None
    assert _requested_page("not json") is None
    assert _requested_page('{"requestedPage": "2"}') is None


# ── parse_detail_html ──────────────────────────────────────────────────────


def test_detail_prefers_dom() -> None:
    """The description element is used first, as plain text, one text run per line."""
    text = parse_detail_html(_detail_html())
    assert text is not None
    lines = text.splitlines()
    assert lines[0].startswith("For managing and replying to messages")
    assert "Responsibilities:" in lines
    assert "Job Requirements" not in text
    assert not _TAG_RE.search(text)
    assert all(line.strip() == line and line for line in lines)


def test_detail_keeps_line_breaks() -> None:
    """Lines that JobVision's JSON-LD fuses together stay separate lines."""
    html = _detail_html()
    assert "defined toneRequirements:" in html  # the JSON-LD fusion this priority avoids
    text = parse_detail_html(html)
    assert text is not None
    lines = text.splitlines()
    assert len(lines) > 10
    assert "toneRequirements" not in text
    assert "Requirements:" in lines
    assert lines[lines.index("Requirements:") - 1].endswith("defined tone")


def test_detail_falls_back_to_json_ld() -> None:
    """Without the description element, the JSON-LD description is used as plain text."""
    html = _detail_html().replace("job-specification", "job-other")
    text = parse_detail_html(html)
    assert text is not None
    assert text.startswith("For managing and replying to messages")
    assert "toneRequirements:" in text
    assert not _TAG_RE.search(text)


def test_detail_empty_dom_and_json_ld_description() -> None:
    """Without the element and with an empty JSON-LD description, the result is None."""
    html = _detail_html().replace("job-specification", "job-other")
    html = html.replace('"description":"For managing', '"unused":"For managing', 1)
    assert parse_detail_html(html) is None


def test_detail_without_description() -> None:
    """A page with neither JSON-LD nor the description element yields None."""
    assert parse_detail_html("<html><body><p>nothing here</p></body></html>") is None
    assert parse_detail_html("") is None


# ── DOM fallback ───────────────────────────────────────────────────────────


def test_search_dom_matches_api() -> None:
    """The rendered cards yield the same jobs as the API items they were rendered from."""
    dom_jobs = {job.external_id: job for job in _parse_search_dom(
        (_FIXTURES / "search_page.html").read_text(encoding="utf-8")
    )}
    api_jobs = {job.external_id: job for job in parse_search_payload(_record()["body"])}
    assert list(dom_jobs) == [_ONSITE_ID, _REMOTE_ID, _ANONYMOUS_ID]
    for external_id, dom_job in dom_jobs.items():
        api_job = api_jobs[external_id]
        assert dom_job.platform == "jobvision"
        assert dom_job.title == api_job.title
        assert dom_job.company == api_job.company
        assert dom_job.work_mode == api_job.work_mode
        assert dom_job.city == api_job.city
        assert dom_job.salary == api_job.salary
        assert dom_job.url == api_job.url


def test_search_dom_skips_malformed_cards() -> None:
    """Cards without a link or title are skipped; the rest still parse."""
    html = (_FIXTURES / "search_page.html").read_text(encoding="utf-8")
    broken = "<job-card><div>no link</div></job-card><job-card><a id=\"el-9\" href=\"/jobs/9/x\"></a></job-card>"
    html = html.replace("<job-card-list>", "<job-card-list>" + broken, 1)
    assert [job.external_id for job in _parse_search_dom(html)] == [_ONSITE_ID, _REMOTE_ID, _ANONYMOUS_ID]


def test_search_dom_empty_page() -> None:
    """A page without cards yields no jobs."""
    assert _parse_search_dom("<html><body></body></html>") == []
