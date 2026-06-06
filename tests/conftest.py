"""Shared pytest fixtures."""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def clear_backend_env(monkeypatch):
    """Strip backend-selection env vars so tests start from a clean slate."""
    for var in ("AGENT_BACKEND", "CLAUDE_MODE", "OLLAMA_MODEL", "CODEX_MODE"):
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
