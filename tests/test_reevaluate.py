"""Tests for `--reevaluate`: the pure `plan_reevaluation`, the CLI options, and the run itself.

Configs are built in memory. The browser and the Telegram notifier are
replaced by fakes, so nothing touches the network.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import main
from src.database import Database
from src.models import AppConfig, JobPost, JobStatus, Settings, WorkMode
from src.rule_engine import RuleEngine


def _config() -> AppConfig:
    return AppConfig.model_validate(
        {
            "scheduler": {"interval_minutes": 30, "active_hours": "08:00-23:00", "timezone": "Asia/Tehran"},
            "scraping": {"enabled_platforms": ["jobvision", "jobinja"], "max_pages_per_keyword": 1},
            "city_aliases": {"mashhad": ["mashhad", "مشهد"]},
            "search_profiles": [
                {
                    "profile_name": "AI",
                    "keywords": ["AI", "هوش مصنوعی"],
                    "exclude_keywords": ["Intern"],
                    "location_rules": [{"city": "mashhad", "remote_only": False}, {"city": "ALL", "remote_only": True}],
                },
                {"profile_name": "Python", "keywords": ["Python"]},
            ],
        }
    )


def _job(external_id: str, title: str, **overrides: Any) -> JobPost:
    data: dict[str, Any] = {
        "platform": "jobinja",
        "external_id": external_id,
        "title": title,
        "company": "شرکت نمونه",
        "city": "مشهد",
        "work_mode": WorkMode.ONSITE,
        "url": f"https://jobinja.ir/jobs/{external_id}",
        "status": JobStatus.IGNORED,
    }
    data.update(overrides)
    return JobPost(**data)


def _settings(db_path: Path) -> Settings:
    return Settings(
        _env_file=None, TELEGRAM_BOT_TOKEN="123:test-token", TELEGRAM_CHAT_ID="@test_channel", DB_PATH=str(db_path)
    )


# ── plan_reevaluation ────────────────────────────────────────────


def test_plan_reevaluation_returns_matching_ignored_jobs_in_order() -> None:
    config = _config()
    jobs = [
        _job("1", "Python Developer"),
        _job("2", "Accountant"),
        _job("3", "مهندس هوش مصنوعی و Python"),
        _job("4", "AI Intern"),
        _job("5", "AI Engineer", city="Tehran"),
        _job("6", "AI Engineer", city="Tehran", work_mode=WorkMode.REMOTE),
        _job("7", "AI Engineer", status=JobStatus.NOTIFIED),
        _job("8", "AI Engineer", status=JobStatus.NEW),
    ]

    plan = main.plan_reevaluation(jobs, RuleEngine(config))

    assert [(job.external_id, matched) for job, matched in plan] == [
        ("1", ["Python"]),
        ("3", ["AI", "Python"]),
        ("6", ["AI"]),
    ]
    assert plan[0][0] is jobs[0]


def test_plan_reevaluation_empty() -> None:
    assert main.plan_reevaluation([], RuleEngine(_config())) == []
    assert main.plan_reevaluation([_job("1", "Accountant")], RuleEngine(_config())) == []


def test_reevaluation_table_lists_platform_id_title_and_profiles() -> None:
    plan = [
        (_job("1001", "مهندس‌هوش مصنوعی"), ["AI"]),
        (_job("v-22", "Senior   Python Developer", platform="jobvision"), ["AI", "Python"]),
        (_job("3", "A" * 200), ["Python"]),
    ]
    lines = main._format_reevaluation_table(plan).splitlines()
    assert lines[0].split() == ["Platform", "ID", "Title", "Matched", "profiles"]
    assert "jobinja" in lines[2] and "1001" in lines[2] and "AI" in lines[2]
    assert "Senior Python Developer" in lines[3] and "AI, Python" in lines[3]
    assert "…" in lines[4] and "A" * 200 not in lines[4]
    # The ID column starts at the same position on every row.
    assert {line.index(cell) for line, cell in zip(lines[2:], ["1001", "v-22", "3 "], strict=True)} == {
        lines[0].index("ID")
    }


# ── CLI ──────────────────────────────────────────────────────────


def test_parse_args_reevaluate_defaults() -> None:
    args = main._parse_args(["--reevaluate"])
    assert args.reevaluate is True
    assert args.since_days == 30
    assert args.dry_run is False


def test_parse_args_reevaluate_options() -> None:
    args = main._parse_args(["--reevaluate", "--since-days", "7", "--dry-run"])
    assert (args.since_days, args.dry_run) == (7, True)


@pytest.mark.parametrize(
    "argv",
    [
        ["--once", "--dry-run"],
        ["--daemon", "--since-days", "5"],
        ["--once", "--reevaluate"],
        ["--reevaluate", "--since-days", "0"],
        ["--reevaluate", "--since-days", "abc"],
        [],
    ],
)
def test_parse_args_rejects_invalid_combinations(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        main._parse_args(argv)


def test_parse_args_once_unchanged() -> None:
    args = main._parse_args(["--once", "--headful"])
    assert (args.once, args.daemon, args.reevaluate, args.headful) == (True, False, False, True)


# ── The run ──────────────────────────────────────────────────────


class _FakeNotifier:
    """Stands in for `TelegramNotifier`; records sent jobs instead of calling Telegram."""

    sent: list[str] = []
    fail_ids: set[str] = set()

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def __aenter__(self) -> "_FakeNotifier":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def send_job(self, job: JobPost, **kwargs: Any) -> bool:
        if job.external_id in self.fail_ids:
            return False
        self.sent.append(job.external_id)
        return True


class _ExplodingNotifier:
    """Fails the test if the notifier is created at all."""

    def __init__(self, settings: Settings) -> None:
        raise AssertionError("the notifier must not be created in a dry run")


def _no_browser() -> None:
    raise AssertionError("--reevaluate must never launch a browser")


def _seed(db_path: Path) -> None:
    now = datetime.now(UTC)

    async def scenario() -> None:
        async with Database(db_path) as db:
            await db.init_db()
            await db.save_job(_job("match", "Python Developer", created_at=now - timedelta(days=2)))
            await db.save_job(_job("nomatch", "Accountant", created_at=now - timedelta(days=2)))
            await db.save_job(_job("old", "Python Developer", created_at=now - timedelta(days=60)))
            await db.save_job(_job("fails", "AI Engineer", created_at=now - timedelta(days=1)))
            await db.save_job(
                _job("sent", "Python Developer", status=JobStatus.NOTIFIED, matched_profiles=["Python"])
            )

    asyncio.run(scenario())


def _statuses(db_path: Path) -> dict[str, tuple[JobStatus, list[str]]]:
    async def scenario() -> dict[str, tuple[JobStatus, list[str]]]:
        async with Database(db_path) as db:
            result = {}
            for external_id in ("match", "nomatch", "old", "fails", "sent"):
                job = await db.get_job("jobinja", external_id)
                assert job is not None
                result[external_id] = (job.status, job.matched_profiles)
            return result

    return asyncio.run(scenario())


def test_dry_run_changes_nothing_and_prints_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "jobs.db"
    _seed(db_path)
    before = _statuses(db_path)
    monkeypatch.setattr(main, "async_playwright", _no_browser)
    monkeypatch.setattr(main, "TelegramNotifier", _ExplodingNotifier)

    summary = asyncio.run(main._reevaluate(_settings(db_path), _config(), since_days=30, dry_run=True))

    assert (summary.checked, summary.candidates, summary.requeued, summary.notified) == (3, 2, 0, 0)
    assert _statuses(db_path) == before
    out = capsys.readouterr().out
    assert "2 IGNORED job(s) would be requeued" in out
    assert "match" in out and "fails" in out
    assert "nomatch" not in out and "old" not in out


def test_dry_run_with_no_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(main, "async_playwright", _no_browser)
    monkeypatch.setattr(main, "TelegramNotifier", _ExplodingNotifier)
    exit_code = asyncio.run(
        main._run_reevaluate(_settings(tmp_path / "empty.db"), _config(), since_days=30, dry_run=True)
    )
    assert exit_code == 0
    assert "No IGNORED job" in capsys.readouterr().out


def test_reevaluate_requeues_then_notifies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "jobs.db"
    _seed(db_path)
    monkeypatch.setattr(main, "async_playwright", _no_browser)
    monkeypatch.setattr(_FakeNotifier, "sent", [])
    monkeypatch.setattr(_FakeNotifier, "fail_ids", {"fails"})
    monkeypatch.setattr(main, "TelegramNotifier", _FakeNotifier)

    exit_code = asyncio.run(main._run_reevaluate(_settings(db_path), _config(), since_days=30, dry_run=False))

    assert exit_code == 2  # one notification failed; that job stays NEW for the next run
    assert _FakeNotifier.sent == ["match"]
    statuses = _statuses(db_path)
    assert statuses["match"] == (JobStatus.NOTIFIED, ["Python"])
    assert statuses["fails"] == (JobStatus.NEW, ["AI"])
    assert statuses["nomatch"] == (JobStatus.IGNORED, [])
    assert statuses["old"] == (JobStatus.IGNORED, [])
    assert statuses["sent"] == (JobStatus.NOTIFIED, ["Python"])


def test_reevaluate_summary_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "jobs.db"
    _seed(db_path)
    monkeypatch.setattr(main, "async_playwright", _no_browser)
    monkeypatch.setattr(_FakeNotifier, "sent", [])
    monkeypatch.setattr(_FakeNotifier, "fail_ids", set())
    monkeypatch.setattr(main, "TelegramNotifier", _FakeNotifier)

    summary = asyncio.run(main._reevaluate(_settings(db_path), _config(), since_days=90, dry_run=False))

    assert (summary.checked, summary.candidates, summary.requeued, summary.notified, summary.failed) == (
        4,
        3,
        3,
        3,
        0,
    )
    assert sorted(_FakeNotifier.sent) == ["fails", "match", "old"]
    assert not summary.has_problems
