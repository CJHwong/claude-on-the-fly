"""Online config source: clone/refresh a git repo and shallow-merge with local.

The local `symphony.yaml` may declare:

    config_source: git+https://github.com/<org>/symphony-config@main
    config_path: pm/                # optional subdir within the repo
    config_refresh_ms: 300000       # refresh cadence, default 5min, min 30s

When `config_source` is set, the daemon clones the repo on first start
(or fetches an existing cache), reads the remote `symphony.yaml` (and
optional `prompts/...` tree), shallow-merges with the local file, and
writes the merged result to a transient cache file the rest of the
daemon consumes.

Auth is delegated to git itself — HTTPS uses the user's credential
helper, SSH uses `~/.ssh`. No PAT plumbing here.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


# Local-only keys never inherited from the remote config. Everything else
# is replaceable by an entry in the local file.
LOCAL_ONLY_KEYS = frozenset({"config_source", "config_path", "config_refresh_ms"})


class RemoteConfigError(RuntimeError):
    """Anything that prevents fetch/parse/merge of the remote config."""


@dataclass(frozen=True)
class RemoteSource:
    url: str
    ref: str  # branch or tag
    subpath: str  # may be ""

    @classmethod
    def parse(cls, value: str, *, default_path: str = "") -> RemoteSource:
        """Parse a `git+<url>@<ref>` or plain `<url>@<ref>` declaration.

        Accepts:
            git+https://github.com/org/repo@main
            git+ssh://git@github.com/org/repo@develop
            https://github.com/org/repo            (ref defaults to HEAD)
        """
        raw = value.strip()
        if raw.startswith("git+"):
            raw = raw[len("git+") :]
        if not raw:
            raise RemoteConfigError("config_source is empty")
        # Last `@` separates url from ref, but only if the segment doesn't
        # contain `/` (so that user@host SSH URLs are not mis-parsed).
        ref = "HEAD"
        if "@" in raw:
            head, _, tail = raw.rpartition("@")
            if "/" not in tail and tail:
                raw = head
                ref = tail
        return cls(url=raw, ref=ref, subpath=default_path.strip("/"))


def _run_git(args: list[str], *, cwd: Path | None = None) -> str:
    """Run git, return stdout. Raise RemoteConfigError on non-zero exit."""
    if shutil.which("git") is None:
        raise RemoteConfigError("git not on PATH")
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RemoteConfigError(f"git timed out: {exc}") from exc
    if proc.returncode != 0:
        raise RemoteConfigError(
            f"git {' '.join(args[:2])}... failed (exit {proc.returncode}): "
            f"{proc.stderr.strip() or '<no stderr>'}"
        )
    return proc.stdout


class RemoteConfigCache:
    """Clones / fetches a git repo and exposes the on-disk path."""

    def __init__(self, source: RemoteSource, cache_dir: Path) -> None:
        self._source = source
        self._cache_dir = cache_dir

    @property
    def source(self) -> RemoteSource:
        return self._source

    @property
    def root_dir(self) -> Path:
        return self._cache_dir

    @property
    def working_dir(self) -> Path:
        """Effective directory after applying `config_path` subpath."""
        if self._source.subpath:
            return self._cache_dir / self._source.subpath
        return self._cache_dir

    def fetch(self) -> Path:
        """Clone or fetch + reset the repo. Returns `working_dir`.

        On any git failure, raises RemoteConfigError; the caller decides
        whether to keep using the prior cache.
        """
        if (self._cache_dir / ".git").is_dir():
            try:
                _run_git(
                    ["fetch", "--depth", "1", "origin", self._source.ref],
                    cwd=self._cache_dir,
                )
                _run_git(
                    ["reset", "--hard", "FETCH_HEAD"],
                    cwd=self._cache_dir,
                )
            except RemoteConfigError:
                # Fall back to fetching the configured ref name (covers cases
                # where the ref is a branch tracked by name rather than FETCH_HEAD).
                _run_git(["fetch", "origin"], cwd=self._cache_dir)
                _run_git(
                    ["reset", "--hard", f"origin/{self._source.ref}"],
                    cwd=self._cache_dir,
                )
        else:
            self._cache_dir.parent.mkdir(parents=True, exist_ok=True)
            if self._cache_dir.exists():
                # Stale, partial clone — wipe and retry.
                shutil.rmtree(self._cache_dir)
            clone_args = ["clone", "--depth", "1"]
            # `--branch HEAD` is rejected by git ("Remote branch HEAD not
            # found"). A ref of "HEAD" means "no ref given" → clone the remote's
            # default branch by omitting --branch.
            if self._source.ref != "HEAD":
                clone_args += ["--branch", self._source.ref]
            clone_args += [self._source.url, str(self._cache_dir)]
            _run_git(clone_args)

        working = self.working_dir
        if not working.is_dir():
            raise RemoteConfigError(f"config_path subdir not found in repo: {working}")
        return working


def shallow_merge(*, remote: dict[str, Any], local: dict[str, Any]) -> dict[str, Any]:
    """Local-overrides-only shallow merge.

    For every key in `local` (except the local-only ones):
    - If it exists in `remote`, local value replaces the remote value.
    - If it doesn't exist in `remote`, raise — local can only override
      keys the remote already declares.

    For `trackers`, the merge is one level deeper: each tracker key in
    `local.trackers` must exist in `remote.trackers`. A tracker's scalar and
    list fields are replaced by the local value; dict-valued fields
    (`instruction_by_repo`, `max_concurrent_by_state`) are merged entry-wise so
    a local override that tweaks one repo doesn't silently drop the remote's
    other entries.
    """
    out: dict[str, Any] = dict(remote)

    for key, value in local.items():
        if key in LOCAL_ONLY_KEYS:
            continue
        if key == "trackers":
            if not isinstance(value, dict):
                raise RemoteConfigError("local trackers override must be a mapping")
            remote_trackers = out.get("trackers") or {}
            if not isinstance(remote_trackers, dict):
                raise RemoteConfigError(
                    "remote trackers is not a mapping; cannot merge"
                )
            merged_trackers = dict(remote_trackers)
            for tname, override in value.items():
                if tname not in remote_trackers:
                    raise RemoteConfigError(
                        f"local override references unknown tracker '{tname}' "
                        f"(remote has: {sorted(remote_trackers)})"
                    )
                if not isinstance(override, dict):
                    raise RemoteConfigError(
                        f"local trackers.{tname} override must be a mapping"
                    )
                base = dict(remote_trackers[tname])
                for field_key, field_val in override.items():
                    existing = base.get(field_key)
                    if isinstance(field_val, dict) and isinstance(existing, dict):
                        # Entry-wise merge for maps like instruction_by_repo so
                        # the local override adds/replaces single entries instead
                        # of wiping the remote's whole map.
                        merged_field = dict(existing)
                        merged_field.update(field_val)
                        base[field_key] = merged_field
                    else:
                        base[field_key] = field_val
                merged_trackers[tname] = base
            out["trackers"] = merged_trackers
            continue
        if key not in remote:
            raise RemoteConfigError(
                f"local override references unknown key '{key}' "
                f"(remote keys: {sorted(remote)})"
            )
        out[key] = value

    return out


def load_remote_config(
    local_path: Path,
    *,
    cache_root: Path,
) -> tuple[Path | None, dict[str, Any] | None, RemoteSource | None]:
    """Resolve the remote config (if declared) for a local symphony.yaml.

    Returns `(working_dir, merged_raw, source)`. When local has no
    `config_source`, all three are None and the caller falls through to
    the existing local-only path.
    """
    try:
        local_raw = yaml.safe_load(local_path.read_text()) or {}
    except (yaml.YAMLError, OSError) as exc:
        raise RemoteConfigError(
            f"cannot read local config {local_path}: {exc}"
        ) from exc
    if not isinstance(local_raw, dict):
        raise RemoteConfigError(
            f"local config must be a mapping (got {type(local_raw).__name__})"
        )
    raw_source = local_raw.get("config_source")
    if not raw_source:
        return None, None, None

    source = RemoteSource.parse(
        str(raw_source), default_path=str(local_raw.get("config_path") or "")
    )
    cache = RemoteConfigCache(source, cache_root / _slug(source.url))
    working = cache.fetch()
    remote_path = working / local_path.name
    if not remote_path.is_file():
        raise RemoteConfigError(f"remote config not found at {remote_path}")
    try:
        remote_raw = yaml.safe_load(remote_path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise RemoteConfigError(
            f"remote config parse error ({remote_path}): {exc}"
        ) from exc
    if not isinstance(remote_raw, dict):
        raise RemoteConfigError(
            f"remote config must be a mapping (got {type(remote_raw).__name__})"
        )
    merged = shallow_merge(remote=remote_raw, local=local_raw)
    return working, merged, source


def remote_prompts_root(local_config_path: Path, *, cache_root: Path) -> Path | None:
    """Return the `prompts/` dir inside the pulled remote config cache, or None.

    Does NOT fetch — it assumes a prior `load_remote_config()` already
    populated the cache. Used by the Settings page and the daemon to discover
    instruction files contributed by the remote config repo. Returns None when
    there's no `config_source` or the prompts dir doesn't exist yet.
    """
    try:
        raw = yaml.safe_load(local_config_path.read_text()) or {}
    except (yaml.YAMLError, OSError):
        return None
    if not isinstance(raw, dict) or not raw.get("config_source"):
        return None
    source = RemoteSource.parse(
        str(raw["config_source"]), default_path=str(raw.get("config_path") or "")
    )
    cache = RemoteConfigCache(source, cache_root / _slug(source.url))
    prompts = cache.working_dir / "prompts"
    return prompts if prompts.is_dir() else None


_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _slug(text: str) -> str:
    """Filesystem-safe slug derived from a URL. Lossy on purpose."""
    out = _SLUG_RE.sub("-", text)
    return out.strip("-")[:120] or "repo"
