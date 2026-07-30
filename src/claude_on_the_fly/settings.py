"""One operator-editable file for sandbox policy: hosts and brokered commands.

Both lists used to live somewhere awkward. The egress allowlist was a frozenset in
`egress.py`, with additions arriving as a comma-joined environment variable, which
leaves nowhere to write down *why* a host was allowed — and for a list where every
entry is a covert channel you accepted, the reason is the important half. Brokered
commands already had a YAML file, so there were two mechanisms for two halves of
the same policy.

Now there is one file, `~/.claude-on-the-fly/sandbox.yaml`, seeded from the
bundled template on first run so the operator opens something commented rather
than inventing a schema.

**Merged per section, not per file.** A malformed `egress:` block logs an ERROR
naming itself and falls back to the bundled defaults, while `commands:` still
loads. The whole-file fallback would have been simpler, and wrong: a typo in a
list of hosts would silently revoke a brokered tool, and the operator's only clue
would be a CLI that stopped working for reasons nothing connects to the edit they
just made.

**Why DATA_DIR and not the workspace.** This file decides what runs outside the
sandbox holding real credentials, and which hosts skip the operator prompt.
DATA_DIR is deliberately absent from the seatbelt write allowlist, so a sandboxed
agent cannot add itself a tool, drop a readback refusal, or pre-approve a host.
Putting it anywhere the agent can write would make it a suggestion.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger(__name__)

# Vetted defaults, shipped in the package beside the seatbelt profiles.
BUNDLED_SETTINGS = Path(__file__).parent / "sandbox.yaml"

# Top-level keys the loader understands. A hand-edited file's likeliest failure is
# a misspelled section, which YAML accepts happily and which would otherwise do
# nothing at all with no diagnostic; `check_operator_settings` names them instead.
SECTIONS = ("egress", "commands")


def operator_settings() -> Path:
    """The operator's own file. Resolved per call so tests can redirect DATA_DIR."""
    from claude_on_the_fly.agent import DATA_DIR

    return DATA_DIR / "sandbox.yaml"


def read_document(path: Path) -> dict[str, Any]:
    """Parse a settings file into a mapping. Raises ValueError if it is not one.

    An empty file is a mapping with nothing in it, not an error: commenting every
    line out is a legitimate way to say "bundled defaults, please".
    """
    import yaml

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        # Normalised to ValueError so callers have one exception type to catch. A
        # YAMLError is not a ValueError, so without this an unparseable file took
        # the daemon down at startup instead of falling back.
        raise ValueError(f"not valid YAML ({exc.__class__.__name__})") from None
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"top level must be a mapping, got {type(raw).__name__}")
    # cast, not annotate: dict is invariant in its key type, so a narrowed
    # dict[Unknown, Unknown] will not assign to dict[str, Any].
    return cast("dict[str, Any]", raw)


def _section(document: dict[str, Any], name: str) -> dict[str, Any]:
    """One section of an already-parsed document, or {} if it is absent."""
    value = document.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"`{name}:` must be a mapping, got {type(value).__name__}")
    return cast("dict[str, Any]", value)


def bundled(name: str) -> dict[str, Any]:
    """One section of the bundled defaults.

    Deliberately does not fall back: a broken bundled file is a packaging bug, and
    swallowing it would ship a build whose whole policy is quietly empty.
    """
    return _section(read_document(BUNDLED_SETTINGS), name)


def operator(name: str) -> dict[str, Any]:
    """One section of the operator's file, or {} if it is absent or unusable.

    The two failure logs differ on purpose. An unreadable *file* means none of the
    operator's additions are in effect, anywhere; an unreadable *section* means the
    rest of the file still loaded. Conflating them sends whoever reads the log
    hunting through edits that were fine.
    """
    path = operator_settings()
    if not path.is_file():
        return {}
    try:
        document = read_document(path)
    except (ValueError, OSError) as exc:
        logger.error(
            "settings: ignoring all of %s (%s); bundled defaults are in effect, so "
            "nothing you added there is active",
            path,
            exc,
        )
        return {}
    try:
        return _section(document, name)
    except ValueError as exc:
        logger.error(
            "settings: ignoring the `%s:` section of %s (%s); its bundled defaults "
            "are in effect. Other sections still load.",
            name,
            path,
            exc,
        )
        return {}


def seed_operator_settings() -> Path | None:
    """Copy the bundled template to the operator path if nothing is there yet.

    Returns the path when it wrote one, None otherwise. The point is that the first
    thing an operator opens is the commented template, with every field and its
    rationale already in front of them, rather than a blank file whose schema they
    have to go find. Never overwrites: an existing file is theirs.
    """
    path = operator_settings()
    if path.exists():
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(BUNDLED_SETTINGS, path)
    except OSError as exc:
        # Not fatal. Every loader falls back to the bundled defaults, so a daemon
        # that cannot seed still runs the vetted policy.
        logger.warning("settings: could not seed %s (%s)", path, exc)
        return None
    logger.info("settings: seeded %s from the bundled defaults", path)
    return path


def check_operator_settings() -> None:
    """Seed the operator file, then report anything wrong with it, once, at startup.

    Validation belongs here rather than only in the loaders because the loaders run
    at first use — a session's first CONNECT, a broker construction — which is a
    long way from the edit that broke it. Naming the problem at boot is the
    difference between a config typo and a mystery.
    """
    seed_operator_settings()
    path = operator_settings()
    logger.info("settings: sandbox policy from %s", path)
    if not path.is_file():
        return
    try:
        document = read_document(path)
    except (ValueError, OSError) as exc:
        logger.error(
            "settings: %s is unusable (%s); bundled defaults in effect", path, exc
        )
        return
    unknown = sorted(key for key in document if key not in SECTIONS)
    if unknown:
        logger.error(
            "settings: %s has unrecognised top-level key(s) %s, which do nothing. "
            "Sections are %s.",
            path,
            unknown,
            list(SECTIONS),
        )
    for name in SECTIONS:
        try:
            _section(document, name)
        except ValueError as exc:
            logger.error("settings: %s: %s", path, exc)
