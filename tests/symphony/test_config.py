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
  project_key: PROJ
{extras}
"""
    )
    prompt.write_text(body)
    return cfg, prompt


@pytest.fixture
def env_creds(monkeypatch):
    """No-op fixture retained so existing test signatures stay stable. Auth
    moved to `acli auth login` and is no longer config-driven."""
    return None


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
    """base_url and project_key are still required; email/token are gone."""
    cfg = TrackerConfig.from_dict(
        {
            "base_url": "",
            "project_key": "",
        }
    )
    with pytest.raises(ValueError):
        cfg.validate()


def test_tracker_rejects_legacy_email_and_api_token():
    """Old configs that still carry email/api_token must fail loudly so
    operators know to switch to `acli auth login`."""
    with pytest.raises(ValueError, match="acli auth login"):
        TrackerConfig.from_dict(
            {
                "base_url": "https://x.atlassian.net",
                "email": "me@x.com",
                "api_token": "tok",
                "project_key": "PROJ",
            }
        )


def test_load_config_defaults(tmp_path, env_creds):
    from claude_on_the_fly.symphony.config import JiraTrackerConfig

    cfg_path, prompt_path = _write_pair(tmp_path)
    cfg = load_config(cfg_path)
    cfg.validate()
    tracker = cfg.tracker
    assert isinstance(tracker, JiraTrackerConfig)
    assert tracker.base_url == "https://x.atlassian.net"
    assert tracker.project_key == "PROJ"
    assert tracker.jql == ""  # no candidate filter by default
    assert cfg.polling_ms == 30000
    assert tracker.max_concurrent == 1  # default per-tracker budget
    assert cfg.max_turns == 20
    assert tracker.instruction == "_default"  # default instruction selection


def test_load_config_overrides(tmp_path, env_creds):
    """Legacy shape: top-level max_concurrent is hoisted into the wrapped
    Jira tracker (back-compat with old singular `tracker:` configs)."""
    cfg_path, _ = _write_pair(
        tmp_path,
        extras=("polling_ms: 5000\nmax_concurrent: 3\nmax_turns: 10\n"),
    )
    cfg = load_config(cfg_path)
    assert cfg.polling_ms == 5000
    assert cfg.tracker.max_concurrent == 3
    assert cfg.max_turns == 10


def test_load_config_top_level_max_concurrent_rejected_with_new_trackers_shape(
    tmp_path,
):
    """Under the new `trackers:` shape, top-level `max_concurrent:` is
    ambiguous and rejected — operators must put it under each tracker."""
    cfg_path = tmp_path / "symphony.yaml"
    prompt = tmp_path / "symphony-prompt.md"
    prompt.write_text("hi")
    cfg_path.write_text(
        """
trackers:
  jira:
    kind: jira
    base_url: https://x.atlassian.net
    project_key: PROJ
max_concurrent: 5
"""
    )
    with pytest.raises(ValueError, match="Top-level `max_concurrent` is no longer"):
        load_config(cfg_path)


def test_load_config_instruction_parsed(tmp_path, env_creds):
    cfg_path = tmp_path / "symphony.yaml"
    cfg_path.write_text(
        """
tracker:
  base_url: https://x.atlassian.net
  project_key: PROJ
  instruction: pm
"""
    )
    cfg = load_config(cfg_path)
    cfg.validate()
    assert cfg.tracker.instruction == "pm"


def test_load_config_rejects_legacy_prompt_key(tmp_path, env_creds):
    cfg_path = tmp_path / "symphony.yaml"
    cfg_path.write_text(
        """
tracker:
  base_url: https://x.atlassian.net
  project_key: PROJ
  prompt: ./some-prompt.md
"""
    )
    with pytest.raises(ValueError, match="tracker.prompt / tracker.prompts_dir"):
        load_config(cfg_path)


def test_load_config_polling_min(tmp_path, env_creds):
    cfg_path, _ = _write_pair(tmp_path, extras="polling_ms: 999\n")
    with pytest.raises(ValueError, match="polling_ms"):
        load_config(cfg_path)


def test_load_config_max_concurrent_min(tmp_path, env_creds):
    """Per-tracker max_concurrent must be >= 1."""
    cfg_path, _ = _write_pair(tmp_path, extras="max_concurrent: 0\n")
    cfg = load_config(cfg_path)
    with pytest.raises(ValueError, match="tracker.max_concurrent must be >= 1"):
        cfg.validate()


def test_load_config_max_concurrent_non_numeric(tmp_path, env_creds):
    """A non-numeric max_concurrent fails with a clean message, not a raw
    int() ValueError."""
    cfg_path, _ = _write_pair(tmp_path, extras="max_concurrent: two\n")
    with pytest.raises(ValueError, match="max_concurrent must be an integer"):
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
            "project_key": "P",
        }
    )
    assert cfg.kind == "jira"


def test_tracker_kind_normalizes_case():
    cfg = TrackerConfig.from_dict(
        {
            "kind": "JIRA",
            "base_url": "https://x.atlassian.net",
            "project_key": "P",
        }
    )
    assert cfg.kind == "jira"


def test_load_config_missing_instruction_file_is_not_a_validate_error(
    tmp_path, env_creds
):
    """The instruction file is resolved at runtime (local + remote dirs), so a
    missing file is NOT a config-validation error — the daemon falls back to
    the built-in prompt and logs a warning."""
    cfg_path = tmp_path / "symphony.yaml"
    cfg_path.write_text(
        """
tracker:
  base_url: https://x.atlassian.net
  project_key: PROJ
  instruction: nonexistent
"""
    )
    cfg = load_config(cfg_path)
    cfg.validate()  # must not raise
    assert cfg.tracker.instruction == "nonexistent"


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
            "project_key": "PROJ",
        }
    )
    with pytest.raises(ValueError, match="tracker.base_url is required"):
        cfg.validate()


def test_tracker_validate_project_key_required() -> None:
    cfg = TrackerConfig.from_dict(
        {
            "base_url": "https://x.atlassian.net",
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
    cfg_path.write_text(
        """
trackers:
  jira:
    base_url: https://x.atlassian.net
    project_key: PROJ
    instruction: rnd
  github:
    instruction: qa
polling_ms: 5000
"""
    )
    from claude_on_the_fly.symphony.config import GitHubTrackerConfig

    cfg = load_config(cfg_path)
    cfg.validate()
    assert set(cfg.trackers) == {"jira", "github"}
    jira = cfg.trackers["jira"]
    gh = cfg.trackers["github"]
    assert isinstance(gh, GitHubTrackerConfig)  # narrows for search_query below
    assert jira.kind == "jira"  # inferred from the key
    assert jira.instruction == "rnd"
    assert gh.kind == "github"  # inferred from the key
    assert gh.search_query == "is:pr is:open -is:draft user-review-requested:@me"
    assert gh.instruction == "qa"
    assert cfg.polling_ms == 5000


def test_kind_inferred_from_key_no_explicit_kind(tmp_path, env_creds):
    """A stanza with no `kind:` takes its kind from the key name."""
    cfg_path = tmp_path / "symphony.yaml"
    cfg_path.write_text(
        """
trackers:
  github:
    search_query: is:pr is:open
"""
    )
    cfg = load_config(cfg_path)
    cfg.validate()
    assert cfg.trackers["github"].kind == "github"


def test_load_config_legacy_singular_tracker_form_is_wrapped(tmp_path, env_creds):
    """Old shape with `tracker:` is auto-wrapped into the new `trackers:` map."""
    cfg_path, _ = _write_pair(tmp_path)
    cfg = load_config(cfg_path)
    cfg.validate()
    # The wrapped tracker is keyed by its `kind`.
    assert set(cfg.trackers) == {"jira"}
    assert cfg.tracker.kind == "jira"


def test_load_config_rejects_gate_label(tmp_path, env_creds):
    cfg_path = tmp_path / "symphony.yaml"
    cfg_path.write_text(
        """
tracker:
  base_url: https://x.atlassian.net
  project_key: PROJ
  gate_label: stevedore
"""
    )
    with pytest.raises(ValueError, match="tracker.gate_label is no longer"):
        load_config(cfg_path)


def test_load_config_unsupported_kind_raises(tmp_path, env_creds):
    cfg_path = tmp_path / "symphony.yaml"
    cfg_path.write_text("trackers:\n  bogus:\n    kind: linear\n")
    with pytest.raises(ValueError, match="tracker.kind='linear' unsupported"):
        load_config(cfg_path)


def test_github_tracker_config_defaults_no_auth_fields_needed():
    from claude_on_the_fly.symphony.config import GitHubTrackerConfig

    cfg = GitHubTrackerConfig.from_dict({"kind": "github"})
    assert cfg.kind == "github"
    assert cfg.search_query == "is:pr is:open -is:draft user-review-requested:@me"


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


def test_dump_effective_config_renders_yaml():
    from claude_on_the_fly.symphony.config import (
        GitHubTrackerConfig,
        JiraTrackerConfig,
        SymphonyConfig,
        dump_effective_config,
    )

    cfg = SymphonyConfig(
        trackers={
            "jira": JiraTrackerConfig(
                kind="jira",
                base_url="https://x.atlassian.net",
                project_key="ACES",
                jql='status = "In Progress"',
                instruction="_default",
            ),
            "github": GitHubTrackerConfig(
                kind="github", instruction="qa", max_concurrent=5
            ),
        }
    )
    out = dump_effective_config(cfg)
    # Readable, sorted YAML with the per-tracker fields present.
    assert "trackers:" in out
    assert "project_key: ACES" in out
    assert "instruction: _default" in out
    assert "instruction: qa" in out
    assert "polling_ms:" in out
    # `kind` is inferred from the stanza key — don't echo it back.
    assert "kind:" not in out


def test_dump_effective_config_keeps_overridden_kind():
    """A stanza whose kind differs from its key is a real override — keep it."""
    from claude_on_the_fly.symphony.config import (
        JiraTrackerConfig,
        SymphonyConfig,
        dump_effective_config,
    )

    cfg = SymphonyConfig(
        trackers={
            "secondary": JiraTrackerConfig(
                kind="jira",
                base_url="https://x.atlassian.net",
                project_key="ACES",
            )
        }
    )
    out = dump_effective_config(cfg)
    assert "kind: jira" in out
