"""Tests for the pure scraper helpers and the Cloudflare/BaseScraper lifecycle in `src.scrapers.base`."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from src.models import AppConfig, JobPost, WorkMode
from src.scrapers import base, constants
from src.scrapers.base import (
    BaseScraper,
    ScraperBlockedError,
    detect_work_mode,
    extract_json_ld_job_posting,
    save_debug_artifact,
    wait_for_cloudflare,
)

_JOB = {"@context": "https://schema.org", "@type": "JobPosting", "title": "مهندس هوش مصنوعی"}


def _html(*blocks: str) -> str:
    """Wrap raw JSON-LD block texts in an HTML document."""
    scripts = "".join(f'<script type="application/ld+json">{block}</script>' for block in blocks)
    return f"<html><head><title>t</title>{scripts}</head><body><p>body</p></body></html>"


# ── extract_json_ld_job_posting ────────────────────────────────────────────


def test_json_ld_single_object() -> None:
    """A lone JobPosting object is returned as-is."""
    assert extract_json_ld_job_posting(_html(json.dumps(_JOB, ensure_ascii=False))) == _JOB


def test_json_ld_top_level_list() -> None:
    """A JobPosting inside a top-level list is found."""
    other = {"@type": "Organization", "name": "x"}
    assert extract_json_ld_job_posting(_html(json.dumps([other, _JOB]))) == _JOB


def test_json_ld_graph() -> None:
    """A JobPosting inside `@graph` is found."""
    graph = {"@context": "https://schema.org", "@graph": [{"@type": "WebPage"}, _JOB]}
    assert extract_json_ld_job_posting(_html(json.dumps(graph))) == _JOB


def test_json_ld_type_list() -> None:
    """An `@type` list that contains JobPosting counts."""
    job = {"@type": ["Thing", "JobPosting"], "title": "x"}
    assert extract_json_ld_job_posting(_html(json.dumps(job))) == job


def test_json_ld_invalid_block_then_valid() -> None:
    """An invalid block is skipped and the following valid one is used."""
    assert extract_json_ld_job_posting(_html("{not json,", json.dumps(_JOB))) == _JOB


def test_json_ld_raw_newline_in_string() -> None:
    """Raw newlines inside JSON strings (common in descriptions) are tolerated."""
    block = '{"@type": "JobPosting", "description": "line one\nline two"}'
    assert extract_json_ld_job_posting(_html(block)) == {"@type": "JobPosting", "description": "line one\nline two"}


def test_json_ld_no_job_posting() -> None:
    """Without a JobPosting the result is None."""
    html = _html(json.dumps({"@type": "Organization"}), json.dumps([{"@type": "BreadcrumbList"}]))
    assert extract_json_ld_job_posting(html) is None


def test_json_ld_no_blocks() -> None:
    """A page without JSON-LD returns None."""
    assert extract_json_ld_job_posting("<html><body>nothing</body></html>") is None


# ── detect_work_mode ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "دورکاری",
        "دور کاری",
        "دور‌کاری",  # ZWNJ
        "امکان دورکاری",
        "دورکار\u064a",  # Arabic Yeh
    ],
)
def test_work_mode_persian_remote(text: str) -> None:
    """Persian remote cues are recognized, including the ZWNJ spelling."""
    assert detect_work_mode(text) == WorkMode.REMOTE


@pytest.mark.parametrize("text", ["هیبریدی", "ه\u064aبر\u064aدی", "ه\u064aبر\u064aد"])  # Arabic Yeh variants
def test_work_mode_persian_hybrid(text: str) -> None:
    """Persian hybrid cues are recognized, including Arabic-Yeh spellings."""
    assert detect_work_mode(text) == WorkMode.HYBRID


@pytest.mark.parametrize("text", ["Remote", "REMOTE job", "Work From Home"])
def test_work_mode_english_remote_mixed_case(text: str) -> None:
    """English remote cues match regardless of case."""
    assert detect_work_mode(text) == WorkMode.REMOTE


@pytest.mark.parametrize("text", ["Hybrid", "hYbRiD"])
def test_work_mode_english_hybrid_mixed_case(text: str) -> None:
    """English hybrid cues match regardless of case."""
    assert detect_work_mode(text) == WorkMode.HYBRID


@pytest.mark.parametrize("text", ["دورکاری / هیبریدی", "Remote or Hybrid", "هیبرید - remote"])
def test_work_mode_hybrid_takes_precedence(text: str) -> None:
    """When both kinds of cue appear, HYBRID wins."""
    assert detect_work_mode(text) == WorkMode.HYBRID


@pytest.mark.parametrize("text", ["تمام وقت", "مشهد", "Full-time", ""])
def test_work_mode_onsite_without_cue(text: str) -> None:
    """Text without a cue defaults to ONSITE."""
    assert detect_work_mode(text) == WorkMode.ONSITE


# ── save_debug_artifact ────────────────────────────────────────────────────


def test_save_debug_artifact_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Artifacts land under DEBUG_DIR/platform with a safe, bounded slug and never overwrite."""
    monkeypatch.setattr(constants, "DEBUG_DIR", tmp_path)
    name = "page_/jobs?q=a b:c" + "x" * 100
    first = save_debug_artifact("jobinja", name, "<html/>", "html")
    second = save_debug_artifact("jobinja", name, b"{}", ".json")

    assert first.parent == tmp_path / "jobinja"
    assert first.read_text(encoding="utf-8") == "<html/>"
    assert second.read_bytes() == b"{}"
    assert second.suffix == ".json"
    slug = first.stem.split("_", 2)[2]  # strip the "YYYYmmddTHHMMSS_ffffffZ_" prefix
    assert len(slug) <= 60
    assert all(char not in slug for char in "/?: ")


def test_save_debug_artifact_unsafe_name_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A name with no safe characters falls back to a generic slug."""
    monkeypatch.setattr(constants, "DEBUG_DIR", tmp_path)
    path = save_debug_artifact("jobvision", "/../", "x", "txt")
    assert path.parent == tmp_path / "jobvision"
    assert path.name.endswith("_artifact.txt")


# ── wait_for_cloudflare (fake page; no browser) ────────────────────────────


class _FakePage:
    """Duck-typed Page whose title reports a challenge for the first `challenged_checks` checks."""

    def __init__(self, challenged_checks: int) -> None:
        self.url = "https://example.test/jobs"
        self._remaining = challenged_checks
        self.clicks = 0

    async def title(self) -> str:
        if self._remaining > 0:
            self._remaining -= 1
            return "Just a moment..."
        return "Jobs"

    def locator(self, selector: str) -> "_FakeLocator":
        return _FakeLocator()

    def is_closed(self) -> bool:
        return False

    async def click(self, *args: Any, **kwargs: Any) -> None:
        self.clicks += 1


class _FakeLocator:
    """Locator stub that never finds a DOM marker."""

    async def count(self) -> int:
        return 0


@pytest.fixture
def fast_cloudflare(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the Cloudflare limits so tests run in well under a second."""
    monkeypatch.setattr(constants, "CLOUDFLARE_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(constants, "CLOUDFLARE_WAIT_HEADLESS_S", 0.05)
    monkeypatch.setattr(constants, "CLOUDFLARE_WAIT_HEADFUL_S", 0.5)


def test_cloudflare_no_challenge(fast_cloudflare: None) -> None:
    """A clean page returns immediately."""
    page = _FakePage(challenged_checks=0)
    asyncio.run(wait_for_cloudflare(page, "jobinja", headful=False))  # type: ignore[arg-type]


def test_cloudflare_challenge_clears(fast_cloudflare: None) -> None:
    """A challenge that clears within the limit passes without touching the page."""
    page = _FakePage(challenged_checks=2)
    asyncio.run(wait_for_cloudflare(page, "jobinja", headful=False))  # type: ignore[arg-type]
    assert page.clicks == 0


def test_cloudflare_headless_limit_raises(fast_cloudflare: None) -> None:
    """A challenge outlasting the headless limit raises ScraperBlockedError."""
    page = _FakePage(challenged_checks=10_000)
    with pytest.raises(ScraperBlockedError):
        asyncio.run(wait_for_cloudflare(page, "jobinja", headful=False))  # type: ignore[arg-type]
    assert page.clicks == 0


def test_cloudflare_headful_uses_longer_limit(fast_cloudflare: None) -> None:
    """Headful mode waits past the headless limit (time for a manual solve)."""
    page = _FakePage(challenged_checks=15)  # ~0.15 s: beyond headless 0.05 s, within headful 0.5 s
    asyncio.run(wait_for_cloudflare(page, "jobinja", headful=True))  # type: ignore[arg-type]


# ── BaseScraper lifecycle ──────────────────────────────────────────────────


class _FakeContext:
    """Records whether it was closed."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _DummyScraper(BaseScraper):
    """Minimal concrete scraper whose fetch fails."""

    platform = "jobinja"

    async def fetch_jobs(self, keywords: list[str]) -> list[JobPost]:
        raise RuntimeError("boom")


def _config() -> AppConfig:
    """Build a minimal valid config."""
    return AppConfig.model_validate(
        {
            "scheduler": {"interval_minutes": 30, "active_hours": "08:00-23:00", "timezone": "Asia/Tehran"},
            "scraping": {"enabled_platforms": ["jobinja"], "max_pages_per_keyword": 3},
            "search_profiles": [{"profile_name": "p", "keywords": ["AI"]}],
        }
    )


def test_base_scraper_saves_state_and_closes_after_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """`__aexit__` saves storage_state and then closes the context even when scraping raised."""
    context = _FakeContext()
    events: list[str] = []

    async def fake_create(browser: Any, platform: str) -> _FakeContext:
        events.append(f"create:{platform}")
        return context

    async def fake_save(ctx: Any, platform: str) -> None:
        assert not ctx.closed, "state must be saved before the context closes"
        events.append(f"save:{platform}")

    monkeypatch.setattr(base, "create_browser_context", fake_create)
    monkeypatch.setattr(base, "save_storage_state", fake_save)

    async def run() -> None:
        async def is_known(platform: str, external_id: str) -> bool:
            return False

        scraper = _DummyScraper(object(), _config(), is_known)  # type: ignore[arg-type]
        assert scraper._max_pages == 3
        async with scraper:
            await scraper.fetch_jobs(["AI"])

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(run())
    assert events == ["create:jobinja", "save:jobinja"]
    assert context.closed


def test_base_scraper_closes_even_if_save_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing state save is logged, and the context is still closed."""
    context = _FakeContext()

    async def fake_create(browser: Any, platform: str) -> _FakeContext:
        return context

    async def failing_save(ctx: Any, platform: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(base, "create_browser_context", fake_create)
    monkeypatch.setattr(base, "save_storage_state", failing_save)

    async def run() -> None:
        async def is_known(platform: str, external_id: str) -> bool:
            return False

        async with _DummyScraper(object(), _config(), is_known):  # type: ignore[arg-type]
            pass

    asyncio.run(run())
    assert context.closed
