"""Tests for inline-button callback data: encoding, the 64-byte limit, malformed data, and job/resume refs."""

from typing import Any

import pytest

from src.bot.callbacks import (
    MAX_CALLBACK_BYTES,
    decode_callback,
    encode_callback,
    job_ref,
    parse_job_ref,
    parse_resume_ref,
)


@pytest.mark.parametrize("action", ["t", "r", "a", "s", "y", "c", "x", "n", "d"])
def test_round_trip_for_every_action(action: Any) -> None:
    data = encode_callback(action, "jb:tER9")
    assert data == f"{action}:jb:tER9"
    assert decode_callback(data) == (action, "jb:tER9")


def test_exactly_64_bytes_is_allowed_and_65_is_not() -> None:
    ref = "9" * (MAX_CALLBACK_BYTES - 2)  # "a:" + 62 digits = 64 bytes
    data = encode_callback("a", ref)
    assert len(data.encode("utf-8")) == 64
    assert decode_callback(data) == ("a", ref)
    with pytest.raises(ValueError, match="65 bytes"):
        encode_callback("a", ref + "9")


def test_limit_counts_utf8_bytes_not_characters() -> None:
    ref = "ج" * 31  # 31 characters, 62 bytes
    assert len(encode_callback("t", ref).encode("utf-8")) == 64
    with pytest.raises(ValueError):
        encode_callback("t", ref + "ج")  # 34 characters but 66 bytes


@pytest.mark.parametrize(("action", "ref"), [("z", "1"), ("tt", "1"), ("", "1"), ("a", "")])
def test_encode_rejects_unknown_action_or_empty_ref(action: Any, ref: str) -> None:
    with pytest.raises(ValueError):
        encode_callback(action, ref)


@pytest.mark.parametrize(
    "data",
    ["", "t", "t:", ":12", "z:12", "tt:12", "a12", "T:12", "a:" + "1" * 63, None, 12],
)
def test_decode_returns_none_for_malformed_data(data: Any) -> None:
    assert decode_callback(data) is None


def test_decode_keeps_colons_in_the_ref() -> None:
    assert decode_callback("d:jv:1522572") == ("d", "jv:1522572")


@pytest.mark.parametrize(
    ("platform", "external_id", "ref"),
    [("jobvision", "1522572", "jv:1522572"), ("jobinja", "tER9", "jb:tER9")],
)
def test_job_ref_round_trip(platform: Any, external_id: str, ref: str) -> None:
    assert job_ref(platform, external_id) == ref
    assert parse_job_ref(ref) == (platform, external_id)


@pytest.mark.parametrize(("ref", "expected"), [("JV:42", ("jobvision", "42")), (" jb: tER9 ", ("jobinja", "tER9"))])
def test_parse_job_ref_is_lenient_about_case_and_spaces(ref: str, expected: tuple[str, str]) -> None:
    assert parse_job_ref(ref) == expected


@pytest.mark.parametrize("ref", ["", "jv", "jv:", "xx:1", "jobvision:1", ":1"])
def test_parse_job_ref_rejects_invalid_refs(ref: str) -> None:
    assert parse_job_ref(ref) is None


@pytest.mark.parametrize(("ref", "expected"), [("12", 12), ("1", 1), ("007", 7)])
def test_parse_resume_ref(ref: str, expected: int) -> None:
    assert parse_resume_ref(ref) == expected


@pytest.mark.parametrize("ref", ["0", "-1", "abc", "12a", "", "۱۲", "１２", "1.5"])
def test_parse_resume_ref_rejects_invalid_ids(ref: str) -> None:
    assert parse_resume_ref(ref) is None
