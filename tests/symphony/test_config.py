"""Config parsing, $VAR resolution, end-to-end load_config."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_on_the_fly.symphony.config import (
    TrackerConfig,
    expand_path,
    load_config,
    resolve_env,
)


def _write_pair(tmp_path, *, body: str = "hi", extras: str = "") -> tuple:
    cfg = tmp_path / "symphony.yaml"
    prompt = tmp_path / "symphony-prompt.md"
    cfg.write_text(
        f"""
tracker:
  base_url: https://x.atlassian.net
  email: $J_EMAIL
  api_token: $J_TOK
  project_key: PROJ
{extras}
"""
    )
    prompt.write_text(body)
    return cfg, prompt


@pytest.fixture
def env_creds(monkeypatch):
    monkeypatch.setenv("J_EMAIL", "me@x.com")
    monkeypatch.setenv("J_TOK", "tok")


def test_resolve_env_passthrough():
    assert resolve_env("plain") == "plain"
    assert resolve_env("https://example.com") == "https://example.com"
    assert resolve_env(123) == 123
    assert resolve_env(None) is None


def test_resolve_env_var_set(monkeypatch):
    monkeypatch.setenv("FOO", "bar")
    assert resolve_env("$FOO") == "bar"


def test_resolve_env_var_unset(monkeypatch):
    monkeypatch.delenv("MISSING_XYZ", raising=False)
    assert resolve_env("$MISSING_XYZ") == ""


def test_resolve_env_only_literal_var(monkeypatch):
    monkeypatch.setenv("FOO", "bar")
    assert resolve_env("prefix-$FOO") == "prefix-$FOO"
    assert resolve_env("$FOO/path") == "$FOO/path"


def test_expand_path_tilde(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    p = expand_path("~/foo")
    assert p == (tmp_path / "foo").resolve()


def test_tracker_missing_required():
    cfg = TrackerConfig.from_dict(
        {
            "base_url": "https://x.atlassian.net",
            "email": "",
            "api_token": "",
            "project_key": "PROJ",
        }
    )
    with pytest.raises(ValueError):
        cfg.validate()


def test_load_config_defaults(tmp_path, env_creds):
    from claude_on_the_fly.symphony.config import JiraTrackerConfig

    cfg_path, prompt_path = _write_pair(tmp_path)
    cfg = load_config(cfg_path)
    cfg.validate()
    tracker = cfg.tracker
    assert isinstance(tracker, JiraTrackerConfig)
    assert tracker.email == "me@x.com"
    assert tracker.api_token == "tok"
    assert tracker.project_key == "PROJ"
    assert tracker.active_states == ("To Do", "In Progress")
    assert cfg.polling_ms == 30000
    assert cfg.max_concurrent == 1
    assert cfg.max_turns == 20
    assert tracker.prompt_path == prompt_path.resolve()


def test_load_config_overrides(tmp_path, env_creds):
    cfg_path, _ = _write_pair(
        tmp_path,
        extras=(
            "polling_ms: 5000\n"
            "max_concurrent: 3\n"
            "max_turns: 10\n"
            "gate_label: stevedore\n"
        ),
    )
    cfg = load_config(cfg_path)
    assert cfg.polling_ms == 5000
    assert cfg.max_concurrent == 3
    assert cfg.max_turns == 10
    assert cfg.tracker.gate_label == "stevedore"


def test_load_config_explicit_prompt_path(tmp_path, env_creds):
    custom_prompt = tmp_path / "custom.md"
    custom_prompt.write_text("custom")
    cfg_path, _ = _write_pair(tmp_path, extras=f"prompt: {custom_prompt}\n")
    cfg = load_config(cfg_path)
    cfg.validate()
    assert cfg.tracker.prompt_path == custom_prompt.resolve()


def test_load_config_polling_min(tmp_path, env_creds):
    cfg_path, _ = _write_pair(tmp_path, extras="polling_ms: 999\n")
    with pytest.raises(ValueError, match="polling_ms"):
        load_config(cfg_path)


def test_load_config_max_concurrent_min(tmp_path, env_creds):
    cfg_path, _ = _write_pair(tmp_path, extras="max_concurrent: 0\n")
    with pytest.raises(ValueError, match="max_concurrent"):
        load_config(cfg_path)


def test_load_config_max_turns_unlimited(tmp_path, env_creds):
    """max_turns: -1 means unlimited turns per worker session."""
    cfg_path, _ = _write_pair(tmp_path, extras="max_turns: -1\n")
    cfg = load_config(cfg_path)
    assert cfg.max_turns == -1


def test_load_config_max_turns_zero_invalid(tmp_path, env_creds):
    cfg_path, _ = _write_pair(tmp_path, extras="max_turns: 0\n")
    with pytest.raises(ValueError, match="max_turns"):
        load_config(cfg_path)


def test_load_config_max_turns_lt_negative_one_invalid(tmp_path, env_creds):
    cfg_path, _ = _write_pair(tmp_path, extras="max_turns: -2\n")
    with pytest.raises(ValueError, match="max_turns"):
        load_config(cfg_path)


def test_load_config_per_state_concurrency_lowercases_keys(tmp_path, env_creds):
    cfg_path, _ = _write_pair(
        tmp_path,
        extras=('max_concurrent_by_state:\n  Rework: 1\n  "In Progress": 5\n'),
    )
    cfg = load_config(cfg_path)
    assert cfg.tracker.max_concurrent_by_state == {"rework": 1, "in progress": 5}


def test_load_config_per_state_drops_invalid_entries(tmp_path, env_creds):
    cfg_path, _ = _write_pair(
        tmp_path,
        extras=(
            "max_concurrent_by_state:\n"
            "  Building: 3\n"
            "  Bad: 0\n"
            "  AlsoBad: -1\n"
            "  Garbage: abc\n"
        ),
    )
    cfg = load_config(cfg_path)
    assert cfg.tracker.max_concurrent_by_state == {"building": 3}


def test_load_config_per_state_must_be_mapping(tmp_path, env_creds):
    cfg_path, _ = _write_pair(tmp_path, extras="max_concurrent_by_state: not-a-map\n")
    with pytest.raises(ValueError, match="max_concurrent_by_state"):
        load_config(cfg_path)


def test_tracker_kind_defaults_to_jira():
    cfg = TrackerConfig.from_dict(
        {
            "base_url": "https://x.atlassian.net",
            "email": "a@b",
            "api_token": "t",
            "project_key": "P",
        }
    )
    assert cfg.kind == "jira"


def test_tracker_kind_normalizes_case():
    cfg = TrackerConfig.from_dict(
        {
            "kind": "JIRA",
            "base_url": "https://x.atlassian.net",
            "email": "a@b",
            "api_token": "t",
            "project_key": "P",
        }
    )
    assert cfg.kind == "jira"


def test_load_config_missing_prompt_file(tmp_path, env_creds):
    cfg_path, prompt_path = _write_pair(tmp_path)
    prompt_path.unlink()
    cfg = load_config(cfg_path)
    with pytest.raises(ValueError, match="prompt file not found"):
        cfg.validate()


def test_load_config_invalid_yaml(tmp_path):
    cfg_path = tmp_path / "symphony.yaml"
    cfg_path.write_text("this: is: invalid: too: many: colons:")
    with pytest.raises((ValueError, Exception)):
        load_config(cfg_path)


def test_load_config_not_a_mapping(tmp_path):
    cfg_path = tmp_path / "symphony.yaml"
    cfg_path.write_text("- just\n- a\n- list\n")
    with pytest.raises(ValueError, match="mapping"):
        load_config(cfg_path)


def test_expand_path_empty_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """When env var is empty string, resolve_env returns '' and expand_path returns None."""
    monkeypatch.setenv("EMPTY_VAR", "")
    result = expand_path("$EMPTY_VAR")
    assert result is None


def test_expand_path_relative_with_base(tmp_path: Path) -> None:
    """Relative path resolved against base."""
    base = tmp_path / "base"
    result = expand_path("foo/bar", base=base)
    assert result == (base / "foo/bar").resolve()


def test_tracker_validate_base_url_required() -> None:
    cfg = TrackerConfig.from_dict(
        {
            "base_url": "",
            "email": "e@x.com",
            "api_token": "tok",
            "project_key": "PROJ",
        }
    )
    with pytest.raises(ValueError, match="tracker.base_url is required"):
        cfg.validate()


def test_tracker_validate_api_token_required() -> None:
    cfg = TrackerConfig.from_dict(
        {
            "base_url": "https://x.atlassian.net",
            "email": "e@x.com",
            "api_token": "",
            "project_key": "PROJ",
        }
    )
    with pytest.raises(ValueError, match="tracker.api_token is required"):
        cfg.validate()


def test_tracker_validate_project_key_required() -> None:
    cfg = TrackerConfig.from_dict(
        {
            "base_url": "https://x.atlassian.net",
            "email": "e@x.com",
            "api_token": "tok",
            "project_key": "",
        }
    )
    with pytest.raises(ValueError, match="tracker.project_key is required"):
        cfg.validate()


def test_max_retry_backoff_ms_validation(tmp_path, env_creds) -> None:
    cfg_path, _ = _write_pair(tmp_path, extras="max_retry_backoff_ms: 500\n")
    with pytest.raises(ValueError, match="max_retry_backoff_ms must be >= 1000"):
        load_config(cfg_path)


def test_symphony_config_from_dict_non_dict() -> None:
    from pathlib import Path as _Path

    from claude_on_the_fly.symphony.config import SymphonyConfig

    with pytest.raises(ValueError, match="config must decode to a YAML mapping"):
        SymphonyConfig.from_dict("not a dict", base=_Path("/tmp"))  # type: ignore[arg-type]


def test_load_config_missing_file(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="config not found"):
        load_config(tmp_path / "nonexistent.yaml")


# ---------------------------------------------------------------------------
# Multi-tracker shape + backward compat
# ---------------------------------------------------------------------------


def test_load_config_new_multi_tracker_shape(tmp_path, env_creds):
    """`trackers:` map with both jira and github stanzas constructs a
    SymphonyConfig with one entry per source."""
    cfg_path = tmp_path / "symphony.yaml"
    jira_prompt = tmp_path / "symphony-prompt-jira.md"
    gh_prompt = tmp_path / "symphony-prompt-github.md"
    jira_prompt.write_text("jira prompt")
    gh_prompt.write_text("github prompt")
    cfg_path.write_text(
        f"""
trackers:
  jira:
    kind: jira
    base_url: https://x.atlassian.net
    email: $J_EMAIL
    api_token: $J_TOK
    project_key: PROJ
    gate_label: stevedore
    prompt: {jira_prompt}
  github:
    kind: github
    prompt: {gh_prompt}
polling_ms: 5000
"""
    )
    cfg = load_config(cfg_path)
    cfg.validate()
    assert set(cfg.trackers) == {"jira", "github"}
    jira = cfg.trackers["jira"]
    gh = cfg.trackers["github"]
    assert jira.kind == "jira"
    assert jira.gate_label == "stevedore"
    assert jira.prompt_path == jira_prompt.resolve()
    assert gh.kind == "github"
    assert gh.gate_label is None  # no gate label for github
    assert gh.active_states == ("open",)
    assert gh.terminal_states == ("closed", "merged")
    assert gh.prompt_path == gh_prompt.resolve()
    assert cfg.polling_ms == 5000


def test_load_config_legacy_singular_tracker_form_is_wrapped(tmp_path, env_creds):
    """Old shape with `tracker:` + top-level prompt/gate_label is auto-wrapped
    into the new `trackers:` map, hoisting those fields into the wrapped
    tracker so existing configs keep working."""
    cfg_path, _ = _write_pair(tmp_path, extras="gate_label: stevedore\n")
    cfg = load_config(cfg_path)
    cfg.validate()
    # The wrapped tracker is keyed by its `kind`.
    assert set(cfg.trackers) == {"jira"}
    jira = cfg.trackers["jira"]
    assert jira.gate_label == "stevedore"
    # Singular alias and convenience properties still work.
    assert cfg.tracker is jira
    assert cfg.tracker.gate_label == "stevedore"


def test_load_config_unsupported_kind_raises(tmp_path, env_creds):
    cfg_path = tmp_path / "symphony.yaml"
    cfg_path.write_text("trackers:\n  bogus:\n    kind: linear\n")
    with pytest.raises(ValueError, match="tracker.kind='linear' unsupported"):
        load_config(cfg_path)


def test_github_tracker_config_defaults_no_auth_fields_needed():
    from claude_on_the_fly.symphony.config import GitHubTrackerConfig

    cfg = GitHubTrackerConfig.from_dict({"kind": "github"})
    assert cfg.kind == "github"
    assert cfg.active_states == ("open",)
    assert cfg.terminal_states == ("closed", "merged")
    assert cfg.gate_label is None


def test_github_tracker_config_search_query_default():
    """Default search_query matches the current hardcoded value."""
    from claude_on_the_fly.symphony.config import GitHubTrackerConfig

    cfg = GitHubTrackerConfig.from_dict({"kind": "github"})
    assert cfg.search_query == "is:pr is:open -is:draft user-review-requested:@me"


def test_github_tracker_config_search_query_custom():
    """Custom search_query is parsed from config dict."""
    from claude_on_the_fly.symphony.config import GitHubTrackerConfig

    cfg = GitHubTrackerConfig.from_dict(
        {"kind": "github", "search_query": "is:pr is:open org:gofreight"}
    )
    assert cfg.search_query == "is:pr is:open org:gofreight"


def test_github_tracker_config_search_query_empty_falls_back():
    """Empty string falls back to the default query."""
    from claude_on_the_fly.symphony.config import GitHubTrackerConfig

    cfg = GitHubTrackerConfig.from_dict({"kind": "github", "search_query": ""})
    assert cfg.search_query == "is:pr is:open -is:draft user-review-requested:@me"


def test_trackers_must_be_mapping(tmp_path, env_creds):
    cfg_path = tmp_path / "symphony.yaml"
    cfg_path.write_text("trackers: not-a-map\n")
    with pytest.raises(ValueError, match="`trackers` must be a mapping"):
        load_config(cfg_path)
