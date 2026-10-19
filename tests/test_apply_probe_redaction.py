"""Tests for the apply-flow probe: redaction as pure functions, and a sandbox capture against a fake site."""

import asyncio
import json
import stat
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import Route, async_playwright

from scripts import apply_probe
from scripts.apply_probe import (
    ApplyCapture,
    redact_body,
    redact_headers,
    redact_html,
    redact_json,
    redact_url,
    save_auth_state,
    scrub_text,
    summarize_multipart,
)

# ── Headers and URLs ───────────────────────────────────────────────────────


def test_secret_headers_are_dropped() -> None:
    headers = {
        "Cookie": "sid=SECRET",
        "set-cookie": "sid=SECRET; HttpOnly",
        "Authorization": "Bearer SECRET",
        "Proxy-Authorization": "Basic SECRET",
        "X-CSRF-TOKEN": "SECRET",
        "x-xsrf-token": "SECRET",
        "X-Auth-User": "SECRET",
        "x-session-id": "SECRET",
        "x-api-key": "SECRET",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
        "Location": "https://jobinja.ir/next?ticket=SECRET",
    }
    kept = redact_headers(headers)
    assert kept == {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
        "Location": "https://jobinja.ir/next?ticket=<redacted len=6>",
    }


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://jobinja.ir/jobs/12?q=AI&token=abc123&empty=",
            "https://jobinja.ir/jobs/12?q=<redacted len=2>&token=<redacted len=6>&empty=<redacted len=0>",
        ),
        ("https://user:pass@jobvision.ir:8443/api/v1/apply", "https://jobvision.ir:8443/api/v1/apply"),
        ("https://jobinja.ir/page#access_token=xyz", "https://jobinja.ir/page#<redacted len=16>"),
        ("data:image/png;base64,AAAA", "data:<redacted len=26>"),
        ("https://jobinja.ir/", "https://jobinja.ir/"),
    ],
)
def test_redact_url(url: str, expected: str) -> None:
    assert redact_url(url) == expected


# ── Bodies ─────────────────────────────────────────────────────────────────


def test_form_body_keeps_names_and_lengths() -> None:
    body = "name=%DA%A9%DB%8C%D8%A7%D9%86&mobile=09121234567&message=&_token=abc".encode()
    assert redact_body(body, "application/x-www-form-urlencoded; charset=UTF-8") == [
        {"name": "name", "value": "<redacted len=4>"},
        {"name": "mobile", "value": "<redacted len=11>"},
        {"name": "message", "value": "<redacted len=0>"},
        {"name": "_token", "value": "<redacted len=3>"},
    ]


def test_json_body_keeps_structure_and_keys() -> None:
    body = json.dumps(
        {"jobId": 1522572, "coverLetter": "سلام", "answers": [{"q": 1, "a": "yes"}], "remote": True, "note": None}
    ).encode()
    assert redact_body(body, "application/json") == {
        "jobId": "<redacted len=7>",
        "coverLetter": "<redacted len=4>",
        "answers": [{"q": "<redacted len=1>", "a": "<redacted len=3>"}],
        "remote": "<redacted len=4>",
        "note": None,
    }
    assert redact_json(["a", 12]) == ["<redacted len=1>", "<redacted len=2>"]


@pytest.mark.parametrize(
    ("body", "content_type", "expected"),
    [
        (None, "application/json", None),
        (b"", "text/plain", None),
        (b"{not json", "application/json", "<redacted body len=9 type=application/json>"),
        (b"hello there", "text/plain;charset=utf-8", "<redacted body len=11 type=text/plain>"),
        (b"\x00\x01", None, "<redacted body len=2 type=unknown>"),
    ],
)
def test_other_bodies_are_summarized(body: bytes | None, content_type: str | None, expected: Any) -> None:
    assert redact_body(body, content_type) == expected


def _multipart(boundary: str, parts: list[tuple[str, bytes]]) -> bytes:
    """Build a multipart/form-data body from (headers, content) parts."""
    chunks = [f"--{boundary}\r\n{headers}\r\n\r\n".encode() + content + b"\r\n" for headers, content in parts]
    return b"".join(chunks) + f"--{boundary}--\r\n".encode()


def test_multipart_files_are_summarized_and_fields_redacted() -> None:
    boundary = "----WebKitFormBoundaryAbC123"
    pdf = b"%PDF-1.7 " + b"x" * 991
    body = _multipart(
        boundary,
        [
            ('Content-Disposition: form-data; name="_token"', b"SECRET-TOKEN"),
            ('Content-Disposition: form-data; name="cover_letter"', "نامه من".encode()),
            (
                'Content-Disposition: form-data; name="resume"; filename="Kian_Resume_en.pdf"\r\n'
                "Content-Type: application/pdf",
                pdf,
            ),
            (
                'Content-Disposition: form-data; name="empty_file"; filename=""\r\n'
                "Content-Type: application/octet-stream",
                b"",
            ),
        ],
    )
    summary = summarize_multipart(body, f"multipart/form-data; boundary={boundary}")
    assert summary == [
        {"name": "_token", "value": "<redacted len=12>"},
        {"name": "cover_letter", "value": "<redacted len=7>"},
        {"name": "resume", "value": "<file Kian_Resume_en.pdf, 1000 bytes, application/pdf>"},
        {"name": "empty_file", "value": "<file , size not captured, application/octet-stream>"},
    ]
    assert redact_body(body, f'multipart/form-data; boundary="{boundary}"') == summary


def test_multipart_without_boundary() -> None:
    assert summarize_multipart(b"--x\r\n", "multipart/form-data") == [{"error": "multipart body without a boundary"}]


# ── HTML and text ──────────────────────────────────────────────────────────

_FORM_HTML = """<!DOCTYPE html>
<html><head>
<meta name="csrf-token" content="META-SECRET">
<meta name="description" content="Public job page">
<script>window.state = {"token": "SCRIPT-SECRET", "user": "Kian"};</script>
<script src="/static/app.js"></script>
</head><body>
<header>Kian Rostami · kian.rostami@example.com · +98 912 123 4567</header>
<form method="post" action="/apply?signature=SIGNED-SECRET">
  <a href="https://jobinja.ir/logout?token=LINK-SECRET#frag">exit</a>
  <img src="data:image/png;base64,DATA-SECRET">
  <input type="hidden" name="_token" value="HIDDEN-SECRET">
  <input name="mobile" value="09121234567">
  <input name="city" value="مشهد">
  <input type="checkbox" name="remote" checked>
  <textarea name="cover_letter">Dear team, PRIVATE-LETTER</textarea>
  <input type="submit" value="ارسال درخواست">
  <button type="submit">ثبت</button>
</form>
<p>Call ۰۹۱۲۱۲۳۴۵۶۷ or write to hr@company.ir</p>
</body></html>"""


def test_redact_html_removes_values_scripts_tokens_and_contacts() -> None:
    html = redact_html(_FORM_HTML)
    for secret in (
        "META-SECRET",
        "SCRIPT-SECRET",
        "HIDDEN-SECRET",
        "09121234567",
        "PRIVATE-LETTER",
        "kian.rostami@example.com",
        "+98 912 123 4567",
        "۰۹۱۲۱۲۳۴۵۶۷",
        "hr@company.ir",
        "مشهد",
        "SIGNED-SECRET",
        "LINK-SECRET",
        "DATA-SECRET",
    ):
        assert secret not in html
    assert '<script src="/static/app.js"></script>' in html
    assert 'action="/apply?signature=&lt;redacted len=13&gt;"' in html
    assert 'href="https://jobinja.ir/logout?token=&lt;redacted len=11&gt;#&lt;redacted len=4&gt;"' in html
    assert 'value="ارسال درخواست"' in html  # button labels stay
    assert 'name="mobile" value="&lt;redacted len=11&gt;"' in html
    assert "&lt;redacted len=25&gt;</textarea>" in html
    assert 'content="Public job page"' in html
    assert "checked" in html and "<button type=\"submit\">ثبت</button>" in html


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("mail a.b+c@example.co.uk now", "mail <redacted len=19> now"),
        ("تماس ۰۹۱۲ ۱۲۳ ۴۵۶۷", "تماس <redacted len=13>"),
        ("call 0912-123-4567", "call <redacted len=13>"),
        ("call 00989121234567.", "call <redacted len=14>."),
        ("job 1522572 and year 2026", "job 1522572 and year 2026"),
    ],
)
def test_scrub_text(text: str, expected: str) -> None:
    assert scrub_text(text) == expected


# ── Auth state and CLI ─────────────────────────────────────────────────────


def test_auth_state_is_private_and_reports_only_the_cookie_count(tmp_path: Path) -> None:
    path = tmp_path / "browser_state" / "jobinja_auth_state.json"
    state = {"cookies": [{"name": "sid", "value": "SECRET"}, {"name": "x", "value": "y"}], "origins": []}
    assert save_auth_state(state, path) == 2
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8")) == state
    assert not list(path.parent.glob("*.tmp"))
    assert apply_probe.auth_state_path("jobvision").name == "jobvision_auth_state.json"


@pytest.mark.parametrize(
    "url",
    ["https://jobinja.ir/companies/x/jobs/1", "https://www.jobinja.ir/jobs/1"],
)
def test_job_url_must_be_on_the_platform(url: str) -> None:
    assert apply_probe._parse_args(["--platform", "jobinja", "--job-url", url]).job_url == url


@pytest.mark.parametrize(
    "argv",
    [
        ["--platform", "jobinja", "--job-url", "https://evil.example.com/jobinja.ir"],
        ["--platform", "jobinja", "--job-url", "http://jobinja.ir/jobs/1"],
        ["--platform", "jobinja", "--job-url", "https://notjobinja.ir/jobs/1"],
        ["--platform", "jobvision", "--job-url", "https://jobinja.ir/jobs/1"],
        ["--platform", "jobinja", "--login", "--job-url", "https://jobinja.ir/jobs/1"],
        ["--platform", "linkedin", "--login"],
    ],
)
def test_invalid_arguments_are_rejected(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        apply_probe._parse_args(argv)


# ── Sandbox capture (headless Chromium, fake site) ─────────────────────────

_JOB_URL = "https://jobinja.ir/companies/acme/jobs/AB12/ai-engineer"
_APPLY_URL = "https://jobinja.ir/companies/acme/jobs/AB12/apply"
_SECRETS = (
    "COOKIE-SECRET",
    "SESSION-SECRET",
    "HIDDEN-SECRET",
    "SCRIPT-SECRET",
    "TYPED-LETTER",
    "kian.rostami@example.com",
    "09121234567",
    "QUERY-SECRET",
)
_JOB_PAGE = f"""<!DOCTYPE html><html><head><title>AI Engineer</title>
<script>window.__STATE__ = {{"csrf": "SCRIPT-SECRET"}};</script></head>
<body><header>Kian · kian.rostami@example.com</header>
<form method="post" action="{_APPLY_URL}?ref=QUERY-SECRET" enctype="multipart/form-data">
<input type="hidden" name="_token" value="HIDDEN-SECRET">
<input name="mobile" value="09121234567">
<textarea name="cover_letter"></textarea>
<input type="file" name="resume">
<button type="submit">ارسال درخواست</button>
</form></body></html>"""
_DONE_PAGE = (
    "<!DOCTYPE html><html><head><title>Done</title></head>"
    "<body><p>درخواست شما ارسال شد</p><p>Contact: kian.rostami@example.com</p></body></html>"
)


async def _fake_site(route: Route) -> None:
    """Serve the fake job board: the job page, and the apply POST answered with a confirmation page.

    (Chromium does not route the follow-up request of a fulfilled redirect, so
    the fake answers the POST directly, as many real sites do.)
    """
    request = route.request
    url = request.url.split("?", 1)[0]
    if request.method == "POST" and url == _APPLY_URL:
        await route.fulfill(
            status=200,
            content_type="text/html; charset=utf-8",
            headers={"Set-Cookie": "flash=SESSION-SECRET; Path=/"},
            body=_DONE_PAGE,
        )
    elif url == _JOB_URL:
        await route.fulfill(
            status=200,
            content_type="text/html; charset=utf-8",
            headers={"Set-Cookie": "sid=COOKIE-SECRET; Path=/; HttpOnly"},
            body=_JOB_PAGE,
        )
    else:
        await route.fulfill(status=404, body="")


async def _wait_for(capture: ApplyCapture, predicate: Any, timeout: float = 15.0) -> None:
    """Wait until a timeline event matches `predicate`."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not any(predicate(event) for event in capture.events):
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out; events: {[event['type'] for event in capture.events]}")
        await asyncio.sleep(0.05)


def test_sandbox_capture_of_a_manual_application(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apply_probe, "_SNAPSHOT_SETTLE_S", 0.0)
    out_dir = tmp_path / "capture"
    resume = tmp_path / "Kian_Resume_en.pdf"
    resume.write_bytes(b"%PDF-1.7 fake resume")

    async def scenario() -> ApplyCapture:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                await context.add_cookies([{"name": "auth", "value": "COOKIE-SECRET", "url": "https://jobinja.ir"}])
                await context.route("https://jobinja.ir/**", _fake_site)
                capture = ApplyCapture(platform="jobinja", out_dir=out_dir, start_url=_JOB_URL + "?utm=QUERY-SECRET")
                commands: asyncio.Queue[str] = asyncio.Queue()
                run = asyncio.create_task(capture.run(context, commands))

                await _wait_for(capture, lambda event: event["type"] == "snapshot")
                commands.put_nowait("s")
                await _wait_for(capture, lambda event: event.get("reason") == "manual")

                # The test plays the human applicant; the probe itself never clicks or types.
                page = context.pages[0]
                await page.fill("textarea[name=cover_letter]", "TYPED-LETTER for the team")
                await page.set_input_files("input[name=resume]", str(resume))
                await page.click("button[type=submit]")
                await _wait_for(capture, lambda event: event["type"] == "snapshot" and event["title"] == "Done")
                commands.put_nowait("q")
                await asyncio.wait_for(run, timeout=15)
                return capture
            finally:
                await browser.close()

    capture = asyncio.run(scenario())
    index = json.loads((out_dir / "index.json").read_text(encoding="utf-8"))
    assert index["platform"] == "jobinja"
    assert index["start_url"] == _JOB_URL + "?utm=<redacted len=12>"
    assert index["events"][-1]["type"] == "finished"

    post = next(event for event in index["events"] if event["type"] == "request" and event["method"] == "POST")
    assert post["url"] == _APPLY_URL + "?ref=<redacted len=12>"
    fields = {field["name"]: field["value"] for field in post["body"]}
    assert fields["_token"] == "<redacted len=13>"
    assert fields["mobile"] == "<redacted len=11>"
    assert fields["cover_letter"] == "<redacted len=25>"
    # Chromium's request events omit uploaded file contents.
    assert fields["resume"] == "<file Kian_Resume_en.pdf, size not captured, application/pdf>"
    response = next(event for event in index["events"] if event.get("request_seq") == post["seq"])
    assert response["status"] == 200
    assert response["headers"]["content-type"].startswith("text/html")
    assert all("set-cookie" not in {name.lower() for name in event.get("headers", {})} for event in index["events"])

    snapshots = [event for event in index["events"] if event["type"] == "snapshot"]
    assert [event["reason"] for event in snapshots].count("manual") == 1
    assert sum(event["reason"] == "navigation" for event in snapshots) >= 2
    for event in snapshots:
        assert (out_dir / event["html"]).is_file()
        assert (out_dir / event["screenshot"]).read_bytes().startswith(b"\x89PNG")

    saved = [path for path in out_dir.glob("*") if path.suffix in (".json", ".html")]
    saved_text = "\n".join(path.read_text(encoding="utf-8") for path in saved)
    assert "درخواست شما ارسال شد" in saved_text  # the confirmation page was captured
    for secret in _SECRETS:
        assert secret not in saved_text, secret
    assert not list(out_dir.glob("*.tmp"))
    assert capture.events[-1]["type"] == "finished"
