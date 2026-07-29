"""cron: config validation, producer output parsing, and the admission rules
that decide what a fire actually hands to the queue."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from claude_on_the_fly.cron import (
    DEFAULT_MAX_FIRES,
    CronDaemon,
    CronEntry,
    load_config,
    parse_items,
)
from claude_on_the_fly.jobs.core import Job
from claude_on_the_fly.jobs.key_state import KeyStateStore, fingerprint


def write_config(path: Path, entries: list[dict]) -> Path:
    path.write_text(yaml.safe_dump({"entries": entries}), encoding="utf-8")
    return path


def cfg(tmp_path: Path, *entries: dict) -> Path:
    return write_config(tmp_path / "cron.yaml", list(entries))


class FakeQueue:
    """Counts by key the way the maildir does, without the filesystem."""

    def __init__(self) -> None:
        self.jobs: list[Job] = []

    def enqueue(self, job: Job) -> None:
        self.jobs.append(job)

    def count_unfinished(self, entry: str, item: str | None = None) -> int:
        target = entry if item is None else f"{entry}/{item}"
        if item is None:
            return sum(1 for j in self.jobs if (j.key or "").split("/")[0] == entry)
        return sum(1 for j in self.jobs if j.key == target)

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


def daemon(tmp_path: Path, config: Path, queue: FakeQueue | None = None) -> CronDaemon:
    return CronDaemon(
        config_path=config,
        queue=queue or FakeQueue(),
        key_state=KeyStateStore(tmp_path / "state"),
    )


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfigShape:
    def test_prompt_only_entry_loads(self, tmp_path: Path) -> None:
        path = cfg(tmp_path, {"name": "digest", "cron": "0 9 * * *", "prompt": "hi"})
        (entry,) = load_config(path)
        assert entry.kind == "prompt"

    def test_command_with_prompt_is_a_producer(self, tmp_path: Path) -> None:
        path = cfg(
            tmp_path,
            {
                "name": "jira",
                "cron": "* * * * *",
                "command": "echo '{}'",
                "prompt": "work {{ item.key }}",
            },
        )
        (entry,) = load_config(path)
        assert entry.kind == "producer"

    def test_command_alone_is_a_side_effect(self, tmp_path: Path) -> None:
        path = cfg(tmp_path, {"name": "prune", "cron": "0 4 * * *", "command": "true"})
        (entry,) = load_config(path)
        assert entry.kind == "command"

    def test_prompt_and_prompt_file_together_is_rejected(self, tmp_path: Path) -> None:
        brief = tmp_path / "b.md"
        brief.write_text("x", encoding="utf-8")
        path = cfg(
            tmp_path,
            {
                "name": "a",
                "cron": "* * * * *",
                "prompt": "x",
                "prompt_file": str(brief),
            },
        )
        with pytest.raises(ValueError, match="'prompt' OR 'prompt_file'"):
            load_config(path)

    def test_an_entry_with_no_work_at_all_is_rejected(self, tmp_path: Path) -> None:
        path = cfg(tmp_path, {"name": "a", "cron": "* * * * *"})
        with pytest.raises(ValueError, match="needs 'prompt'"):
            load_config(path)

    def test_empty_entries_list_is_rejected(self, tmp_path: Path) -> None:
        path = cfg(tmp_path)
        with pytest.raises(ValueError, match="at least one entry"):
            load_config(path)

    def test_duplicate_names_are_rejected(self, tmp_path: Path) -> None:
        entry = {"name": "a", "cron": "* * * * *", "prompt": "x"}
        path = cfg(tmp_path, entry, dict(entry))
        with pytest.raises(ValueError, match="duplicate name"):
            load_config(path)

    def test_name_charset_is_enforced(self, tmp_path: Path) -> None:
        path = cfg(tmp_path, {"name": "bad name!", "cron": "* * * * *", "prompt": "x"})
        with pytest.raises(ValueError, match="must match"):
            load_config(path)

    def test_invalid_cron_is_rejected(self, tmp_path: Path) -> None:
        path = cfg(tmp_path, {"name": "a", "cron": "not a cron", "prompt": "x"})
        with pytest.raises(ValueError, match="invalid cron"):
            load_config(path)

    def test_timeout_bounds_are_enforced(self, tmp_path: Path) -> None:
        for bad in (0, 86401):
            path = cfg(
                tmp_path,
                {"name": "a", "cron": "* * * * *", "prompt": "x", "timeout": bad},
            )
            with pytest.raises(ValueError, match="timeout"):
                load_config(path)

    def test_max_concurrent_above_one_needs_a_producer(self, tmp_path: Path) -> None:
        """Accepting it silently would promise parallelism the dedup rule makes
        impossible: with no producer there is only ever one item, the entry."""
        path = cfg(
            tmp_path,
            {"name": "a", "cron": "* * * * *", "prompt": "x", "max_concurrent": 3},
        )
        with pytest.raises(ValueError, match="needs a 'command'"):
            load_config(path)


class TestPromptFile:
    def test_relative_path_resolves_against_the_config(self, tmp_path: Path) -> None:
        """So a config plus its prompts stays one movable bundle."""
        (tmp_path / "prompts").mkdir()
        (tmp_path / "prompts" / "jira.md").write_text("brief", encoding="utf-8")
        path = cfg(
            tmp_path,
            {"name": "a", "cron": "* * * * *", "prompt_file": "./prompts/jira.md"},
        )
        (entry,) = load_config(path)
        assert entry.prompt_source() == "brief"

    def test_missing_prompt_file_fails_at_load(self, tmp_path: Path) -> None:
        path = cfg(
            tmp_path, {"name": "a", "cron": "* * * * *", "prompt_file": "./absent.md"}
        )
        with pytest.raises(ValueError, match="prompt_file not found"):
            load_config(path)

    def test_edits_take_effect_without_touching_the_config(
        self, tmp_path: Path
    ) -> None:
        """Read at fire time, so editing the brief applies on the next fire."""
        brief = tmp_path / "b.md"
        brief.write_text("first", encoding="utf-8")
        path = cfg(tmp_path, {"name": "a", "cron": "* * * * *", "prompt_file": "b.md"})
        (entry,) = load_config(path)
        assert entry.prompt_source() == "first"

        brief.write_text("second", encoding="utf-8")

        assert entry.prompt_source() == "second"


class TestTemplateValidation:
    def test_a_syntax_error_fails_at_load(self, tmp_path: Path) -> None:
        """Otherwise it surfaces on a fire at 3am instead of when you save."""
        path = cfg(tmp_path, {"name": "a", "cron": "* * * * *", "prompt": "{% if %}"})
        with pytest.raises(ValueError, match="does not compile"):
            load_config(path)

    def test_a_plain_entry_cannot_reference_item(self, tmp_path: Path) -> None:
        """There is no producer to supply one, so this can only ever fail. The
        dry render against an empty context is what catches it."""
        path = cfg(
            tmp_path, {"name": "a", "cron": "* * * * *", "prompt": "do {{ item.key }}"}
        )
        with pytest.raises(ValueError, match="cannot supply"):
            load_config(path)

    def test_a_producer_may_reference_item(self, tmp_path: Path) -> None:
        path = cfg(
            tmp_path,
            {
                "name": "a",
                "cron": "* * * * *",
                "command": "true",
                "prompt": "do {{ item.key }}",
            },
        )
        assert load_config(path)


# ---------------------------------------------------------------------------
# Producer output
# ---------------------------------------------------------------------------


class TestParseItems:
    def test_one_object_per_line(self) -> None:
        out = '{"key": "ACE-1"}\n{"key": "ACE-2"}\n'
        assert [i["key"] for i in parse_items(out, "jira")] == ["ACE-1", "ACE-2"]

    def test_blank_lines_are_ignored(self) -> None:
        assert len(parse_items('\n{"key": "A"}\n\n', "jira")) == 1

    def test_a_bad_line_costs_only_itself(self, caplog) -> None:
        out = '{"key": "ACE-1"}\nnot json\n{"key": "ACE-2"}\n'
        items = parse_items(out, "jira")
        assert [i["key"] for i in items] == ["ACE-1", "ACE-2"]
        assert "not JSON" in caplog.text

    def test_an_array_is_rejected_with_the_fix_named(self, caplog) -> None:
        """The most likely mistake, since `jq` emits arrays by default."""
        assert parse_items('[{"key": "A"}]', "jira") == []
        assert "jq -c" in caplog.text

    def test_an_item_without_a_key_is_skipped(self, caplog) -> None:
        assert parse_items('{"title": "no key"}', "jira") == []
        assert "no usable 'key'" in caplog.text

    def test_a_blank_key_is_skipped(self) -> None:
        assert parse_items('{"key": "   "}', "jira") == []


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


def _producer_entry(**overrides) -> CronEntry:
    base = {
        "name": "jira",
        "cron": "* * * * *",
        "command": "true",
        "prompt": "work {{ item.key }}",
    }
    base.update(overrides)
    return CronEntry(
        name=base["name"],
        cron=base["cron"],
        prompt=base["prompt"],
        command=base["command"],
        max_concurrent=base.get("max_concurrent", 1),
        max_fires=base.get("max_fires", DEFAULT_MAX_FIRES),
        timeout=base.get("timeout", 1800),
    )


class TestAdmission:
    def test_an_item_becomes_a_keyed_job(self, tmp_path: Path) -> None:
        queue = FakeQueue()
        cron = daemon(tmp_path, cfg(tmp_path), queue)
        cron._admit(_producer_entry(), [{"key": "ACE-1", "title": "t"}])

        (job,) = queue.jobs
        assert job.key == "jira/ACE-1"
        assert job.session_key == "jira/ACE-1", "a tracker item resumes across fires"
        assert job.platform == "cron"
        assert job.origin == {"kind": "cron", "entry": "jira"}
        assert job.timeout == 1800.0
        assert job.prompt == "work ACE-1"

    def test_an_already_queued_item_is_not_enqueued_twice(self, tmp_path: Path) -> None:
        queue = FakeQueue()
        cron = daemon(tmp_path, cfg(tmp_path), queue)
        items = [{"key": "ACE-1"}]
        cron._admit(_producer_entry(), items)
        cron._admit(_producer_entry(), items)

        assert len(queue.jobs) == 1

    def test_max_concurrent_defers_the_rest(self, tmp_path: Path) -> None:
        queue = FakeQueue()
        cron = daemon(tmp_path, cfg(tmp_path), queue)
        cron._admit(
            _producer_entry(max_concurrent=2),
            [{"key": "ACE-1"}, {"key": "ACE-2"}, {"key": "ACE-3"}],
        )

        assert [j.key for j in queue.jobs] == ["jira/ACE-1", "jira/ACE-2"]

    def test_a_parked_item_is_skipped(self, tmp_path: Path) -> None:
        queue = FakeQueue()
        store = KeyStateStore(tmp_path / "state")
        cron = CronDaemon(cfg(tmp_path), queue, store)
        item = {"key": "ACE-1", "status": "open"}
        for _ in range(3):
            store.record_fire("jira/ACE-1", fingerprint(item))

        cron._admit(_producer_entry(max_fires=3), [item])

        assert queue.jobs == []

    def test_a_parked_item_resumes_once_it_changes(self, tmp_path: Path) -> None:
        queue = FakeQueue()
        store = KeyStateStore(tmp_path / "state")
        cron = CronDaemon(cfg(tmp_path), queue, store)
        for _ in range(3):
            store.record_fire("jira/ACE-1", fingerprint({"key": "ACE-1", "s": "open"}))

        cron._admit(_producer_entry(max_fires=3), [{"key": "ACE-1", "s": "review"}])

        assert len(queue.jobs) == 1

    def test_an_unrenderable_item_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        """Strict rendering, so a field the producer omitted on *one* item costs
        that item and lets the rest of the fire through."""
        queue = FakeQueue()
        cron = daemon(tmp_path, cfg(tmp_path), queue)
        entry = _producer_entry(prompt="work {{ item.key }} at {{ item.status }}")

        cron._admit(entry, [{"key": "ACE-1"}, {"key": "ACE-2", "status": "open"}])

        assert [j.key for j in queue.jobs] == ["jira/ACE-2"]

    def test_every_item_failing_to_render_is_reported_as_a_config_bug(
        self, tmp_path: Path, caplog
    ) -> None:
        queue = FakeQueue()
        cron = daemon(tmp_path, cfg(tmp_path), queue)
        entry = _producer_entry(prompt="work {{ item.titel }}")

        cron._admit(entry, [{"key": "ACE-1"}, {"key": "ACE-2"}])

        assert queue.jobs == []
        assert "no item could be rendered" in caplog.text


class TestPlainEntry:
    def test_one_job_per_fire_with_no_session_key(self, tmp_path: Path) -> None:
        """A daily digest should start clean, not accumulate context forever."""
        queue = FakeQueue()
        cron = daemon(tmp_path, cfg(tmp_path), queue)
        entry = CronEntry(name="digest", cron="0 9 * * *", prompt="summarise")

        cron._enqueue_plain(entry)

        (job,) = queue.jobs
        assert job.key == "digest"
        assert job.session_key is None
        assert job.prompt == "summarise"

    def test_a_still_running_previous_fire_blocks_the_next(
        self, tmp_path: Path
    ) -> None:
        queue = FakeQueue()
        cron = daemon(tmp_path, cfg(tmp_path), queue)
        entry = CronEntry(name="digest", cron="0 9 * * *", prompt="summarise")

        cron._enqueue_plain(entry)
        cron._enqueue_plain(entry)

        assert len(queue.jobs) == 1


# ---------------------------------------------------------------------------
# Firing
# ---------------------------------------------------------------------------


class TestFire:
    async def test_a_producer_fire_enqueues_what_it_printed(
        self, tmp_path: Path
    ) -> None:
        queue = FakeQueue()
        cron = daemon(tmp_path, cfg(tmp_path), queue)
        entry = _producer_entry(
            command='printf \'%s\\n\' \'{"key":"ACE-1"}\' \'{"key":"ACE-2"}\'',
            max_concurrent=2,
        )

        await cron._fire(entry)

        assert [j.key for j in queue.jobs] == ["jira/ACE-1", "jira/ACE-2"]

    async def test_a_failing_producer_enqueues_nothing(
        self, tmp_path: Path, caplog
    ) -> None:
        """A poller that cannot reach its tracker must not be read as "no work",
        which would let every in-flight key look finished."""
        queue = FakeQueue()
        cron = daemon(tmp_path, cfg(tmp_path), queue)

        await cron._fire(_producer_entry(command="exit 3"))

        assert queue.jobs == []
        assert "producer exited 3" in caplog.text

    async def test_a_broken_entry_does_not_stop_the_daemon(
        self, tmp_path: Path, caplog
    ) -> None:
        queue = FakeQueue()
        cron = daemon(tmp_path, cfg(tmp_path), queue)
        # prompt_file that vanished after load.
        entry = CronEntry(name="a", cron="* * * * *", prompt_file=tmp_path / "gone.md")

        await cron._fire(entry)  # must not raise

        assert queue.jobs == []

    async def test_a_side_effect_command_logs_and_enqueues_nothing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        written: list[tuple[str, str]] = []
        monkeypatch.setattr(
            "claude_on_the_fly.cron.append_log",
            lambda name, block: written.append((name, block)),
        )
        monkeypatch.setattr(
            "claude_on_the_fly.cron.log_path", lambda name: tmp_path / "out.log"
        )
        queue = FakeQueue()
        cron = daemon(tmp_path, cfg(tmp_path), queue)
        entry = CronEntry(name="prune", cron="0 4 * * *", command="exit 0", timeout=30)

        await cron._run_side_effect(entry)

        assert queue.jobs == []
        assert any("exit=0" in block for _, block in written)


class TestReload:
    def test_an_unrelated_edit_keeps_the_pending_fire_time(
        self, tmp_path: Path
    ) -> None:
        """Otherwise saving the file rescheduled every entry, and a `0 9 * * *`
        job could be pushed a whole day by an unrelated change."""
        path = cfg(
            tmp_path,
            {"name": "a", "cron": "0 9 * * *", "prompt": "x", "timeout": 100},
        )
        cron = daemon(tmp_path, path)
        cron.reload()
        before = cron._state["a"].next_fire

        write_config(
            path, [{"name": "a", "cron": "0 9 * * *", "prompt": "x", "timeout": 200}]
        )
        cron.reload()

        assert cron._state["a"].next_fire == before
        assert cron._state["a"].entry.timeout == 200

    def test_a_changed_cron_reschedules(self, tmp_path: Path) -> None:
        path = cfg(tmp_path, {"name": "a", "cron": "0 9 * * *", "prompt": "x"})
        cron = daemon(tmp_path, path)
        cron.reload()
        before = cron._state["a"].next_fire

        write_config(path, [{"name": "a", "cron": "*/5 * * * *", "prompt": "x"}])
        cron.reload()

        assert cron._state["a"].next_fire != before

    def test_a_broken_edit_keeps_the_prior_entries(self, tmp_path: Path) -> None:
        """A daemon that dropped every entry on a typo would silently stop working
        until somebody noticed."""
        path = cfg(tmp_path, {"name": "a", "cron": "0 9 * * *", "prompt": "x"})
        cron = daemon(tmp_path, path)
        cron.reload()

        path.write_text("entries: not-a-list", encoding="utf-8")
        cron._maybe_reload()

        assert set(cron._state) == {"a"}


class TestStop:
    async def test_stop_cancels_a_running_side_effect(self, tmp_path: Path) -> None:
        cron = daemon(tmp_path, cfg(tmp_path))
        entry = CronEntry(name="slow", cron="* * * * *", command="sleep 30", timeout=60)
        cron._spawn_command(entry)
        await asyncio.sleep(0.05)

        await cron.stop()

        assert not cron._command_tasks


# ---------------------------------------------------------------------------
# Pre-rename configs
# ---------------------------------------------------------------------------


class TestLegacySchema:
    def test_the_jobs_key_still_loads(self, tmp_path: Path) -> None:
        """`entries:` was `jobs:`. An install written before the rename must not
        need editing to start."""
        path = tmp_path / "schedule.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "jobs": [
                        {"name": "hello", "cron": "* * * * *", "prompt": "say hello"}
                    ]
                }
            ),
            encoding="utf-8",
        )
        (entry,) = load_config(path)
        assert entry.name == "hello"
        assert entry.kind == "prompt"

    def test_script_and_args_become_one_command(self, tmp_path: Path) -> None:
        path = tmp_path / "cron.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "entries": [
                        {
                            "name": "cleanup",
                            "cron": "*/30 * * * *",
                            "script": "/opt/bin/cleanup.sh",
                            "args": ["--verbose", "-n", "3"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (entry,) = load_config(path)
        assert entry.kind == "command"
        assert entry.command == "/opt/bin/cleanup.sh --verbose -n 3"

    def test_script_parts_are_quoted(self, tmp_path: Path) -> None:
        """`script` was exec'd with an argv list, so a space or a `;` was inert.
        Through a shell it is not, and an unquoted translation would run two
        commands where the original ran one."""
        path = tmp_path / "cron.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "entries": [
                        {
                            "name": "risky",
                            "cron": "* * * * *",
                            "script": "/opt/my scripts/go.sh",
                            "args": ["one; echo two"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (entry,) = load_config(path)
        assert entry.command == "'/opt/my scripts/go.sh' 'one; echo two'"

    def test_script_and_command_together_is_rejected(self, tmp_path: Path) -> None:
        path = cfg(
            tmp_path,
            {"name": "a", "cron": "* * * * *", "script": "/x.sh", "command": "echo hi"},
        )
        with pytest.raises(ValueError, match="not both"):
            load_config(path)


class TestConfigResolution:
    def test_cron_yaml_wins_when_both_exist(self, tmp_path: Path, monkeypatch) -> None:
        from claude_on_the_fly import agent
        from claude_on_the_fly.cron import resolve_config_path

        monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
        (tmp_path / "cron.yaml").write_text("entries: []", encoding="utf-8")
        (tmp_path / "schedule.yaml").write_text("jobs: []", encoding="utf-8")

        assert resolve_config_path() == tmp_path / "cron.yaml"

    def test_falls_back_to_the_legacy_name(self, tmp_path: Path, monkeypatch) -> None:
        """So doctor and the TUI read an unmigrated install as configured."""
        from claude_on_the_fly import agent
        from claude_on_the_fly.cron import resolve_config_path

        monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
        (tmp_path / "schedule.yaml").write_text("jobs: []", encoding="utf-8")

        assert resolve_config_path() == tmp_path / "schedule.yaml"

    def test_names_the_new_file_when_neither_exists(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from claude_on_the_fly import agent
        from claude_on_the_fly.cron import resolve_config_path

        monkeypatch.setattr(agent, "DATA_DIR", tmp_path)

        assert resolve_config_path() == tmp_path / "cron.yaml"

    def test_explicit_path_is_taken_as_given(self, tmp_path: Path) -> None:
        from claude_on_the_fly.cron import resolve_config_path

        assert resolve_config_path(tmp_path / "mine.yaml") == tmp_path / "mine.yaml"


class TestMigration:
    def test_rewrites_the_legacy_file_and_keeps_the_original(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from claude_on_the_fly import agent
        from claude_on_the_fly.cron import migrate_legacy_config

        monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
        legacy = tmp_path / "schedule.yaml"
        legacy.write_text(
            '# my notes\njobs:\n  - name: hello\n    cron: "* * * * *"\n'
            '    prompt: "say hello"\n',
            encoding="utf-8",
        )

        summary = migrate_legacy_config()

        assert summary is not None
        written = load_config(tmp_path / "cron.yaml")
        assert [e.name for e in written] == ["hello"]
        assert not legacy.exists(), "the old name must stop being loadable"
        kept = tmp_path / "schedule.yaml.migrated"
        assert "# my notes" in kept.read_text(), "the original is preserved verbatim"

    def test_is_a_no_op_when_already_migrated(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """It runs on every start, so it has to be idempotent, and it must never
        overwrite a cron.yaml somebody has since edited."""
        from claude_on_the_fly import agent
        from claude_on_the_fly.cron import migrate_legacy_config

        monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
        current = tmp_path / "cron.yaml"
        current.write_text("entries: [{name: mine, cron: '* * * * *', prompt: x}]\n")
        (tmp_path / "schedule.yaml").write_text("jobs: []\n")

        assert migrate_legacy_config() is None
        assert "mine" in current.read_text()

    def test_is_a_no_op_with_nothing_to_migrate(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from claude_on_the_fly import agent
        from claude_on_the_fly.cron import migrate_legacy_config

        monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
        assert migrate_legacy_config() is None
        assert not (tmp_path / "cron.yaml").exists()

    def test_a_broken_legacy_file_is_left_alone(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """Rewriting a file we cannot parse would destroy it. Leave it and let the
        normal config error explain itself."""
        from claude_on_the_fly import agent
        from claude_on_the_fly.cron import migrate_legacy_config

        monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
        legacy = tmp_path / "schedule.yaml"
        legacy.write_text("jobs:\n  - name: bad\n    cron: nonsense\n    prompt: x\n")

        assert migrate_legacy_config() is None
        assert legacy.exists()
        assert not (tmp_path / "cron.yaml").exists()
        assert "cannot migrate" in caplog.text

    def test_a_legacy_script_job_migrates_to_a_command(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The translation lands in the file the operator now edits, rather than
        happening invisibly on every load."""
        from claude_on_the_fly import agent
        from claude_on_the_fly.cron import migrate_legacy_config

        monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
        (tmp_path / "schedule.yaml").write_text(
            yaml.safe_dump(
                {
                    "jobs": [
                        {
                            "name": "cleanup",
                            "cron": "*/30 * * * *",
                            "script": "/opt/cleanup.sh",
                            "args": ["--verbose"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        migrate_legacy_config()

        # Comment lines explain the rename, so assert on the YAML body alone.
        full = (tmp_path / "cron.yaml").read_text()
        body = "\n".join(
            line for line in full.splitlines() if not line.lstrip().startswith("#")
        )
        assert "command: /opt/cleanup.sh --verbose" in body
        assert "script:" not in body
        assert "entries:" in body and "jobs:" not in body
