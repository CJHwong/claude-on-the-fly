#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Remove finished git clones from cotf workspaces.

How to make it work:
  1. Install uv (https://docs.astral.sh/uv/) and git. Nothing else is needed.
  2. Run it once by hand without --apply. It lists every clone it would remove and
     why it keeps the rest:
       uv run --script sweep_workspace_clones.py
  3. Add a bare command entry to cron.yaml:
       - name: sweep-clones
         cron: "10 3 * * *"
         timeout: 900
         command: uv run --script /path/to/sweep_workspace_clones.py --apply
  4. Optional: install a trash command (`trash` on macOS, trash-cli's `trash-put`, or
     GNOME's `gio`) so a removal can be undone. Without one, removal is permanent.
  A data directory other than ~/.claude-on-the-fly needs --root <dir>/workspaces.

An agent that changes code clones the repo into its conversation's workspace. Once the
branch is pushed, the clone holds nothing that is not on the remote, and cotf never
sweeps a workspace on its own.

A clone is removed only when all of these hold:
  - it was last touched more than --days ago (newest of the directory, .git/HEAD and
    .git/logs/HEAD);
  - `git status --porcelain` is empty;
  - every local branch head is reachable from some remote-tracking ref;
  - no other clone borrows its object store through `objects/info/alternates`.
Everything else is reported and left alone. Default is a dry run; --apply acts.
Removal goes through a trash command when one is installed, so it is recoverable.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path.home() / ".claude-on-the-fly" / "workspaces"
MAX_DEPTH = 4  # slack/dm/<id>/<clone> is the deepest a conversation clone sits
SKIP = {".sent", "outbox", "memory", "inbox", "node_modules"}


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def find_clones(root: Path) -> list[Path]:
    """Directories holding a `.git` directory, without descending into one."""
    found: list[Path] = []

    def walk(directory: Path, depth: int) -> None:
        if (directory / ".git").is_dir():
            found.append(directory)
            return
        if depth == MAX_DEPTH:
            return
        for child in sorted(directory.iterdir()):
            if child.is_dir() and not child.is_symlink() and child.name not in SKIP:
                walk(child, depth + 1)

    walk(root, 0)
    return found


def last_touched(repo: Path) -> float:
    """Newest of the directory, HEAD and the reflog. Not the index: `git status`
    rewrites it, so a dry run would make every clone look fresh."""
    stamps = [repo.stat().st_mtime]
    for name in ("HEAD", "logs/HEAD"):
        path = repo / ".git" / name
        if path.exists():
            stamps.append(path.stat().st_mtime)
    return max(stamps)


def unpushed_branches(repo: Path) -> list[str]:
    """Local branches whose head no remote-tracking ref contains."""
    refs = git(
        repo, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads"
    )
    stray = []
    for line in refs.splitlines():
        branch, sha = line.rsplit(" ", 1)
        if not git(repo, "branch", "-r", "--contains", sha).strip():
            stray.append(branch)
    return stray


def borrowed_stores(clones: list[Path]) -> set[Path]:
    """Object stores another clone reads through `objects/info/alternates`."""
    stores: set[Path] = set()
    for clone in clones:
        alternates = clone / ".git/objects/info/alternates"
        if not alternates.is_file():
            continue
        for line in filter(None, map(str.strip, alternates.read_text().splitlines())):
            path = Path(line)
            if not path.is_absolute():
                path = clone / ".git/objects" / path
            stores.add(path.resolve())
    return stores


def verdict(repo: Path, days: float, borrowed: set[Path]) -> tuple[str, str]:
    """('remove' | 'keep', reason)."""
    age_days = (time.time() - last_touched(repo)) / 86400
    if age_days < days:
        return "keep", f"touched {age_days:.1f}d ago"
    try:
        if git(repo, "status", "--porcelain").strip():
            return "keep", "uncommitted changes"
        stray = unpushed_branches(repo)
    except RuntimeError as exc:
        return "keep", f"git error: {str(exc).splitlines()[0]}"
    if stray:
        return "keep", f"unpushed: {', '.join(stray)}"
    if (repo / ".git/objects").resolve() in borrowed:
        return "keep", "object store used by another clone"
    return "remove", f"clean, pushed, {age_days:.0f}d old"


def trash_command() -> list[str] | None:
    """macOS `trash`, trash-cli's `trash-put`, or GNOME's `gio trash`."""
    for name in ("trash", "trash-put"):
        if found := shutil.which(name):
            return [found]
    if found := shutil.which("gio"):
        return [found, "trash"]
    return None


def remove(repo: Path) -> None:
    command = trash_command()
    if command:
        subprocess.run([*command, str(repo)], check=True, timeout=600)
    else:
        shutil.rmtree(repo)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--days", type=float, default=7)
    parser.add_argument("--apply", action="store_true", help="remove instead of report")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    if not args.root.is_dir():
        print(f"no workspace root at {args.root}", file=sys.stderr)
        return 1
    clones = find_clones(args.root)
    borrowed = borrowed_stores(clones)
    removed = kept = 0
    for repo in clones:
        action, reason = verdict(repo, args.days, borrowed)
        rel = repo.relative_to(args.root)
        if action == "keep":
            kept += 1
            print(f"keep          {rel}  ({reason})")
            continue
        if args.apply:
            remove(repo)
        removed += 1
        print(f"{'removed' if args.apply else 'would remove'}  {rel}  ({reason})")
    print(f"{'removed' if args.apply else 'would remove'} {removed}, kept {kept}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
