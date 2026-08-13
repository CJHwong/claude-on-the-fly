"""One reader for the environment a daemon actually runs with.

`DATA_DIR/.env` is only half of a deployment's environment. A daemon gets it
because `tui/supervisor.spawn` merges the file into the child; a process that
was not spawned that way sees whatever its shell exported and nothing else. Any
code that resolves a path from a variable an operator set in that file therefore
has to answer the same question the spawn path answers, or it looks at somewhere
the daemon never wrote.

That question used to be answered in `tui/supervisor`, which put it out of reach
of `transcript` and `checks` without those modules importing the TUI. It lives
here instead: no intra-package imports at module scope, so anything may use it.

**File wins on conflicts.** That is the spawn path's rule, and the readers here
exist to agree with the spawn path. It is deliberately the opposite of
`load_dotenv()`, which leaves an existing variable alone — a frontend calling
that at startup is choosing "the shell may override my file", while a viewer
asking "where did the daemon write" needs the file's answer or it is guessing.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values

logger = logging.getLogger(__name__)

# Parsed file contents, keyed by (path, mtime_ns). `dotenv_values` reparses on
# every call and the callers include per-frame TUI code, so cache the parse but
# never the merge: an environment variable set after the last read must still be
# visible on the next one.
_parsed: tuple[Path, int, dict[str, str]] | None = None


def default_env_file() -> Path:
    """The operator's `.env`. Resolved per call so tests can redirect DATA_DIR."""
    from claude_on_the_fly.agent import DATA_DIR

    return DATA_DIR / ".env"


def _file_values(env_file: Path) -> dict[str, str]:
    global _parsed
    try:
        mtime_ns = env_file.stat().st_mtime_ns
    except OSError:
        return {}
    cached = _parsed
    if cached is not None and cached[0] == env_file and cached[1] == mtime_ns:
        return cached[2]
    values = {k: v for k, v in dotenv_values(env_file).items() if v is not None}
    _parsed = (env_file, mtime_ns, values)
    return values


def merged(env_file: Path | None) -> dict[str, str]:
    """`os.environ` merged with `env_file` (if it exists). File wins.

    `None` means "no file" rather than "the default one", because the spawn path
    uses that to mean a caller who has already decided the child gets a bare
    environment.
    """
    values: dict[str, str] = dict(os.environ)
    if env_file is not None and env_file.is_file():
        values.update(_file_values(env_file))
    return values


def daemon_environment() -> dict[str, str]:
    """What a supervised daemon on this machine receives, from any process."""
    return merged(default_env_file())


def claude_config_dir(env: Mapping[str, str] | None = None) -> Path:
    """`CLAUDE_CONFIG_DIR`, or claude's default.

    Pass `env` when the caller already holds the resolved mapping (the doctor
    hands one to every check). Omit it and this reads the daemon's environment,
    which is the right default for a viewer: the interesting case is precisely
    the one where this process and the daemon disagree.
    """
    resolved = daemon_environment() if env is None else env
    return Path(resolved.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def codex_home(env: Mapping[str, str] | None = None) -> Path:
    """`CODEX_HOME`, or codex's default.

    The shared one. Each workspace gets its own home for the rollouts it writes
    (`codex_state.home_dir`); this is the operator-level directory those link
    their config and credential back to, and where rollouts written before
    per-workspace homes existed still live.
    """
    resolved = daemon_environment() if env is None else env
    return Path(resolved.get("CODEX_HOME") or Path.home() / ".codex")
