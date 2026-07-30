"""Tests for content redaction in the log.

These log files live in a directory shaped for a file syncer, so anything written
there should be assumed to leave the machine and to persist for the retention
window. Prompts and agent replies are the user's data, not diagnostics, so the
default has to be redaction and the tests have to hold that default down.
"""

from __future__ import annotations

import pytest

from claude_on_the_fly import logs


@pytest.fixture(autouse=True)
def _content_off(monkeypatch):
    monkeypatch.delenv("COTF_LOG_CONTENT", raising=False)


def test_content_logging_is_off_by_default():
    assert logs.log_content() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_content_logging_opt_in_values(monkeypatch, value):
    monkeypatch.setenv("COTF_LOG_CONTENT", value)
    assert logs.log_content() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "maybe"])
def test_content_logging_stays_off_for_anything_else(monkeypatch, value):
    monkeypatch.setenv("COTF_LOG_CONTENT", value)
    assert logs.log_content() is False


def test_redact_hides_the_text_but_keeps_the_shape():
    secret = "transfer $40k to account 12345, my password is hunter2"
    out = logs.redact(secret)
    assert secret not in out
    assert "hunter2" not in out
    assert str(len(secret)) in out


def test_redact_marks_empty_distinctly():
    # An empty message is a real diagnostic case (a filtered mention, a
    # whitespace-only edit), so it must not look like a redacted one.
    assert logs.redact("") == "<empty>"
    assert logs.redact(None) == "<empty>"


def test_redact_passes_a_preview_through_when_enabled(monkeypatch):
    monkeypatch.setenv("COTF_LOG_CONTENT", "1")
    text = "a" * 500
    out = logs.redact(text)
    assert out == "a" * logs.CONTENT_PREVIEW_CHARS


def test_redact_argv_keeps_short_tokens_verbatim():
    argv = ["pr", "list", "--limit", "5", "--repo", "owner/name"]
    assert logs.redact_argv(argv) == argv


def test_redact_argv_clips_a_prose_payload():
    body = "I reviewed this and think the approach is wrong because " * 20
    out = logs.redact_argv(["pr", "comment", "--body", body])
    assert out[:3] == ["pr", "comment", "--body"]
    assert body not in out[3]
    # The count of withheld characters survives, so truncation is visible.
    assert f"+{len(body) - 48}" in out[3]


def test_redact_argv_is_length_based_not_flag_based():
    """A new content-carrying flag must be covered without being enumerated."""
    out = logs.redact_argv(["issue", "create", "--some-future-flag", "z" * 300])
    assert "z" * 300 not in out[3]


def test_redact_argv_full_when_content_enabled(monkeypatch):
    monkeypatch.setenv("COTF_LOG_CONTENT", "1")
    argv = ["pr", "comment", "--body", "q" * 300]
    assert logs.redact_argv(argv) == argv
