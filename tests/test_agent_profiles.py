"""agent profiles: how one is resolved, the cron key that names one, and the
job path that carries it from a fired entry to the argv the CLI is spawned with.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from claude_on_the_fly import agent, cron, preflight
from claude_on_the_fly.agent import Response
from claude_on_the_fly.backends.claude import ClaudeBackend
from claude_on_the_fly.backends.codex import CodexBackend
from claude_on_the_fly.jobs.agent_runner import OrchestratorAgentRunner
from claude_on_the_fly.jobs.core import Job
from claude_on_the_fly.jobs.file_queue import FileInboxQueue
from claude_on_the_fly.jobs.key_state import KeyStateStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Every key the resolver consults. Cleared per test so the developer's own
# environment cannot decide what a test proves — several of these are set on a
# real workstation, and `settings.get` puts the environment above the file.
_AGENT_ENV = (
    "AGENT_BACKEND",
    "CLAUDE_MODE",
    "CLAUDE_MODEL",
    "CLAUDE_EFFORT",
    "CODEX_MODE",
    "CODEX_MODEL",
    "CODEX_EFFORT",
    "OLLAMA_MODEL",
    "OLLAMA_EFFORT",
    "OLLAMA_CONTEXT_WINDOW",
)


@pytest.fixture
def clean_agent_env(monkeypatch):
    for name in _AGENT_ENV:
        monkeypatch.delenv(name, raising=False)


def write_agent_config(path: Path, agent_section: dict) -> None:
    path.write_text(yaml.safe_dump({"agent": agent_section}), encoding="utf-8")


def cron_config(tmp_path: Path, *entries: dict) -> Path:
    path = tmp_path / "cron.yaml"
    path.write_text(yaml.safe_dump({"entries": list(entries)}), encoding="utf-8")
    return path


class FakeQueue:
    """Collects what the producer enqueued. No filesystem."""

    def __init__(self) -> None:
        self.jobs: list[Job] = []

    def enqueue(self, job: Job) -> None:
        self.jobs.append(job)

    def count_unfinished(self, entry: str, item: str | None = None) -> int:
        return 0

    # Unused by the producer, present so the shape matches the port.
    def claim(self): ...
    def complete(self, job, result): ...
    def mark_delivered(self, job_id): ...
    def undelivered(self):
        return []

    def list_unfinished(self, limit):
        return []

    def recover_stale(self, ttl_s):
        return 0


# ---------------------------------------------------------------------------
# Resolution with no profile: the contract every existing deployment relies on
# ---------------------------------------------------------------------------


class TestResolveTheGlobalConfig:
    """`resolve_profile(None)` has to reproduce the old reads exactly. A daemon
    that never defines a profile must not notice this refactor happened."""

    def test_nothing_set_is_claude_native_with_no_model(self, clean_agent_env) -> None:
        profile = agent.resolve_profile()
        assert profile.backend == "claude"
        assert profile.mode == "native"
        assert profile.model == ""
        assert profile.effort == ""

    def test_claude_model_and_effort_come_from_their_own_keys(
        self, clean_agent_env, monkeypatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_MODEL", "opus")
        monkeypatch.setenv("CLAUDE_EFFORT", "xhigh")
        profile = agent.resolve_profile()
        assert (profile.model, profile.effort) == ("opus", "xhigh")

    def test_codex_reads_the_codex_keys(self, clean_agent_env, monkeypatch) -> None:
        monkeypatch.setenv("AGENT_BACKEND", "codex")
        monkeypatch.setenv("CODEX_MODEL", "gpt-5")
        monkeypatch.setenv("CODEX_EFFORT", "high")
        profile = agent.resolve_profile()
        assert profile.backend == "codex"
        assert (profile.model, profile.effort) == ("gpt-5", "high")

    def test_a_backend_is_case_insensitive(self, clean_agent_env, monkeypatch) -> None:
        monkeypatch.setenv("AGENT_BACKEND", "CODEX")
        assert agent.resolve_profile().backend == "codex"

    def test_surrounding_whitespace_is_stripped(
        self, clean_agent_env, monkeypatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_MODEL", "  opus  ")
        monkeypatch.setenv("CLAUDE_EFFORT", " high ")
        profile = agent.resolve_profile()
        assert (profile.model, profile.effort) == ("opus", "high")

    def test_ollama_mode_reads_the_ollama_keys_instead(
        self, clean_agent_env, monkeypatch
    ) -> None:
        """The launcher chose the model, so the launcher's keys decide. The
        backend's own model and effort are not consulted, and not merged."""
        monkeypatch.setenv("CLAUDE_MODE", "ollama")
        monkeypatch.setenv("CLAUDE_MODEL", "opus")
        monkeypatch.setenv("CLAUDE_EFFORT", "low")
        monkeypatch.setenv("OLLAMA_MODEL", "qwen3:30b")
        monkeypatch.setenv("OLLAMA_EFFORT", "max")
        monkeypatch.setenv("OLLAMA_CONTEXT_WINDOW", "200000")
        profile = agent.resolve_profile()
        assert profile.mode == "ollama"
        assert profile.model == "qwen3:30b"
        assert profile.effort == "max"
        assert profile.ollama_context_window == 200000

    def test_codex_ollama_reads_the_same_shared_keys(
        self, clean_agent_env, monkeypatch
    ) -> None:
        monkeypatch.setenv("AGENT_BACKEND", "codex")
        monkeypatch.setenv("CODEX_MODE", "ollama")
        monkeypatch.setenv("CODEX_EFFORT", "minimal")
        monkeypatch.setenv("OLLAMA_MODEL", "qwen3:30b")
        monkeypatch.setenv("OLLAMA_EFFORT", "low")
        profile = agent.resolve_profile()
        assert (profile.model, profile.effort) == ("qwen3:30b", "low")

    def test_the_ollama_effort_does_not_leak_into_native_mode(
        self, clean_agent_env, monkeypatch
    ) -> None:
        """`ollama.effort` belongs to the mode that swapped the model out. In
        native mode the CLI is the operator's own, and it has a key of its own."""
        monkeypatch.setenv("OLLAMA_EFFORT", "max")
        assert agent.resolve_profile().effort == ""

    def test_an_unset_ollama_effort_stays_empty(
        self, clean_agent_env, monkeypatch
    ) -> None:
        """Empty means inherit: no flag is passed and the CLI reads its own
        config, exactly as it did before the key existed."""
        monkeypatch.setenv("CLAUDE_MODE", "ollama")
        monkeypatch.setenv("OLLAMA_MODEL", "qwen3:30b")
        assert agent.resolve_profile().effort == ""

    def test_the_context_window_is_ollama_only(
        self, clean_agent_env, monkeypatch
    ) -> None:
        """Native and pty derive a real reading from the CLI. Carrying a stated
        one there would let it override the measurement."""
        monkeypatch.setenv("OLLAMA_CONTEXT_WINDOW", "200000")
        for mode in ("native", "pty"):
            monkeypatch.setenv("CLAUDE_MODE", mode)
            assert agent.resolve_profile().ollama_context_window is None

    def test_an_unusable_context_window_is_dropped_not_fatal(
        self, clean_agent_env, monkeypatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_MODE", "ollama")
        monkeypatch.setenv("OLLAMA_MODEL", "qwen3:30b")
        monkeypatch.setenv("OLLAMA_CONTEXT_WINDOW", "not-a-number")
        assert agent.resolve_profile().ollama_context_window is None

    def test_ollama_without_a_model_is_refused(
        self, clean_agent_env, monkeypatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_MODE", "ollama")
        with pytest.raises(ValueError, match="requires OLLAMA_MODEL"):
            agent.resolve_profile()

    def test_an_unknown_backend_is_refused(self, clean_agent_env, monkeypatch) -> None:
        monkeypatch.setenv("AGENT_BACKEND", "gemini")
        with pytest.raises(ValueError, match="Unknown AGENT_BACKEND"):
            agent.resolve_profile()

    def test_an_unknown_mode_is_refused(self, clean_agent_env, monkeypatch) -> None:
        monkeypatch.setenv("CLAUDE_MODE", "telepathy")
        with pytest.raises(ValueError, match="Unknown CLAUDE_MODE"):
            agent.resolve_profile()


# ---------------------------------------------------------------------------
# Overlay
# ---------------------------------------------------------------------------


class TestProfileOverlay:
    def test_a_profile_overrides_only_the_keys_it_names(
        self, clean_agent_env, operator_settings, monkeypatch
    ) -> None:
        monkeypatch.setenv("AGENT_BACKEND", "codex")
        write_agent_config(
            operator_settings,
            {"profiles": {"cheap": {"codex": {"model": "gpt-5-mini"}}}},
        )
        monkeypatch.setenv("CODEX_EFFORT", "high")
        profile = agent.resolve_profile("cheap")
        assert profile.model == "gpt-5-mini"
        assert profile.effort == "high", "an unnamed key inherits the global value"
        assert profile.backend == "codex"
        assert profile.mode == "native"

    def test_a_profile_beats_the_environment(
        self, clean_agent_env, operator_settings, monkeypatch
    ) -> None:
        """`settings` puts the environment above config.yaml so a deployment
        cannot lose a daemon-wide default to a file nobody edited. A profile is
        neither daemon-wide nor a default: the operator named it on this entry."""
        monkeypatch.setenv("AGENT_BACKEND", "codex")
        monkeypatch.setenv("CODEX_MODEL", "gpt-5")
        write_agent_config(
            operator_settings,
            {"profiles": {"cheap": {"codex": {"model": "gpt-5-mini"}}}},
        )
        assert agent.resolve_profile("cheap").model == "gpt-5-mini"
        assert agent.resolve_profile().model == "gpt-5", "the global still reads env"

    def test_a_profile_can_switch_the_backend(
        self, clean_agent_env, operator_settings, monkeypatch
    ) -> None:
        monkeypatch.setenv("AGENT_BACKEND", "codex")
        monkeypatch.setenv("CLAUDE_MODEL", "opus")
        write_agent_config(
            operator_settings, {"profiles": {"deep": {"backend": "claude"}}}
        )
        profile = agent.resolve_profile("deep")
        assert profile.backend == "claude"
        assert profile.model == "opus", "the new backend's own keys now apply"

    def test_a_profile_can_switch_to_ollama_and_reroute(
        self, clean_agent_env, operator_settings, monkeypatch
    ) -> None:
        """The reason a profile carries `mode` at all: mode decides which keys
        own the model and the effort, so a profile that set only `model` while
        the mode flipped underneath it would be silently ignored."""
        monkeypatch.setenv("AGENT_BACKEND", "codex")
        monkeypatch.setenv("CODEX_MODEL", "gpt-5")
        write_agent_config(
            operator_settings,
            {
                "profiles": {
                    "local": {
                        "codex": {"mode": "ollama"},
                        "ollama": {"model": "qwen3:30b", "effort": "low"},
                    }
                }
            },
        )
        profile = agent.resolve_profile("local")
        assert profile.mode == "ollama"
        assert (profile.model, profile.effort) == ("qwen3:30b", "low")

    def test_an_ollama_profile_inherits_the_global_ollama_model(
        self, clean_agent_env, operator_settings, monkeypatch
    ) -> None:
        monkeypatch.setenv("OLLAMA_MODEL", "qwen3:30b")
        write_agent_config(
            operator_settings,
            {"profiles": {"local": {"claude": {"mode": "ollama"}}}},
        )
        assert agent.resolve_profile("local").model == "qwen3:30b"

    def test_an_unknown_profile_names_the_ones_that_exist(
        self, clean_agent_env, operator_settings
    ) -> None:
        """Falling back to the global config would run the entry on the model it
        was explicitly moved off, and nothing would say so."""
        write_agent_config(operator_settings, {"profiles": {"cheap": {}, "deep": {}}})
        with pytest.raises(ValueError, match=r"Unknown agent profile: 'chaep'"):
            agent.resolve_profile("chaep")
        with pytest.raises(ValueError, match="cheap, deep"):
            agent.resolve_profile("chaep")

    def test_naming_a_profile_with_none_defined_is_refused(
        self, clean_agent_env, operator_settings
    ) -> None:
        with pytest.raises(ValueError, match="no profiles are defined"):
            agent.resolve_profile("cheap")

    def test_a_profile_that_is_not_a_mapping_is_refused(
        self, clean_agent_env, operator_settings
    ) -> None:
        write_agent_config(operator_settings, {"profiles": {"cheap": "gpt-5-mini"}})
        with pytest.raises(
            ValueError, match=r"agent\.profiles\.cheap: must be a mapping"
        ):
            agent.resolve_profile("cheap")

    def test_a_non_string_value_names_its_own_path(
        self, clean_agent_env, operator_settings, monkeypatch
    ) -> None:
        monkeypatch.setenv("AGENT_BACKEND", "codex")
        write_agent_config(
            operator_settings, {"profiles": {"cheap": {"codex": {"model": 5}}}}
        )
        with pytest.raises(ValueError, match=r"agent\.profiles\.cheap\.codex\.model"):
            agent.resolve_profile("cheap")

    def test_a_block_for_the_other_backend_is_inert(
        self, clean_agent_env, operator_settings, monkeypatch
    ) -> None:
        """A profile is an overlay on the same key space, so a `codex:` block
        under a claude backend does nothing -- exactly as the global
        `agent.codex.model` does nothing there. Switch `backend` too, or put the
        override under the backend that is actually running."""
        monkeypatch.setenv("CLAUDE_MODEL", "opus")
        write_agent_config(
            operator_settings,
            {"profiles": {"cheap": {"codex": {"model": "gpt-5-mini"}}}},
        )
        profile = agent.resolve_profile("cheap")
        assert profile.backend == "claude"
        assert profile.model == "opus"

    def test_profile_names_lists_them_sorted(
        self, clean_agent_env, operator_settings
    ) -> None:
        write_agent_config(operator_settings, {"profiles": {"deep": {}, "cheap": {}}})
        assert agent.profile_names() == ["cheap", "deep"]

    def test_profile_names_is_empty_when_none_are_configured(
        self, clean_agent_env, operator_settings
    ) -> None:
        assert agent.profile_names() == []

    def test_profile_names_ignores_a_malformed_block(
        self, clean_agent_env, operator_settings
    ) -> None:
        write_agent_config(operator_settings, {"profiles": "cheap"})
        assert agent.profile_names() == []


# ---------------------------------------------------------------------------
# Session identity
# ---------------------------------------------------------------------------


class TestSessionKey:
    """`.key` seeds a uuid5, so it is a wire format. Changing how it renders
    would restart every session an earlier build wrote."""

    def test_claude_with_no_model_renders_an_empty_slot(self) -> None:
        profile = agent.AgentProfile("claude", "native", "", "")
        assert profile.key == "claude:native:"

    def test_codex_with_no_model_renders_default(self) -> None:
        profile = agent.AgentProfile("codex", "native", "", "")
        assert profile.key == "codex:native:default"

    def test_the_model_is_part_of_the_identity(self) -> None:
        cheap = agent.AgentProfile("codex", "native", "gpt-5-mini", "low")
        deep = agent.AgentProfile("codex", "native", "gpt-5", "high")
        assert cheap.key != deep.key

    def test_effort_alone_does_not_change_the_identity(self) -> None:
        """Effort is not in the key, so raising it on a keyed entry keeps the
        transcript. Changing the model does not, which is the documented cost."""
        low = agent.AgentProfile("codex", "native", "gpt-5", "low")
        high = agent.AgentProfile("codex", "native", "gpt-5", "high")
        assert low.key == high.key

    def test_current_backend_key_reads_the_profile_it_is_given(self) -> None:
        profile = agent.AgentProfile("codex", "native", "gpt-5-mini", "low")
        assert agent.current_backend_key(profile) == "codex:native:gpt-5-mini"


# ---------------------------------------------------------------------------
# The backend the profile builds
# ---------------------------------------------------------------------------


class TestGetBackendTakesTheProfile:
    def test_a_native_profile_binds_the_model_and_effort(self) -> None:
        backend = agent.get_backend(
            agent.AgentProfile("codex", "native", "gpt-5-mini", "low")
        )
        assert isinstance(backend, CodexBackend)
        assert (backend.model, backend.effort) == ("gpt-5-mini", "low")

    def test_a_claude_profile_builds_a_claude_backend(self) -> None:
        backend = agent.get_backend(
            agent.AgentProfile("claude", "native", "opus", "xhigh")
        )
        assert isinstance(backend, ClaudeBackend)
        assert (backend.model, backend.effort) == ("opus", "xhigh")

    def test_an_ollama_profile_pins_the_model_on_the_launcher_only(self) -> None:
        """Passing it twice would put `--model` on the claude argv that ollama
        is already wrapping, where it becomes a dead flag."""
        backend = agent.get_backend(
            agent.AgentProfile("claude", "ollama", "qwen3:30b", "low", 200000)
        )
        assert isinstance(backend, ClaudeBackend)
        assert backend.launcher is not None
        assert backend.launcher.model == "qwen3:30b"
        assert backend.model == ""
        assert backend.effort == "low"
        assert backend.ollama_context_window == 200000

    def test_a_codex_ollama_profile_pins_it_the_same_way(self) -> None:
        backend = agent.get_backend(
            agent.AgentProfile("codex", "ollama", "qwen3:30b", "low")
        )
        assert isinstance(backend, CodexBackend)
        assert backend.launcher is not None
        assert backend.model == ""

    def test_a_pty_profile_selects_the_interactive_interface(self) -> None:
        backend = agent.get_backend(agent.AgentProfile("codex", "pty", "gpt-5", "high"))
        assert isinstance(backend, CodexBackend)
        assert backend._pty is True
        assert backend.model == "gpt-5"

    def test_the_profile_reaches_the_interactive_argv_too(self, tmp_path: Path) -> None:
        """codex pty builds its argv in a different method, which reads the
        model and no effort. A profile has to reach that one as well."""
        backend = agent.get_backend(agent.AgentProfile("codex", "pty", "gpt-5", "high"))
        argv = backend._interactive_argv(tmp_path, None, "hi")
        assert argv[argv.index("-m") + 1] == "gpt-5"
        assert not any("model_reasoning_effort" in part for part in argv)

    def test_the_profile_reaches_the_argv(self, tmp_path: Path) -> None:
        """The whole point: a profile decides what the CLI is spawned with, and
        the process-global settings are not consulted on the way."""
        backend = agent.get_backend(
            agent.AgentProfile("codex", "native", "gpt-5-mini", "low")
        )
        argv = backend._base_argv(tmp_path)
        assert argv[argv.index("-m") + 1] == "gpt-5-mini"
        assert 'model_reasoning_effort="low"' in argv

    def test_two_profiles_build_independent_argv(self, tmp_path: Path) -> None:
        """The concurrency guarantee, stated as a test. The worker runs several
        jobs as tasks in one process; nothing here is read from shared state, so
        one job's model cannot land in another's command."""
        cheap = agent.get_backend(
            agent.AgentProfile("codex", "native", "gpt-5-mini", "low")
        )._base_argv(tmp_path)
        deep = agent.get_backend(
            agent.AgentProfile("codex", "native", "gpt-5", "high")
        )._base_argv(tmp_path)
        assert cheap[cheap.index("-m") + 1] == "gpt-5-mini"
        assert deep[deep.index("-m") + 1] == "gpt-5"


# ---------------------------------------------------------------------------
# The cron key that names a profile
# ---------------------------------------------------------------------------


class TestCronProfileKey:
    def test_a_named_profile_loads_onto_the_entry(
        self, clean_agent_env, operator_settings, tmp_path: Path
    ) -> None:
        write_agent_config(operator_settings, {"profiles": {"cheap": {}}})
        path = cron_config(
            tmp_path,
            {"name": "a", "cron": "* * * * *", "prompt": "x", "profile": "cheap"},
        )
        (entry,) = cron.load_config(path)
        assert entry.profile == "cheap"

    def test_an_entry_without_one_carries_none(
        self, clean_agent_env, operator_settings, tmp_path: Path
    ) -> None:
        path = cron_config(tmp_path, {"name": "a", "cron": "* * * * *", "prompt": "x"})
        (entry,) = cron.load_config(path)
        assert entry.profile is None

    def test_an_unknown_profile_fails_at_load(
        self, clean_agent_env, operator_settings, tmp_path: Path
    ) -> None:
        """A typo has to fail when the operator saves the file, not at 3am on
        the first fire of whichever entry names it."""
        write_agent_config(operator_settings, {"profiles": {"cheap": {}}})
        path = cron_config(
            tmp_path,
            {"name": "a", "cron": "* * * * *", "prompt": "x", "profile": "chaep"},
        )
        with pytest.raises(ValueError, match="unknown profile 'chaep'"):
            cron.load_config(path)

    def test_a_bare_command_cannot_name_one(
        self, clean_agent_env, operator_settings, tmp_path: Path
    ) -> None:
        """Ignoring it would leave a model named on an entry that never reaches
        an agent, doing nothing and saying nothing."""
        write_agent_config(operator_settings, {"profiles": {"cheap": {}}})
        path = cron_config(
            tmp_path,
            {"name": "a", "cron": "* * * * *", "command": "true", "profile": "cheap"},
        )
        with pytest.raises(ValueError, match="runs no agent"):
            cron.load_config(path)

    def test_a_producer_may_name_one(
        self, clean_agent_env, operator_settings, tmp_path: Path
    ) -> None:
        """A producer has a command AND a prompt, and the prompt is what runs
        under the profile."""
        write_agent_config(operator_settings, {"profiles": {"cheap": {}}})
        path = cron_config(
            tmp_path,
            {
                "name": "a",
                "cron": "* * * * *",
                "command": "echo '[]'",
                "prompt": "work {{ item.key }}",
                "profile": "cheap",
            },
        )
        (entry,) = cron.load_config(path)
        assert entry.profile == "cheap"

    def test_an_empty_profile_name_is_refused(
        self, clean_agent_env, operator_settings, tmp_path: Path
    ) -> None:
        path = cron_config(
            tmp_path,
            {"name": "a", "cron": "* * * * *", "prompt": "x", "profile": "  "},
        )
        with pytest.raises(ValueError, match="'profile' must be a non-empty string"):
            cron.load_config(path)

    def test_a_fired_entry_hands_the_profile_to_the_job(
        self, clean_agent_env, operator_settings, tmp_path: Path
    ) -> None:
        write_agent_config(operator_settings, {"profiles": {"cheap": {}}})
        path = cron_config(
            tmp_path,
            {"name": "a", "cron": "* * * * *", "prompt": "x", "profile": "cheap"},
        )
        (entry,) = cron.load_config(path)
        queue = FakeQueue()
        daemon = cron.CronDaemon(
            config_path=path,
            queue=queue,
            key_state=KeyStateStore(tmp_path / "state"),
            alert_sink=None,
        )
        daemon._enqueue(entry, key="a", session_key=None, prompt="x")
        (job,) = queue.jobs
        assert job.profile == "cheap"


# ---------------------------------------------------------------------------
# The queue that carries it
# ---------------------------------------------------------------------------


class TestJobRoundTrip:
    def test_the_profile_survives_the_maildir(self, tmp_path: Path) -> None:
        queue = FileInboxQueue(tmp_path / "jobs")
        queue.enqueue(Job(id="j1", prompt="hi", origin={}, profile="cheap"))
        claimed = queue.claim()
        assert claimed is not None
        assert claimed.profile == "cheap"

    def test_a_job_without_one_claims_as_none(self, tmp_path: Path) -> None:
        queue = FileInboxQueue(tmp_path / "jobs")
        queue.enqueue(Job(id="j1", prompt="hi", origin={}))
        claimed = queue.claim()
        assert claimed is not None
        assert claimed.profile is None

    def test_a_non_string_profile_is_poison(self, tmp_path: Path) -> None:
        """It would otherwise reach `resolve_profile` as an int and fail there,
        one layer past the one that owns the file's shape."""
        queue = FileInboxQueue(tmp_path / "jobs")
        queue.enqueue(Job(id="j1", prompt="hi", origin={}))
        (path,) = list((tmp_path / "jobs" / "new").iterdir())
        path.write_text(
            path.read_text().replace('"profile": null', '"profile": 5'),
            encoding="utf-8",
        )
        assert queue.claim() is None
        assert list((tmp_path / "jobs" / "failed").iterdir())


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


class TestPreflightValidatesEveryProfile:
    def test_a_profile_naming_a_bad_mode_refuses_the_start(
        self, clean_agent_env, operator_settings
    ) -> None:
        """Deferring this to the first fire is what the whole check exists to
        avoid: the operator is watching the daemon start, not the 3am cron."""
        write_agent_config(
            operator_settings,
            {"profiles": {"local": {"claude": {"mode": "telepathy"}}}},
        )
        with (
            patch.object(preflight, "check_claude_cli"),
            pytest.raises(SystemExit),
        ):
            preflight.check_backend()

    def test_the_refusal_names_the_profile(
        self, clean_agent_env, operator_settings
    ) -> None:
        """Without the name an operator sees a mode error and goes looking in
        the global `agent:` block, which is not where the typo is."""
        write_agent_config(
            operator_settings,
            {"profiles": {"local": {"claude": {"mode": "telepathy"}}}},
        )
        with (
            patch.object(preflight, "check_claude_cli"),
            pytest.raises(SystemExit, match=r"agent\.profiles\.local"),
        ):
            preflight.check_backend()

    def test_a_profile_backend_gets_its_own_cli_check(
        self, clean_agent_env, operator_settings
    ) -> None:
        write_agent_config(
            operator_settings, {"profiles": {"deep": {"backend": "codex"}}}
        )
        with (
            patch.object(preflight, "check_claude_cli") as claude,
            patch.object(preflight, "check_codex_cli") as codex,
        ):
            preflight.check_backend()
        assert claude.call_count == 1, "the global backend"
        assert codex.call_count == 1, "the profile's"

    def test_profiles_sharing_a_backend_and_mode_are_checked_once(
        self, clean_agent_env, operator_settings
    ) -> None:
        """The checks spawn the CLI. Several profiles that differ only by model
        share one binary, so checking each would pay for the same answer."""
        write_agent_config(
            operator_settings,
            {
                "profiles": {
                    "cheap": {"claude": {"model": "haiku"}},
                    "deep": {"claude": {"model": "opus"}},
                }
            },
        )
        with patch.object(preflight, "check_claude_cli") as claude:
            preflight.check_backend()
        assert claude.call_count == 1


# ---------------------------------------------------------------------------
# The runner that resolves it
# ---------------------------------------------------------------------------


class TestRunnerUsesTheJobsProfile:
    async def test_the_resolved_profile_reaches_agent_run(
        self, clean_agent_env, operator_settings, tmp_path: Path
    ) -> None:
        write_agent_config(
            operator_settings,
            {
                "backend": "codex",
                "profiles": {"cheap": {"codex": {"model": "gpt-5-mini"}}},
            },
        )
        runner = OrchestratorAgentRunner(data_dir=tmp_path)
        with patch(
            "claude_on_the_fly.jobs.agent_runner.agent.run",
            new_callable=AsyncMock,
            return_value=Response(body="ok"),
        ) as mock_run:
            await runner.run(Job(id="1-a", prompt="p", origin={}, profile="cheap"))
        profile = mock_run.call_args.kwargs["profile"]
        assert profile.model == "gpt-5-mini"

    async def test_a_job_without_a_profile_gets_the_global_one(
        self, clean_agent_env, operator_settings, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_MODEL", "opus")
        runner = OrchestratorAgentRunner(data_dir=tmp_path)
        with patch(
            "claude_on_the_fly.jobs.agent_runner.agent.run",
            new_callable=AsyncMock,
            return_value=Response(body="ok"),
        ) as mock_run:
            await runner.run(Job(id="1-a", prompt="p", origin={}))
        assert mock_run.call_args.kwargs["profile"].model == "opus"

    async def test_a_bad_profile_name_fails_the_job_not_the_worker(
        self, clean_agent_env, operator_settings, tmp_path: Path
    ) -> None:
        """It reaches the runner only if it got past load-time validation, which
        means the operator edited config.yaml after cron.yaml. The job reports
        the name; it does not take the worker down with a traceback."""
        runner = OrchestratorAgentRunner(data_dir=tmp_path)
        with patch(
            "claude_on_the_fly.jobs.agent_runner.agent.run", new_callable=AsyncMock
        ) as mock_run:
            result = await runner.run(
                Job(id="1-a", prompt="p", origin={}, profile="gone")
            )
        assert result.ok is False
        assert "gone" in result.text
        mock_run.assert_not_awaited()

    async def test_two_profiles_get_separate_sessions(
        self, clean_agent_env, operator_settings, tmp_path: Path
    ) -> None:
        """The model is part of the session identity, so a keyed entry that
        changes profile starts a fresh transcript rather than resuming one the
        new model never wrote. That is the documented cost of the feature."""
        write_agent_config(
            operator_settings,
            {
                "backend": "codex",
                "profiles": {
                    "cheap": {"codex": {"model": "gpt-5-mini"}},
                    "deep": {"codex": {"model": "gpt-5"}},
                },
            },
        )
        runner = OrchestratorAgentRunner(data_dir=tmp_path)
        seen: list[str] = []

        async def _capture(**kwargs):
            seen.append(kwargs["session_uuid"])
            return Response(body="ok")

        with patch(
            "claude_on_the_fly.jobs.agent_runner.agent.run", side_effect=_capture
        ):
            for name in ("cheap", "deep"):
                await runner.run(
                    Job(
                        id=f"1-{name}",
                        prompt="p",
                        origin={},
                        session_key="entry",
                        profile=name,
                    )
                )
        assert seen[0] != seen[1]

    def test_a_profile_can_need_a_different_check_than_the_global(
        self, clean_agent_env, operator_settings, monkeypatch
    ) -> None:
        """A profile that switches mode needs the check for the mode it selects,
        not the one the daemon's own config would have run."""
        monkeypatch.setenv("OLLAMA_MODEL", "qwen3:30b")
        write_agent_config(
            operator_settings,
            {"profiles": {"local": {"claude": {"mode": "ollama"}}}},
        )
        with (
            patch.object(preflight, "check_claude_cli") as native,
            patch.object(preflight, "check_ollama_mode") as ollama,
        ):
            preflight.check_backend()
        assert native.call_count == 1, "the global native backend"
        ollama.assert_called_once_with("claude")
