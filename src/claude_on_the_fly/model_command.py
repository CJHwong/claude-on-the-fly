"""The `$model` command: pin one conversation to another model or effort.

One parser and one validator behind every frontend. Slack spells it `$model`,
because its slash commands are blocked inside threads. Telegram spells it
`/model`, because it delivers slash commands in every chat. The spelling is the
frontend's; everything behind it is shared, so the two cannot drift.

Three things live here and nowhere else:

- the grammar: a model name, an optional effort level, and `default` to unpin,
- the check against whatever catalogue the mode answers to,
- the wording of the ack and of every refusal.

Nothing here reads the settings file or holds state. `parse` is a function of
the words, the resolved profile, and the catalogues on disk, which is what makes
every refusal testable without a daemon.

A value this module accepts is not a promise the model exists: a full id that
matches the shape but is not in a catalogue passes on purpose, because refusing
a model released after the catalogue was written is worse than letting the CLI
warn about it.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from claude_on_the_fly import envfile
from claude_on_the_fly.agent import OVERRIDABLE_FIELDS

if TYPE_CHECKING:
    from claude_on_the_fly.agent import AgentProfile

logger = logging.getLogger(__name__)

# The word that unpins a field. No model is named this, and no effort level is.
DEFAULT_TOKEN = "default"
# The two fields a command can change, read from the profile type rather than
# written again here: the orchestrator hands these keys straight to `replace`,
# and a second spelling would be a silent no-op rather than an error.
MODEL, EFFORT = OVERRIDABLE_FIELDS

# Effort sets, mirrored from the backends that validate against them. Duplicated
# as literals for the reason `checks.DEFAULT_JOB_COMMAND` is: importing a private
# name out of a backend to validate a chat command would tie this module to that
# backend's internals. `test_model_command_effort_levels_match_the_backends`
# fails on drift, so the two are checked rather than merely noted.
CLAUDE_EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})
# `max` and `ultra` are codex levels too, and the backend's own set carries both
# even though `docs/reference/config-yaml.md` still lists this key without them.
CODEX_EFFORT_LEVELS = frozenset(
    {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)

# The aliases claude resolves to its newest model of each family, from
# `claude --help`. The catalogue names them too; these are the fallback for when
# no catalogue is readable.
CLAUDE_ALIASES = ("fable", "haiku", "opus", "sonnet")

# A full model id, such as `claude-opus-5` or `gpt-5.6-sol`. The one shape
# accepted without a catalogue. Deliberately narrow: a typo of an alias
# (`sonet`, `opus4`) matches nothing here and is refused, which is the whole
# point of checking.
FULL_ID = re.compile(r"^(?:claude|gpt)-[a-z0-9][a-z0-9.-]*$")


@dataclass(frozen=True)
class Report:
    """A bare command. Nothing changes; describe where this conversation stands."""


@dataclass(frozen=True)
class Change:
    """What to store. A field mapping to None goes back to the configured value.

    A field left out of `values` is untouched, which is what makes a model-only
    command keep the effort the conversation already had. `note` is what the ack
    must add, and is empty unless something about this change is unconfirmed.
    """

    values: dict[str, str | None]
    note: str = ""


@dataclass(frozen=True)
class Refusal:
    """The request cannot be honoured. `text` is the reply, and nothing changes."""

    text: str


def parse(tokens: list[str], profile: AgentProfile) -> Report | Change | Refusal:
    """Read a model command. `tokens` is the argument list, already split.

    Two tokens at most, because a model name has no whitespace in it, in any of
    the three namespaces. A third token is somebody writing a sentence, which
    this command cannot read: it is refused rather than guessed at, since
    guessing would silently pin the conversation to the wrong model.

    A bare `default` is the whole conversation back on `config.yaml`: both
    fields. As a second word it clears only the field it sits in, so
    `$model default high` keeps the configured model at the effort asked for.
    The distinction is the difference between "put this back how it was" and
    "change one thing", and without the first there was no command that cleared
    a pinned effort while leaving the model alone.
    """
    if not tokens:
        return Report()
    if len(tokens) > 2:
        return Refusal(
            "Too many words. Give a model name, then an optional effort level."
        )
    if tokens == [DEFAULT_TOKEN]:
        # Before any catalogue is read: a conversation returning to its
        # configuration has nothing to validate, so this can never be refused
        # because a model list went missing.
        return Change({MODEL: None, EFFORT: None})

    catalogue = _catalogue(profile)
    model_token, effort_token = tokens[0], tokens[1] if len(tokens) > 1 else None

    values: dict[str, str | None] = {}
    if model_token == DEFAULT_TOKEN:
        values[MODEL] = None
    else:
        problem = _model_problem(model_token, profile, catalogue)
        if problem is not None:
            return Refusal(problem)
        values[MODEL] = model_token

    if effort_token is not None:
        # The effort belongs to the model this command leaves behind, so it is
        # checked against the one the conversation will run, not the configured
        # one. The name is resolved through the catalogue first: an alias is what
        # a person types, and the catalogue keys its effort levels by id, so
        # looking up the raw token would miss and silently fall back to the
        # backend's whole set. That made `$model haiku high` pass on a model the
        # catalogue says takes no effort at all.
        subject = _resolve_model(
            str(values[MODEL] or profile.model), profile, catalogue
        )
        problem = _effort_problem(effort_token, profile, catalogue, subject)
        if problem is not None:
            return Refusal(problem)
        values[EFFORT] = None if effort_token == DEFAULT_TOKEN else effort_token
    return Change(values, note=_note_for(values, profile, catalogue))


def describe(profile: AgentProfile, *, pinned: bool) -> str:
    """The state of one conversation, and what it could be switched to.

    `pinned` says at least one field was set from a chat rather than from the
    configuration file, which is worth saying: it is the only way to tell a
    conversation that was moved from one that was always where it is.
    """
    catalogue = _catalogue(profile)
    where = " (pinned)" if pinned else ""
    lines = [
        f"Model: {profile.model or '(the CLI default)'}{where}. "
        f"Effort: {_effort_label(profile)}.",
        f"Backend: {profile.backend}, {profile.mode}.",
    ]
    models = _model_options(profile, catalogue)
    if models:
        lines.append(f"Models: {_join(models)}.")
    if profile.mode != "pty":
        subject = _resolve_model(profile.model, profile, catalogue)
        levels = _effort_options(profile, catalogue, subject)
        lines.append(f"Effort for {subject or 'this model'}: {_join(sorted(levels))}.")
    return "\n".join(lines)


def ack(profile: AgentProfile, *, note: str = "") -> str:
    """Confirm a change. `profile` is the conversation's state after it.

    Both fields are named even when only one moved, so a model-only command
    still tells the reader what effort it kept. The note carries anything the
    reader needs to distrust the ack, such as an unchecked name.

    No catalogue is read here. The profile already carries both answers, and a
    confirmation is not worth a subprocess.
    """
    body = (
        f"Model: {profile.model or '(the CLI default)'}. "
        f"Effort: {_effort_label(profile)}."
    )
    return f"{body}{note}"


def _note_for(
    values: dict[str, str | None], profile: AgentProfile, catalogue: Catalogue
) -> str:
    """What the ack must add about this change, or "".

    Only one thing goes here: a model name that got in on its shape because no
    catalogue was readable. Saying so is the price of accepting it, because a
    silent acceptance is exactly the quiet typo this command exists to prevent.
    """
    name = values.get(MODEL)
    if not name or not _unverified(name, profile, catalogue):
        return ""
    return " I could not read a model catalogue, so the name is unverified."


def _unverified(name: str, profile: AgentProfile, catalogue: Catalogue) -> bool:
    """True when nothing confirmed `name` beyond the shape of it."""
    if catalogue.readable:
        return False
    if profile.mode == "ollama":
        return name.endswith(":cloud")
    return bool(FULL_ID.match(name))


# --------------------------------------------------------------------------
# Catalogues
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Catalogue:
    """What one mode can be switched to, and how hard each model can think.

    `models` is None when no source answered, which is not the same as empty: an
    empty list means a source answered and offered nothing. `aliases` maps a
    short name to the catalogue id it stands for, so an effort level can be
    looked up from either spelling.
    """

    models: list[str] | None
    aliases: dict[str, str]
    effort: dict[str, list[str]]

    @property
    def readable(self) -> bool:
        return self.models is not None


def _catalogue(profile: AgentProfile) -> Catalogue:
    """Read the catalogue the profile's mode answers to.

    One source per namespace, because a model name means something different in
    each: an ollama tag, a claude id or alias, a codex slug. In ollama mode the
    ollama list is authoritative whatever backend serves it, since that is what
    the launcher is handed.
    """
    if profile.mode == "ollama":
        return Catalogue(models=_ollama_models(), aliases={}, effort={})
    if profile.backend == "codex":
        return _codex_catalogue()
    return _claude_catalogue()


def _ollama_models() -> list[str] | None:
    """Every model `ollama list` reports, or None when it cannot be read.

    Mirrors the reading `preflight.check_ollama_mode` does, including the reason
    it takes a `:cloud` model on faith: a remote model is served over the API and
    may not be listed locally. That carve-out is applied by `_model_problem`
    rather than here, so the list stays a faithful copy of what `ollama list`
    said and the caller owns the exception.
    """
    if shutil.which("ollama") is None:
        return None
    try:
        done = subprocess.run(
            ["ollama", "list"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        logger.exception("model_command: `ollama list` failed")
        return None
    if done.returncode != 0:
        logger.warning("model_command: `ollama list` exited %d", done.returncode)
        return None
    # First column of each non-header line is the model name.
    names = {line.split()[0] for line in done.stdout.splitlines()[1:] if line.strip()}
    return sorted(names)


def _claude_catalogue() -> Catalogue:
    """claude's own model catalogue, or an empty one when it cannot be read.

    The CLI caches what it fetched in `<config dir>/cache/model-catalog/`, one
    JSON file per surface, written under a name this code does not control. That
    file is the authority when it is there: it carries the effort levels each
    model accepts, which is a per-model fact no constant here can know.

    The default `~/.claude` is tried as well, because a daemon whose
    `CLAUDE_CONFIG_DIR` points elsewhere still may not have fetched a catalogue
    of its own while the CLI's default directory has one. The resolved directory
    wins when both answer.
    """
    for root in _catalogue_roots(envfile.claude_config_dir()):
        found = _read_claude_catalogue(root)
        if found is not None:
            return found
    return Catalogue(models=None, aliases={}, effort={})


def _catalogue_roots(resolved: Path) -> list[Path]:
    """Where a CLI's caches may be, most authoritative first."""
    default = Path.home() / ".claude"
    return [resolved] if resolved == default else [resolved, default]


def _read_claude_catalogue(config_dir: Path) -> Catalogue | None:
    """One directory's catalogue, or None when nothing readable is in it."""
    paths = sorted((config_dir / "cache" / "model-catalog").glob("*.json"))
    for path in paths:
        block = _load_json(path)
        models = _dig(block, "catalog", "config", "models")
        if not isinstance(models, list):
            continue
        entries = [entry for entry in models if isinstance(entry, dict)]
        ids = [str(entry.get("id")) for entry in entries if entry.get("id")]
        if not ids:
            continue
        aliases: dict[str, str] = {}
        effort: dict[str, list[str]] = {}
        for entry in entries:
            model_id = str(entry.get("id") or "")
            if not model_id:
                continue
            short = str(entry.get("short_name") or "").strip().lower()
            if short:
                aliases[short] = model_id
            effort[model_id] = _effort_from_thinking(entry.get("thinking"))
        return Catalogue(
            models=sorted(aliases) + [i for i in ids if i not in set(aliases.values())],
            aliases=aliases,
            effort=effort,
        )
    return None


def _effort_from_thinking(thinking: object) -> list[str]:
    """The levels one claude model accepts, from its catalogue entry.

    Empty means the model takes no effort at all (`claude-haiku-4-5` says
    `thinking: none`), which is a refusal, not a missing answer: the CLI accepts
    `--effort` on such a model and silently ignores it.
    """
    if not isinstance(thinking, dict):
        return []
    options = thinking.get("effort_options")
    if not isinstance(options, list):
        return []
    levels: list[str] = []
    for option in options:
        # `option.get`, never `option["id"]`: a narrowed `dict` from an `object`
        # has no key type to check a subscript against, and the type checker
        # rejects a literal key on it.
        found = option.get("id") if isinstance(option, dict) else None
        if found:
            levels.append(str(found))
    return levels


def _codex_catalogue() -> Catalogue:
    """codex's own model cache, or an empty one when it cannot be read.

    Two files, because codex writes both: `models_cache.json` for the catalogue
    it fetched, and `model.json` for the model chosen most recently. Either can
    be the only one present, so both are read and merged.
    """
    home = envfile.codex_home()
    slugs: set[str] = set()
    effort: dict[str, list[str]] = {}
    for name in ("models_cache.json", "model.json"):
        block = _load_json(home / name)
        models = block.get("models") if isinstance(block, dict) else None
        if not isinstance(models, list):
            continue
        for entry in models:
            if not isinstance(entry, dict) or not entry.get("slug"):
                continue
            slug = str(entry.get("slug"))
            slugs.add(slug)
            effort[slug] = _codex_levels(entry.get("supported_reasoning_levels"))
    if not slugs:
        return Catalogue(models=None, aliases={}, effort={})
    return Catalogue(models=sorted(slugs), aliases={}, effort=effort)


def _codex_levels(value: object) -> list[str]:
    """The levels one codex model lists, from its own catalogue entry.

    Empty means the catalogue listed none, which is not the same as the model
    taking no effort: `_effort_options` falls back to the backend's own set in
    that case, because codex omits the key on models it has not annotated.
    """
    if not isinstance(value, list):
        return []
    levels: list[str] = []
    for level in value:
        found = level.get("effort") if isinstance(level, dict) else None
        if found:
            levels.append(str(found))
    return levels


def _load_json(path: Path) -> object:
    """A JSON file, or None. A cache is never worth crashing a turn over."""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        logger.debug("model_command: %s is not readable JSON", path)
        return None


def _dig(value: object, *keys: str) -> object:
    """Walk nested mappings, returning None at the first miss."""
    for key in keys:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _model_problem(
    name: str, profile: AgentProfile, catalogue: Catalogue
) -> str | None:
    """Why `name` cannot be used, or None when it can."""
    options = _model_options(profile, catalogue)
    if name in options:
        return None
    if profile.mode == "ollama":
        # A remote model may be absent from `ollama list` and still work. Taken
        # on faith for the reason `preflight.check_ollama_mode` gives.
        if name.endswith(":cloud"):
            return None
        return _unknown("model", name, options)
    if FULL_ID.match(name):
        return None
    return _unknown("model", name, options)


def _effort_problem(
    level: str, profile: AgentProfile, catalogue: Catalogue, model: str
) -> str | None:
    """Why `level` cannot be used for `model`, or None when it can."""
    if profile.mode == "pty":
        # claude-pty resolves its own settings and takes no effort argument, so
        # the value would be stored and never reach the CLI. Refused rather than
        # accepted quietly, and the model change is refused with it: half a
        # command applied is worse than none of it.
        return (
            "claude pty mode resolves its own effort, so I changed nothing. "
            "Give the model name on its own."
        )
    if level == DEFAULT_TOKEN:
        return None
    options = _effort_options(profile, catalogue, model)
    if level in options:
        return None
    return _unknown("effort", level, sorted(options))


def _unknown(kind: str, value: str, options: list[str]) -> str:
    """A refusal that names what would have worked.

    The list is the point of the message: a person who mistyped an alias is one
    keystroke from the right answer, and only this text can tell them which.
    """
    if not options:
        return (
            f'I do not know the {kind} "{value}", and I could not read a list to offer.'
        )
    return f'I do not know the {kind} "{value}". Try: {_join(options)}.'


def _model_options(profile: AgentProfile, catalogue: Catalogue) -> list[str]:
    """Every model name this conversation can be switched to, best effort.

    The aliases come first when a catalogue names them, since they are what a
    person types. A model the catalogue knows only by its full id is listed by
    that id.
    """
    if not catalogue.readable:
        if profile.mode == "ollama":
            return []
        return sorted(CLAUDE_ALIASES) if profile.backend == "claude" else []
    return list(catalogue.models or [])


def _resolve_model(name: str, profile: AgentProfile, catalogue: Catalogue) -> str:
    """The catalogue id an alias stands for, or the name unchanged."""
    return catalogue.aliases.get(name, name) or profile.model


def _effort_options(
    profile: AgentProfile, catalogue: Catalogue, model: str
) -> frozenset[str]:
    """The levels `model` accepts on this backend.

    The catalogue answers per model when it can, which is the only way to know
    that a model takes no effort at all. The backend constant answers otherwise,
    and is what the backends themselves validate against.
    """
    known = catalogue.effort.get(model)
    if known is not None:
        return frozenset(known)
    if profile.backend == "codex":
        return CODEX_EFFORT_LEVELS
    return CLAUDE_EFFORT_LEVELS


def _effort_label(profile: AgentProfile) -> str:
    """How to name the conversation's effort in a reply."""
    if profile.mode == "pty":
        return "not used in pty mode"
    return profile.effort or "unset"


def _join(items: list[str]) -> str:
    """A comma-separated list. Ordered by the caller, never truncated."""
    return ", ".join(items)
