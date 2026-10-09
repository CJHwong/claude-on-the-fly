"""Tests for sweep_workspace_clones.py against real git repositories."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "sweep_workspace_clones", Path(__file__).with_name("sweep_workspace_clones.py")
)
sweep = importlib.util.module_from_spec(SPEC)
sys.modules["sweep_workspace_clones"] = sweep
SPEC.loader.exec_module(sweep)

OLD = 30 * 86400


def run(*args: str, cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def age(repo: Path, seconds: float = OLD) -> None:
    stamp = (Path(repo).stat().st_mtime) - seconds
    for path in (repo, repo / ".git" / "HEAD", repo / ".git" / "logs" / "HEAD"):
        if path.exists():
            os.utime(path, (stamp, stamp))


@pytest.fixture
def world(tmp_path: Path, monkeypatch) -> dict:
    for key, value in {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }.items():
        monkeypatch.setenv(key, value)
    remote = tmp_path / "remote.git"
    run("git", "init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    seed = tmp_path / "seed"
    run("git", "clone", "-q", str(remote), str(seed), cwd=tmp_path)
    (seed / "a.txt").write_text("a")
    run("git", "add", ".", cwd=seed)
    run("git", "commit", "-q", "-m", "a", cwd=seed)
    run("git", "push", "-q", "origin", "HEAD:main", cwd=seed)
    root = tmp_path / "workspaces"
    return {"root": root, "remote": remote}


def clone(world: dict, rel: str) -> Path:
    path = world["root"] / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    run("git", "clone", "-q", str(world["remote"]), str(path), cwd=path.parent)
    return path


def test_dry_run_reports_and_apply_removes_only_finished_clones(
    world, monkeypatch, capsys
):
    monkeypatch.setattr(sweep, "trash_command", lambda: None)
    done = clone(world, "slack/dm/U1/repo")
    dirty = clone(world, "slack/channel/C1/repo")
    (dirty / "b.txt").write_text("b")
    local = clone(world, "cron/standup/repo")
    run("git", "checkout", "-q", "-b", "wip", cwd=local)
    (local / "c.txt").write_text("c")
    run("git", "add", ".", cwd=local)
    run("git", "commit", "-q", "-m", "c", cwd=local)
    fresh = clone(world, "slack/dm/U2/repo")
    for repo in (done, dirty, local):
        age(repo)
    (world["root"] / "slack/dm/U1/outbox/.git").mkdir(parents=True)  # never walked
    (world["root"] / "a/b/c/d/too-deep/.git").mkdir(parents=True)  # past MAX_DEPTH

    assert sweep.main(["--root", str(world["root"])]) == 0
    out = capsys.readouterr().out
    assert "would remove  slack/dm/U1/repo  (clean, pushed, 30d old)" in out
    assert "keep          slack/channel/C1/repo  (uncommitted changes)" in out
    assert "keep          cron/standup/repo  (unpushed: wip)" in out
    assert "keep          slack/dm/U2/repo  (touched 0.0d ago)" in out
    assert out.rstrip().endswith("would remove 1, kept 3")
    assert done.exists()

    assert sweep.main(["--root", str(world["root"]), "--apply"]) == 0
    assert not done.exists() and dirty.exists() and local.exists() and fresh.exists()
    assert "removed 1, kept 3" in capsys.readouterr().out


def test_a_lender_clone_is_kept(world):
    lender = clone(world, "slack/dm/U1/lender")
    borrower = clone(world, "slack/dm/U2/borrower")
    alternates = borrower / ".git/objects/info/alternates"
    alternates.write_text(f"\n{lender / '.git/objects'}\n")
    relative = clone(world, "slack/dm/U3/relative")
    (relative / ".git/objects/info/alternates").write_text(
        "../../../../U1/lender/.git/objects\n"
    )
    age(lender)
    borrowed = sweep.borrowed_stores(sweep.find_clones(world["root"]))
    assert sweep.verdict(lender, 7, borrowed) == (
        "keep",
        "object store used by another clone",
    )


def test_git_error_keeps_the_clone(tmp_path):
    broken = tmp_path / "broken"
    (broken / ".git").mkdir(parents=True)
    age(broken)
    action, reason = sweep.verdict(broken, 7, set())
    assert action == "keep" and reason.startswith("git error:")


def test_missing_root_is_an_error(tmp_path, capsys):
    assert sweep.main(["--root", str(tmp_path / "nope")]) == 1
    assert "no workspace root" in capsys.readouterr().err


@pytest.mark.parametrize(
    "found,expected",
    [
        ({"trash": "/bin/trash"}, ["/bin/trash"]),
        ({"trash-put": "/bin/trash-put"}, ["/bin/trash-put"]),
        ({"gio": "/bin/gio"}, ["/bin/gio", "trash"]),
        ({}, None),
    ],
)
def test_trash_command(monkeypatch, found, expected):
    monkeypatch.setattr(sweep.shutil, "which", found.get)
    assert sweep.trash_command() == expected


def test_remove_prefers_the_trash(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(sweep, "trash_command", lambda: ["/bin/trash"])
    monkeypatch.setattr(sweep.subprocess, "run", lambda args, **_: calls.append(args))
    sweep.remove(tmp_path)
    assert calls == [["/bin/trash", str(tmp_path)]]
