"""Shared pytest fixtures."""

from __future__ import annotations

# Redirect HOME before anything else runs. Module constants like
# `agent.DATA_DIR` bind `Path.home()` at import time, so the first import of the
# package freezes whatever home is current — after that, patching the
# environment redirects nothing. Any import can reach the package transitively,
# so every one of them is deliberately ordered below this block — hence the E402
# suppressions on the two that end up below the assignments. With the real home
# out of reach, no production path can resolve into the developer's live
# `~/.claude-on-the-fly/`, where a worker daemon owns the job maildir.
import atexit
import os
import shutil
import tempfile
from pathlib import Path

_ORIGINAL_HOME = Path.home()
_TEST_HOME = tempfile.mkdtemp(prefix="cotf-test-home-")
os.environ["HOME"] = _TEST_HOME
# Windows: Path.home() consults USERPROFILE, not HOME.
os.environ["USERPROFILE"] = _TEST_HOME
# CLAUDE_CONFIG_DIR wins over home wherever it is set, so redirecting home
# alone would leave those paths on the developer's real config directory. It is
# now always set, so the `Path.home() / ".claude"` fallback is never taken here:
# a test covering that branch has to delenv it first.
os.environ["CLAUDE_CONFIG_DIR"] = str(Path(_TEST_HOME) / ".claude")
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

import json  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def original_home() -> Path:
    """The developer's real home, captured before the redirect above.

    A fixture rather than an importable global: importing this module from a
    test would re-execute it under a second name and run `mkdtemp()` again,
    leaking a directory and capturing the already-redirected home.
    """
    return _ORIGINAL_HOME


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
def ndjson():
    """Encode a sequence of dicts as newline-delimited JSON bytes."""

    def _encode(*messages: dict) -> bytes:
        return b"\n".join(json.dumps(m).encode() for m in messages) + b"\n"

    return _encode
