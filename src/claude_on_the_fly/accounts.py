"""Named credentials and durable account selection for chat conversations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from claude_on_the_fly import settings

if TYPE_CHECKING:
    from collections.abc import Iterator

    from claude_on_the_fly.agent import AgentProfile


class AccountError(ValueError):
    """Account configuration or saved selection cannot be used."""


@dataclass(frozen=True)
class Account:
    name: str
    backend: str
    home: Path | None = None
    mode: str | None = None
    model: str | None = None

    @property
    def source_home(self) -> Path:
        return self.home if self.home is not None else Path.home() / f".{self.backend}"


@dataclass(frozen=True)
class Selection:
    account: Account | None
    pool: tuple[str, ...] = ()


_CURRENT: ContextVar[Account | None] = ContextVar("account", default=None)


def current() -> Account | None:
    return _CURRENT.get()


@contextmanager
def use(account: Account | None, *, mode: str | None = None) -> Iterator[None]:
    selected = replace(account, mode=mode) if account is not None and mode else account
    token = _CURRENT.set(selected)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def _configuration(section: str, *, startup: bool = True) -> dict:
    if startup and section == "accounts" and section in settings._STARTUP_VALUES:
        return cast("dict[str, Any]", settings.startup_value(section, {}))
    path = settings.operator_settings()
    if not path.is_file():
        return {}
    try:
        document = settings._cached_document(path)
    except (OSError, ValueError) as exc:
        raise AccountError("Cannot read account configuration") from exc
    block = document.get(section, {})
    if block is None:
        block = {}
    if not isinstance(block, dict):
        raise AccountError(f"{section}: must be a mapping")
    return cast("dict[str, Any]", block)


def _account(name: str, entry: object) -> Account:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise AccountError(f"Invalid account name: {name!r}")
    if not isinstance(entry, dict):
        raise AccountError(f"accounts.{name}: must be a mapping")
    entry = cast("dict[str, Any]", entry)
    if set(entry.keys()).difference({"backend", "home", "mode", "model"}):
        raise AccountError(f"accounts.{name}: unknown fields")
    backend = entry.get("backend")
    if backend not in ("claude", "codex"):
        raise AccountError(f"accounts.{name}.backend: use claude or codex")
    mode, model = entry.get("mode"), entry.get("model")
    if mode is not None and mode not in ("native", "ollama", "pty"):
        raise AccountError(f"accounts.{name}.mode: use native, ollama or pty")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise AccountError(f"accounts.{name}.model: must be a nonempty string")
    if mode == "ollama" and not model:
        raise AccountError(f"accounts.{name}: ollama requires a model")
    home = entry.get("home")
    if home is not None and (not isinstance(home, str) or not home.strip()):
        raise AccountError(f"accounts.{name}.home: must be a path or null")
    directory = Path(home).expanduser() if home is not None else None
    if directory is not None and not directory.is_absolute():
        raise AccountError(f"accounts.{name}.home: must be an absolute path")
    return Account(name, backend, directory, mode, model)


def all_accounts() -> dict[str, Account]:
    registry = {
        str(name): _account(str(name), entry)
        for name, entry in _configuration("accounts").items()
    }
    roots = {account.source_home.resolve() for account in registry.values()}
    if registry:
        from claude_on_the_fly import envfile

        environment = envfile.daemon_environment()
        roots.update(
            (
                envfile.claude_config_dir(environment).resolve(),
                envfile.codex_home(environment).resolve(),
            )
        )
    for root in roots:
        if Path.home().resolve().is_relative_to(root):
            raise AccountError(
                "Account home must not contain the user's home directory"
            )
        if any(other != root and other.is_relative_to(root) for other in roots):
            raise AccountError("Account homes must not contain another account home")
    return registry


def named(name: str, backend: str) -> Account:
    account = all_accounts().get(name)
    if account is None:
        raise AccountError(f"Unknown account: {name!r}")
    if account.backend != backend:
        raise AccountError(
            f"Account {name!r} has backend {account.backend}, expected {backend}"
        )
    return account


def _aliases(value: object, backend: str) -> tuple[str, ...]:
    if value is None:
        return ()
    values = value if isinstance(value, list) else [value]
    if not values or any(not isinstance(name, str) or not name for name in values):
        raise AccountError(
            f"Account routing for {backend} must name an account or a nonempty list"
        )
    if len(set(values)) != len(values):
        raise AccountError("Account fallback lists must not repeat an alias")
    aliases = cast("list[str]", values)
    for name in aliases:
        named(name, backend)
    return tuple(aliases)


def _rule(block: object, backend: str) -> tuple[Account | None, tuple[str, ...]]:
    if not isinstance(block, dict):
        raise AccountError("Account routing must use mappings")
    block = cast("dict[str, Any]", block)
    if set(block.keys()).difference({"claude", "codex"}):
        raise AccountError("Account routing supports claude and codex only")
    value = block.get(backend)
    aliases = _aliases(value, backend)
    account = named(aliases[0], backend) if aliases else None
    return account, aliases if isinstance(value, list) else ()


def _choice(block: object, backend: str) -> Account | None:
    return _rule(block, backend)[0]


def default_for(backend: str) -> Account | None:
    return _choice(_configuration("routing").get("defaults", {}), backend)


def default_pool(backend: str) -> tuple[str, ...]:
    return _rule(_configuration("routing").get("defaults", {}), backend)[1]


def _subject_rule(block: dict, kind: str, identifier: str) -> dict:
    rules = block.get(kind, {})
    if not isinstance(rules, dict):
        raise AccountError(f"routing.{kind}: must be a mapping")
    return next((value for key, value in rules.items() if str(key) == identifier), {})


def _routed(platform: str, backend: str, channel: str, user: str) -> Selection:
    routing = _configuration("routing")
    block = routing.get(platform, {})
    if not isinstance(block, dict) or set(block) - {"channels", "users"}:
        raise AccountError(f"routing.{platform}: use channels and users mappings")
    choices = (
        _subject_rule(block, "channels", channel),
        _subject_rule(block, "users", user),
        routing.get("defaults", {}),
    )
    for rule in choices:
        account, pool = _rule(rule, backend)
        if account is not None:
            return Selection(account, pool)
    return Selection(None)


def _selection_path(platform: str, session: str, backend: str) -> Path:
    from claude_on_the_fly.agent import DATA_DIR

    digest = hashlib.sha256(
        json.dumps([platform, session, str(backend)]).encode()
    ).hexdigest()
    return DATA_DIR / "state" / "accounts" / f"{digest}.json"


def _saved_record(path: Path) -> tuple[str, tuple[str, ...]] | None:
    if path.is_symlink():
        raise AccountError("Saved account selection must not be a symbolic link")
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise AccountError("Cannot read saved account selection") from exc
    if isinstance(value, str):
        return value, ()
    if not isinstance(value, dict) or set(value) != {"account", "pool"}:
        raise AccountError("Invalid saved account selection")
    name, pool = value["account"], value["pool"]
    if (
        not isinstance(name, str)
        or not isinstance(pool, list)
        or not pool
        or any(not isinstance(item, str) or not item for item in pool)
    ):
        raise AccountError("Invalid saved account selection")
    if name not in pool or len(set(pool)) != len(pool):
        raise AccountError("Invalid saved account selection")
    return name, tuple(pool)


def _saved(path: Path) -> str | None:
    record = _saved_record(path)
    return record[0] if record is not None else None


def _save(path: Path, name: str | dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(name, stream)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def saved_selection(platform: str, session: str, backend: str) -> Selection | None:
    record = _saved_record(_selection_path(platform, session, backend))
    if record is None:
        return None
    name, pool = record
    for alias in pool:
        named(alias, backend)
    return Selection(named(name, backend) if name else None, pool)


def remember(
    platform: str, session: str, backend: str, name: str, pool: tuple[str, ...]
) -> None:
    if pool and name not in pool:
        raise AccountError("Selected account is outside its fallback list")
    named(name, backend)
    record = {"account": name, "pool": list(pool)} if pool else name
    _save(_selection_path(platform, session, backend), record)


def selection(
    platform: str,
    session: str,
    backend: str,
    channel: str,
    user: str,
    *,
    persist: bool = False,
) -> Selection:
    saved = saved_selection(platform, session, backend)
    if saved is not None:
        return saved
    chosen = _routed(platform, backend, channel, user)
    if persist and chosen.account is not None:
        remember(platform, session, backend, chosen.account.name, chosen.pool)
    elif persist and (all_accounts() or _configuration("routing")):
        _save(_selection_path(platform, session, backend), "")
    return chosen


def select(
    platform: str,
    session: str,
    backend: str,
    channel: str,
    user: str,
    *,
    persist: bool = False,
) -> Account | None:
    return selection(platform, session, backend, channel, user, persist=persist).account


def apply_profile(profile: AgentProfile, account: Account | None) -> AgentProfile:
    if account is None:
        return (
            replace(profile, account="")
            if all_accounts() or _configuration("routing")
            else profile
        )
    if account.backend != profile.backend:
        raise AccountError(f"Account {account.name!r} has the wrong backend")
    base_mode, base_model = profile.account_base or (profile.mode, profile.model)
    mode = account.mode or base_mode
    model = account.model if account.model is not None else base_model
    if (mode == "ollama") != (base_mode == "ollama") and account.model is None:
        model = settings.get(f"{profile.backend.upper()}_MODEL", "")
    resolved = replace(
        profile,
        account=account.name,
        mode=mode,
        model=model,
        account_base=(base_mode, base_model),
    )
    return replace(resolved, **dict(profile.overrides))


def for_profile(profile: AgentProfile) -> Account | None:
    if isinstance(profile.account, str):
        return named(profile.account, profile.backend) if profile.account else None
    return default_for(profile.backend)


def resolve_profile(profile: AgentProfile) -> AgentProfile:
    account = for_profile(profile)
    if isinstance(profile.account, str):
        return profile
    if account is None:
        return profile
    return replace(
        apply_profile(profile, account), account_pool=default_pool(profile.backend)
    )


def prepare(workspace: Path) -> None:
    account = current()
    if account is None or account.backend != "claude":
        return
    from claude_on_the_fly import envfile, transcript

    source = (
        envfile.claude_config_dir(envfile.daemon_environment())
        / "projects"
        / transcript._workspace_to_claude_hash(workspace)
    )
    target = account.source_home / "projects" / source.name
    if target.resolve() == source.resolve():
        return
    source.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise AccountError(
            f"Account {account.name!r} already has separate history for this workspace"
        )
    target.symlink_to(source, target_is_directory=True)


def _validate_platform(block: object) -> None:
    if not isinstance(block, dict) or set(block) - {"channels", "users"}:
        raise AccountError("Account platform rules must contain channels and users")
    block = cast("dict[str, Any]", block)
    for kind in ("channels", "users"):
        rules = block.get(kind, {})
        if not isinstance(rules, dict):
            raise AccountError(f"routing.{kind}: must be a mapping")
        for rule in rules.values():
            for backend in ("claude", "codex"):
                _choice(rule, backend)


def validate() -> None:
    all_accounts()
    routes = _configuration("routing")
    if set(routes) - {"defaults", "slack", "telegram"}:
        raise AccountError("routing: use defaults, slack and telegram")
    for backend in ("claude", "codex"):
        _choice(routes.get("defaults", {}), backend)
    for platform in ("slack", "telegram"):
        _validate_platform(routes.get(platform, {}))


# A selected profile must not inherit another login from the parent process.
AUTH_OVERRIDE_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
    "ANTHROPIC_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
)
