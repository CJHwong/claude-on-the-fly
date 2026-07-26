"""Shared pytest fixtures."""

from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def isolate_jobs_dir(tmp_path, monkeypatch):
    """Keep the whole suite off the real background-job maildir.

    `snapshot()` reads `state.DEFAULT_JOBS_DIR` with no argument (the 1Hz
    dashboard refresh calls it that way), so any test that boots the TUI reads
    it — and on a dev machine a real worker owns that directory. The reads are
    read-only, but a test suite whose behavior depends on the developer's live
    queue is not hermetic. Autouse so it cannot be forgotten; returns the root,
    so a test wanting a populated queue just fills it in.
    """
    root = tmp_path / "isolated-jobs"
    monkeypatch.setattr("claude_on_the_fly.tui.state.DEFAULT_JOBS_DIR", root)
    return root


@pytest.fixture(autouse=True)
def isolate_env_file(tmp_path, monkeypatch):
    """Keep the whole suite off the developer's real `.env`.

    `state._queue_kind()` reads it through `supervisor.DEFAULT_ENV_FILE` on
    every `snapshot()`, so without this a `JOBS_QUEUE_KIND` on the dev machine
    would decide what the TUI tests see. Returns the (initially absent) path, so
    a test wanting a specific setting just writes it.
    """
    env_file = tmp_path / ".env"
    monkeypatch.setattr("claude_on_the_fly.tui.supervisor.DEFAULT_ENV_FILE", env_file)
    return env_file


@pytest.fixture
def clear_backend_env(monkeypatch):
    """Strip backend-selection env vars so tests start from a clean slate."""
    for var in (
        "AGENT_BACKEND",
        "CLAUDE_MODE",
        "OLLAMA_MODEL",
        "CODEX_MODE",
        "PI_MODE",
        "OPENCODE_MODE",
        "OPENCODE_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def claude_projects_dir(tmp_path, monkeypatch):
    """Redirect transcript module's CLAUDE_PROJECTS_DIR to a tmp_path subdir."""
    root = tmp_path / "claude-projects"
    root.mkdir()
    monkeypatch.setattr("claude_on_the_fly.transcript.CLAUDE_PROJECTS_DIR", root)
    return root


@pytest.fixture
def codex_sessions_dir(tmp_path, monkeypatch):
    """Redirect transcript module's CODEX_SESSIONS_DIR to a tmp_path subdir."""
    root = tmp_path / "codex-sessions"
    root.mkdir()
    monkeypatch.setattr("claude_on_the_fly.transcript.CODEX_SESSIONS_DIR", root)
    return root


@pytest.fixture
def pi_sessions_dir(tmp_path, monkeypatch):
    """Redirect transcript module's PI_SESSIONS_DIR to a tmp_path subdir.

    Also patches the backend module's PI_SESSIONS_DIR for takeover_command
    and session_log_path tests.
    """
    root = tmp_path / "pi-sessions"
    root.mkdir()
    monkeypatch.setattr("claude_on_the_fly.transcript.PI_SESSIONS_DIR", root)
    monkeypatch.setattr("claude_on_the_fly.backends.pi.PI_SESSIONS_DIR", root)
    return root


@pytest.fixture
def ndjson():
    """Encode a sequence of dicts as newline-delimited JSON bytes."""

    def _encode(*messages: dict) -> bytes:
        return b"\n".join(json.dumps(m).encode() for m in messages) + b"\n"

    return _encode
