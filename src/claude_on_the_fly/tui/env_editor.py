"""Open $EDITOR on the .env file, diff before vs after, map changed keys to
the daemons that need a restart.

Pure functions live here. The TUI `e` binding glues this to the supervisor
(see screens/env_diff.py).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from claude_on_the_fly.checks import FRONTEND_ENV_VARS


@dataclass(frozen=True)
class EnvDiff:
    added: dict[str, str] = field(default_factory=dict)
    removed: dict[str, str] = field(default_factory=dict)
    changed: dict[str, tuple[str, str]] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not self.added and not self.removed and not self.changed

    def changed_keys(self) -> set[str]:
        return set(self.added) | set(self.removed) | set(self.changed)


def diff_env(before: dict[str, str | None], after: dict[str, str | None]) -> EnvDiff:
    """Compute the diff between two parsed env-file mappings.

    Treats `None` (key present but no value) as missing.
    """
    b = {k: v for k, v in before.items() if v is not None}
    a = {k: v for k, v in after.items() if v is not None}
    added = {k: a[k] for k in a.keys() - b.keys()}
    removed = {k: b[k] for k in b.keys() - a.keys()}
    changed = {k: (b[k], a[k]) for k in a.keys() & b.keys() if a[k] != b[k]}
    return EnvDiff(added=added, removed=removed, changed=changed)


def affected_daemons(diff: EnvDiff) -> set[str]:
    """Daemons whose declared env vars overlap with the diff."""
    keys = diff.changed_keys()
    return {name for name, vars_ in FRONTEND_ENV_VARS.items() if keys & set(vars_)}


def _resolve_editor() -> list[str]:
    """Resolve $EDITOR, splitting on whitespace so 'code --wait' works."""
    raw = os.environ.get("EDITOR", "").strip()
    if not raw:
        return ["vi"]
    return raw.split()


def open_in_editor(
    path: Path,
    *,
    seed: str | None = None,
    runner=subprocess.run,
) -> bool:
    """Open `$EDITOR` on `path`. Returns True if the file was created from
    `seed` (a template) because it didn't exist yet.

    Used for editing config files (cron.yaml) where we want a commented
    template on first open rather than an empty buffer.
    """
    created = False
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(seed or "")
        created = True
    runner([*_resolve_editor(), str(path)], check=False)
    return created


def edit_and_diff(
    env_file: Path,
    *,
    runner=subprocess.run,
    create_if_missing: bool = True,
) -> EnvDiff:
    """Open $EDITOR on env_file. Returns the diff after the editor exits.

    If env_file does not exist and create_if_missing is True, create it first
    so the editor opens a writable empty buffer.
    """
    if create_if_missing and not env_file.exists():
        env_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(
                env_file,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            pass
        else:
            os.close(fd)

    before = dict(dotenv_values(env_file))
    cmd = [*_resolve_editor(), str(env_file)]
    runner(cmd, check=False)
    after = dict(dotenv_values(env_file))
    return diff_env(before, after)
