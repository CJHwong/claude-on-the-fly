"""One operator-editable file for everything that is not a credential.

Both lists used to live somewhere awkward. The egress allowlist was a frozenset in
`egress.py`, with additions arriving as a comma-joined environment variable, which
leaves nowhere to write down *why* a host was allowed — and for a list where every
entry is a covert channel you accepted, the reason is the important half. Brokered
commands already had a YAML file, so there were two mechanisms for two halves of
the same policy.

Now there is one file, `~/.claude-on-the-fly/config.yaml`, seeded from the bundled
template on first run so the operator opens something commented rather than
inventing a schema.

**Why a file and not the environment.** All four entrypoints call `load_dotenv()`
with no argument, so python-dotenv searches upward from the *cwd*: the `.env` in
DATA_DIR is read only when `tui/supervisor` injects it into the child, or when the
daemon happens to be launched from that directory. This file is resolved from an
absolute path, so it reads the same however the daemon started. Credentials stay
in `.env`, which is where a secret scanner expects them.

**`.env` keeps working.** Every environment variable a setting moved from still
overrides the file, and warns once naming where it went -- a deployment must not lose
its jail to a `config.yaml` it never edited, and an env var silently losing that fight
would leave nothing to look at.

**Merged per section, not per file.** A malformed `egress:` block logs an ERROR
naming itself and falls back to the bundled defaults, while `commands:` still
loads. The whole-file fallback would have been simpler, and wrong: a typo in a
list of hosts would silently revoke a brokered tool, and the operator's only clue
would be a CLI that stopped working for reasons nothing connects to the edit they
just made.

**Re-read, not restarted.** `_cached_document` invalidates on mtime and size, so
an edit takes effect at the next read with no reload machinery to get wrong. What
that cannot cover is anything decided once at startup — a bound socket, a PATH
shim, whether a service exists at all — so `check_reload` reports those by name
instead of applying them and lying about it.

**Why DATA_DIR and not the workspace.** This file decides what runs outside the
sandbox holding real credentials, and which hosts skip the operator prompt.
DATA_DIR is deliberately absent from the seatbelt write allowlist, so a sandboxed
agent cannot add itself a tool, drop a readback refusal, or pre-approve a host.
Putting it anywhere the agent can write would make it a suggestion.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger(__name__)

FILENAME = "config.yaml"

# Vetted defaults, shipped in the package beside the seatbelt profiles.
BUNDLED_SETTINGS = Path(__file__).parent / FILENAME

# Top-level keys the loader understands. A hand-edited file's likeliest failure is
# a misspelled section, which YAML accepts happily and which would otherwise do
# nothing at all with no diagnostic; `check_operator_settings` names them instead.
SECTIONS = (
    "egress",
    "commands",
    "permissions",
    "sandbox",
    "agent",
    "interim",
    "slack",
    "telegram",
    "jobs",
    "logs",
    "suggestions",
)

# Sections that ship real values in the bundled template, as opposed to a block of
# commented keys. `permissions:` is explicit because every field in it either widens
# what the agent may do or decides whether anyone is asked; the rest keep their
# defaults in the code that reads them, so an absent key means "whatever this build
# does", not "whatever the template happened to say".
DEFAULTED_SECTIONS = ("egress", "commands", "permissions")


@dataclass(frozen=True)
class Field:
    """One setting that used to be an environment variable.

    The flat `SCREAMING_SNAKE` key space stays the internal contract rather than
    being replaced with a typed config object: `checks.py`, `preflight.py`, and
    `tui/env_editor.py` are ~1500 lines built on `Mapping[str, str]`, and rewriting
    them to prove a point about types would be a far larger change than the one
    being asked for. The YAML is nested for whoever edits it; flat past this line.

    `sep` is set only for list-valued fields, and it is per field because the env
    forms never agreed: paths were colon-joined (like PATH), sender ids
    comma-joined. Guessing one would corrupt the other.
    """

    env: str
    sep: str = ""


# YAML path -> the environment variable it replaces. Read by `get`.
FIELDS: dict[str, Field] = {
    "sandbox.mode": Field("COTF_SANDBOX"),
    "sandbox.fs": Field("COTF_SANDBOX_FS"),
    "sandbox.extra_paths": Field("COTF_SANDBOX_EXTRA_PATHS", sep=":"),
    "sandbox.broker_only_loopback": Field("COTF_SANDBOX_BROKER_ONLY_LOOPBACK"),
    "agent.backend": Field("AGENT_BACKEND"),
    "agent.claude.mode": Field("CLAUDE_MODE"),
    "agent.claude.model": Field("CLAUDE_MODEL"),
    "agent.codex.mode": Field("CODEX_MODE"),
    "agent.codex.model": Field("CODEX_MODEL"),
    "agent.ollama.model": Field("OLLAMA_MODEL"),
    "agent.ollama.effort": Field("OLLAMA_EFFORT"),
    "agent.skills_cache_ttl_seconds": Field("SKILLS_CACHE_TTL_SECONDS"),
    "agent.pricing_ttl_seconds": Field("COTF_PRICING_TTL_SECONDS"),
    "agent.auto_compact_pct": Field("COTF_AUTO_COMPACT_PCT"),
    "agent.pty.auto_install": Field("COTF_AUTO_INSTALL_PTY"),
    "agent.pty.auto_refresh": Field("COTF_PTY_AUTO_REFRESH"),
    # Its own section because every section here names a module, and mid-turn
    # progress has one: `interim.py`. It is not a backend knob (it survives a
    # backend swap) and not platform rendering (the pacing question is the same on
    # every frontend), so it belongs beside neither `agent:` nor `slack:`.
    "interim.progress": Field("COTF_INTERIM_PROGRESS"),
    "interim.warmup_seconds": Field("COTF_INTERIM_WARMUP_SECONDS"),
    "interim.min_gap_seconds": Field("COTF_INTERIM_MIN_GAP_SECONDS"),
    "slack.allowed_senders": Field("SLACK_ALLOWED_SENDER_IDS", sep=","),
    "slack.blocked_senders": Field("SLACK_BLOCKED_SENDER_IDS", sep=","),
    "slack.silent_senders": Field("SLACK_SILENT_SENDER_IDS", sep=","),
    "slack.stats": Field("SLACK_STATS_MODE"),
    "slack.slash_command": Field("SLACK_SLASH_COMMAND"),
    "slack.job_command": Field("SLACK_JOB_COMMAND"),
    "slack.session_cap": Field("SLACK_SESSION_CAP"),
    "slack.reply_soft_limit": Field("SLACK_REPLY_SOFT_LIMIT"),
    "telegram.allowed_user_id": Field("TELEGRAM_ALLOWED_USER_ID"),
    "telegram.stats": Field("TELEGRAM_STATS_MODE"),
    "jobs.queue_kind": Field("JOBS_QUEUE_KIND"),
    "jobs.concurrency": Field("JOBS_CONCURRENCY"),
    "jobs.poll_interval_s": Field("JOBS_POLL_INTERVAL_S"),
    "jobs.timeout": Field("JOBS_TIMEOUT"),
    "logs.keep_days": Field("COTF_LOG_KEEP_DAYS"),
    "logs.host_tag": Field("COTF_HOST_TAG"),
    "suggestions.enabled": Field("COTF_SUGGESTIONS_ENABLED"),
}

# Sections and fields that are read once, at startup, because acting on them means
# binding a socket, writing a PATH shim, or deciding whether a service is
# constructed at all. Re-reading the file cannot apply these, so `check_reload`
# names them rather than leaving an operator to wonder why their edit did nothing.
# Dotted paths address one field; a bare name covers a whole section.
RESTART_REQUIRED = (
    # The spawn path must keep using the mode whose broker/proxy/jail services
    # were constructed at startup.
    "sandbox.mode",
    # PATH shims, written once into the agent's environment.
    "commands",
    # Decides whether the approval service is constructed at all, and whether the
    # shim and MCP config are written.
    "permissions.mode",
    # Registered with Slack at startup. An edit that took effect locally would point
    # the daemon at a command nothing is listening for.
    "slack.slash_command",
    # The queue adapter is built once, and the worker on the other side of it was
    # started separately with the old value.
    "jobs.queue_kind",
    # The worker and its runner are constructed once. Applying only some of
    # these values to a live loop would create a misleading mixed posture.
    "jobs.concurrency",
    "jobs.poll_interval_s",
    "jobs.timeout",
)


def operator_settings() -> Path:
    """The operator's own file. Resolved per call so tests can redirect DATA_DIR."""
    from claude_on_the_fly.agent import DATA_DIR

    return DATA_DIR / FILENAME


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
        problem = str(getattr(exc, "problem", "") or exc.__class__.__name__)
        context = str(getattr(exc, "context", "") or "")
        detail = f"{context}: {problem}" if context else problem
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise ValueError(f"not valid YAML{location}: {detail}") from None
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"top level must be a mapping, got {type(raw).__name__}")
    # cast, not annotate: dict is invariant in its key type, so a narrowed
    # dict[Unknown, Unknown] will not assign to dict[str, Any].
    return cast("dict[str, Any]", raw)


# Parsed documents by path, with the (mtime_ns, size) they were parsed at.
_DOCUMENTS: dict[Path, tuple[int, int, dict[str, Any]]] = {}


def _cached_document(path: Path) -> dict[str, Any]:
    """`read_document`, re-parsing only when the file has changed on disk.

    The loaders below run on every session, CONNECT, and tool call, so parsing
    YAML each time is waste — but caching until restart would cost the property
    that makes this file pleasant to own, which is that saving it is enough.

    `st_mtime_ns`, not `st_mtime`: the float loses precision at Unix-timestamp
    magnitude, and two saves inside its resolution would read as no save at all.
    Size is checked as well so a filesystem with a coarse clock still notices.

    Returns a deep copy: callers get sections by reference, and one that mutated
    what it was handed would rewrite policy for every later reader with nothing in
    the file to explain it.
    """
    stat = path.stat()
    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _DOCUMENTS.get(path)
    if cached is not None and cached[:2] == stamp:
        return copy.deepcopy(cached[2])
    # Deliberately not cached on failure: `operator` logs an ERROR every time it
    # reads a broken file, and an operator watching the log for their fix to land
    # needs the next read to actually happen.
    document = read_document(path)
    _DOCUMENTS[path] = (*stamp, document)
    return copy.deepcopy(document)


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

    A *malformed* template still raises. That is a packaging bug which must not ship,
    and swallowing it would produce a build whose whole policy is quietly empty.

    A *missing* one is reported and treated as empty instead. The distinction is worth
    the extra branch: every setting read now goes through here, so an absent file turned
    a packaging problem into a `FileNotFoundError` surfacing from whatever happened to
    ask for a setting first -- observed as a hard crash inside a TUI log refresh, three
    frames from anything to do with config. Empty leaves a degraded but coherent posture
    (no allowlist, no shims, approvals off) and one ERROR per read saying why.
    """
    try:
        document = _cached_document(BUNDLED_SETTINGS)
    except OSError as exc:
        logger.error(
            "settings: the bundled template %s is missing or unreadable (%s). This is "
            "a broken install, not a config mistake: no egress allowlist, no brokered "
            "commands, and approvals off until it is fixed. Reinstall the package.",
            BUNDLED_SETTINGS,
            exc,
        )
        return {}
    return _section(document, name)


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
        document = _cached_document(path)
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


def _from_environment(name: str, env: Mapping[str, str] | None) -> str | None:
    """One environment variable, without ever binding the mapping to a caller's frame.

    This exists for one reason, and it is not style. A traceback renderer that prints
    frame locals -- rich, which the TUI uses -- prints every local it finds, so a
    function holding `os.environ` writes the operator's entire environment into any
    crash report that passes through it. `settings.resolved` is now on the path of
    every setting read, so that was every crash.

    Observed, not theorised: a missing bundled template raised out of `_cached_document`
    and the rendered traceback carried three unrelated API keys out of the shell that
    launched the TUI. Keeping the mapping inside a frame that only calls `.get` -- which
    cannot raise -- means it is never a local anywhere a traceback will look.
    """
    return (os.environ if env is None else env).get(name)


def _environ_snapshot(env: Mapping[str, str] | None) -> dict[str, str]:
    """A plain copy of the environment. Same reasoning as `_from_environment`.

    The copy is still a local in *this* frame, which is why the frame does nothing else:
    one `dict()` call, nothing that can raise between binding and returning.
    """
    return dict(os.environ if env is None else env)


def _flatten(field: Field, value: object) -> str:
    """One YAML value in the string form its environment variable had.

    Raises ValueError so the caller can name the field and fall back, rather than
    letting a list where a scalar belongs take a daemon down at first read.
    """
    if isinstance(value, bool):
        # The readers test membership in a truthy set, so "0" reads as off. `False`
        # stringified to "False", which is neither truthy nor obviously not.
        return "1" if value else "0"
    if isinstance(value, list):
        if not field.sep:
            raise ValueError(f"expected a single value, got a list of {len(value)}")
        return field.sep.join(str(item).strip() for item in value)
    if isinstance(value, dict):
        raise ValueError("expected a value, got a nested block")
    return str(value).strip()


def _dig(document: Mapping[str, Any], path: str) -> object:
    """The value at a dotted path, or None if any step is missing."""
    node: object = document
    for step in path.split("."):
        if not isinstance(node, Mapping):
            return None
        node = node.get(step)
        if node is None:
            return None
    return node


def _document() -> dict[str, Any]:
    """Bundled defaults with the operator's file merged over, section by section."""
    names = {path.split(".")[0] for path in FIELDS}
    return {name: {**bundled(name), **operator(name)} for name in names}


def resolved(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Every migrated setting, keyed by the environment variable it replaces.

    `env` defaults to the process environment. The TUI doctor passes a different
    one: it merges `~/.claude-on-the-fly/.env` over `os.environ` to model what a
    supervised daemon child will actually receive, and reading `os.environ` here
    instead would show the operator a verdict their daemon does not share.

    **A legacy environment variable wins over the file, and says so.** That
    direction is deliberate and it is the whole backward-compatibility story: a
    deployment whose `.env` sets `COTF_SANDBOX=jail` must not have its jail
    switched off by a `config.yaml` it never edited. File-wins would have done
    exactly that on the first upgrade, silently, to the setting where silence costs
    the most.

    A field that cannot be flattened is dropped with an ERROR naming it, leaving
    the reader's own default in force. Failing the daemon over one malformed knob
    would take a deployment down for a typo in a field it may not even use.
    """
    document = _document()
    values: dict[str, str] = {}
    for path, field in FIELDS.items():
        value = _dig(document, path)
        if value is None:
            continue
        try:
            values[field.env] = _flatten(field, value)
        except ValueError as exc:
            logger.error(
                "settings: ignoring `%s` in %s (%s); the built-in default for %s "
                "stays in effect",
                path,
                operator_settings(),
                exc,
                field.env,
            )
    for path, field in FIELDS.items():
        override = _from_environment(field.env, env)
        if override is None:
            continue
        values[field.env] = override
        _warn_legacy(field.env, path)
    return values


# Legacy variables already named in the log. One line per variable per process: the
# point is to tell an operator where the setting moved to, and repeating it on every
# read would bury the rest of the log instead.
_LEGACY_WARNED: set[str] = set()


def _warn_legacy(env: str, path: str) -> None:
    if env in _LEGACY_WARNED:
        return
    _LEGACY_WARNED.add(env)
    logger.warning(
        "settings: %s is set in the environment and still wins, but it has moved to "
        "`%s:` in %s. Move it there and unset the variable; the environment form is "
        "undocumented now and will not gain new options.",
        env,
        path,
        operator_settings(),
    )


def get(name: str, default: str = "") -> str:
    """One migrated setting by its environment-variable name.

    A drop-in for `os.environ.get(name, default)` at the call sites that moved, so
    the diff at each of them is the function name and nothing else. Anything absent
    from both the file and the environment returns `default`, which keeps the
    defaults where they already were -- in the code that reads them -- rather than
    making them depend on whether an operator's file happens to carry a key.

    Absent and empty stay distinct, exactly as `os.environ.get` has them. Some
    settings read a blank value as a deliberate "off" rather than as unset (an empty
    `SLACK_JOB_COMMAND` disables the trigger), and collapsing the two here would
    take that away from every one of them to save a `.strip()` at a few call sites.
    """
    return resolved().get(name, default)


def lookup(name: str) -> str | None:
    """One migrated setting, or None when it is set nowhere.

    For the handful of readers that branch on absence rather than on emptiness --
    `pricing._ttl_seconds` treats an unset TTL as "use the default" and an unparseable
    one as an error worth logging, which needs the two cases told apart.
    """
    return resolved().get(name)


def environment(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """`env` (default: the process environment) with the migrated settings over it.

    For the Mapping-based checkers in `checks.py`, which take an env mapping and are
    pure functions over it. Handing them this instead of `os.environ` is the entire
    change needed to make ~1500 lines of validation see the file, and it keeps them
    testable against a hand-built dict, which is the reason they are shaped that way.

    `resolved(env)` has already settled the environment-wins question against the
    same mapping, so layering it back on top is not a second precedence decision --
    it is the same answer, now reachable in one lookup alongside an unmigrated key
    like a token.
    """
    # `dict(...)` rather than binding the mapping: see `_from_environment`.
    return {**_environ_snapshot(env), **resolved(env)}


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
    logger.info("settings: policy from %s", path)
    if not path.is_file():
        return
    try:
        document = _cached_document(path)
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
    _remember_restart_state()


def _restart_state() -> dict[str, str]:
    """The restart-required fields as they read right now.

    Serialised per field rather than hashed over the whole document so a change can
    be reported by name. JSON with sorted keys because a YAML mapping's order is
    not meaningful and reordering one is not a change.
    """
    merged = {
        name: {**bundled(name), **operator(name)}
        for name in {path.split(".")[0] for path in RESTART_REQUIRED}
    }
    state: dict[str, str] = {}
    for path in RESTART_REQUIRED:
        section, _, field = path.partition(".")
        value = merged[section].get(field) if field else merged[section]
        state[path] = json.dumps(value, sort_keys=True, default=str)
    return state


_RESTART_STATE: dict[str, str] = {}
_STARTUP_VALUES: dict[str, object] = {}


def _remember_restart_state() -> None:
    """Record what the restart-required fields were at startup."""
    global _RESTART_STATE, _STARTUP_VALUES
    _RESTART_STATE = _restart_state()
    _STARTUP_VALUES = {path: _current_value(path) for path in RESTART_REQUIRED}


def _current_value(path: str) -> object:
    """One dotted value with its legacy environment override applied."""
    field = FIELDS.get(path)
    if field is not None:
        override = _from_environment(field.env, None)
        if override is not None:
            return override
    section, _, key = path.partition(".")
    merged = {**bundled(section), **operator(section)}
    return copy.deepcopy(merged.get(key) if key else merged)


def startup_value(path: str, default: object = None) -> object:
    """The value of a restart-required field when startup validation ran.

    Before a daemon has established its baseline (notably in unit tests and
    one-shot CLI commands), resolve the current document. Runtime readers use
    this for settings whose supporting services cannot be rebuilt safely.
    """
    value = _STARTUP_VALUES[path] if path in _STARTUP_VALUES else _current_value(path)
    return copy.deepcopy(default if value is None else value)


def check_reload() -> tuple[str, ...]:
    """Restart-required fields that have been edited since startup, by name.

    Everything else in this file is picked up by the next read, which is why there
    is no reload hook to call: saving the file *is* the mechanism. These are the
    fields that mechanism cannot serve, because acting on them means binding a
    socket, writing a PATH shim, or constructing a service that was never built.

    Reporting rather than applying is the deliberate half. Tearing down a
    credential-holding broker or rewriting the agent's PATH while a turn is in
    flight trades a config annoyance for a class of mid-turn failure, and the
    fields here are set once per deployment. Returns them so the caller can say so
    on the frontend; empty when nothing needing a restart has changed.
    """
    if not _RESTART_STATE:
        return ()
    current = _restart_state()
    return tuple(
        path for path, value in current.items() if _RESTART_STATE.get(path) != value
    )
