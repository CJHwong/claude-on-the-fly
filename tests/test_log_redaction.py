"""Tests for content redaction in the log.

These log files live in a directory shaped for a file syncer, so anything written
there should be assumed to leave the machine and to persist for the retention
window. Prompts and agent replies are the user's data, not diagnostics, so the
default has to be redaction and the tests have to hold that default down.

Redaction is tied to the log level rather than its own switch, because DEBUG
already carries content by routes no helper controls (`raw slack event` dumps the
whole event, slack_bolt logs full payloads). The level is therefore the honest
signal for "this file has everything in it".
"""

from __future__ import annotations

import logging

import pytest

from claude_on_the_fly import logs


@pytest.fixture
def at_level():
    """Set the root level (where configure() puts LOG_LEVEL) and restore it."""
    root = logging.getLogger()
    previous = root.level

    def _set(level: int) -> None:
        root.setLevel(level)

    yield _set
    root.setLevel(previous)


def test_content_is_redacted_at_info(at_level):
    at_level(logging.INFO)
    assert logs.log_content() is False


def test_content_is_redacted_at_warning(at_level):
    at_level(logging.WARNING)
    assert logs.log_content() is False


def test_content_is_kept_at_debug(at_level):
    at_level(logging.DEBUG)
    assert logs.log_content() is True


def test_redact_hides_the_text_but_keeps_the_shape(at_level):
    at_level(logging.INFO)
    secret = "transfer $40k to account 12345, my password is hunter2"
    out = logs.redact(secret)
    assert secret not in out
    assert "hunter2" not in out
    assert str(len(secret)) in out


def test_redact_marks_empty_distinctly(at_level):
    # An empty message is a real diagnostic case (a filtered mention, a
    # whitespace-only edit), so it must not look like a redacted one.
    at_level(logging.INFO)
    assert logs.redact("") == "<empty>"
    assert logs.redact(None) == "<empty>"


def test_redact_passes_a_preview_through_at_debug(at_level):
    at_level(logging.DEBUG)
    text = "a" * 500
    assert logs.redact(text) == "a" * logs.CONTENT_PREVIEW_CHARS


def test_redact_argv_keeps_short_tokens_verbatim(at_level):
    at_level(logging.INFO)
    argv = ["pr", "list", "--limit", "5", "--repo", "owner/name"]
    assert logs.redact_argv(argv) == argv


def test_redact_argv_clips_a_prose_payload(at_level):
    at_level(logging.INFO)
    body = "I reviewed this and think the approach is wrong because " * 20
    out = logs.redact_argv(["pr", "comment", "--body", body])
    assert out[:3] == ["pr", "comment", "--body"]
    assert body not in out[3]
    # The count of withheld characters survives, so truncation is visible.
    assert f"+{len(body) - 48}" in out[3]


def test_redact_argv_is_length_based_not_flag_based(at_level):
    """A new content-carrying flag must be covered without being enumerated."""
    at_level(logging.INFO)
    out = logs.redact_argv(["issue", "create", "--some-future-flag", "z" * 300])
    assert "z" * 300 not in out[3]


def test_redact_argv_is_full_at_debug(at_level):
    at_level(logging.DEBUG)
    argv = ["pr", "comment", "--body", "q" * 300]
    assert logs.redact_argv(argv) == argv


def test_no_env_var_controls_this_any_more():
    """COTF_LOG_CONTENT is gone; the level is the only control."""
    import inspect

    source = inspect.getsource(logs.log_content)
    assert "COTF_LOG_CONTENT" not in source
    assert "environ" not in source
