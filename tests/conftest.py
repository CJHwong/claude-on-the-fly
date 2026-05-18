"""Shared pytest fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture
def clear_backend_env(monkeypatch):
    """Strip backend-selection env vars so tests start from a clean slate."""
    for var in ("AGENT_BACKEND", "CLAUDE_MODE", "OLLAMA_MODEL"):
        monkeypatch.delenv(var, raising=False)
