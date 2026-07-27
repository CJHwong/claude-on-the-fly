"""The suite's outermost blast radius: HOME itself.

Individual fixtures redirect individual constants, and a fixture can be
weakened without anything noticing — dropping `autouse=True` from
`isolate_jobs_dir` once put 29 files into the developer's live job maildir,
where a running worker claimed one and executed it as a real agent session.
conftest's module-level redirect is the backstop that contains that class of
mistake: with home pointed at a temporary directory before the package is ever
imported, no production path resolves to the real one. Note what it does and
does not buy — the harm becomes unreachable, the leak becomes silent. The same
weakening now spills into a temporary directory that nobody inspects.

This module is the guard on the backstop.
"""

from __future__ import annotations

from pathlib import Path

from claude_on_the_fly import agent


def test_home_is_redirected_away_from_the_real_one(original_home: Path) -> None:
    """The redirect happened at all."""
    assert Path.home().resolve() != original_home.resolve()


def test_derived_constants_followed_the_redirect() -> None:
    """The redirect landed early enough to matter.

    Anchored on `agent.DATA_DIR`, never on `state.DEFAULT_JOBS_DIR`: the autouse
    `isolate_jobs_dir` fixture repoints that constant before any test body runs,
    so a check reading it would pass with no home redirect at all. `DATA_DIR` is
    bound once at import time and nothing patches it, so it can only be under
    the temporary home if the redirect preceded the import.

    Not `pwd.getpwuid(os.getuid())` either — under docker or tox the passwd
    entry already differs from HOME, making that comparison vacuous too.
    """
    assert agent.DATA_DIR.resolve().is_relative_to(Path.home().resolve())
