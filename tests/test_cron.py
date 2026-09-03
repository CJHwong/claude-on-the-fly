"""cron: config validation, producer output parsing, and the admission rules
that decide what a fire actually hands to the queue."""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from claude_on_the_fly import cron as cron_mod
from claude_on_the_fly.cron import (
    DEFAULT_MAX_FIRES,
    MAX_PRODUCER_TIMEOUT_S,
    PRODUCER_TIMEOUT_S,
    CronDaemon,
    CronEntry,
    load_config,
    migrate_legacy_config,
    parse_items,
    request_run_now,
)
from claude_on_the_fly.jobs.core import Job, Result
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


class _RecordingAlertSink:
    def __init__(self) -> None:
        self.calls: list[tuple[dict, Result]] = []

    async def alert(self, origin: dict, result: Result) -> None:
        self.calls.append((origin, result))


class _RaisingAlertSink:
    async def alert(self, origin: dict, result: Result) -> None:
        raise RuntimeError("alert down")


def daemon(
    tmp_path: Path,
    config: Path,
    queue: FakeQueue | None = None,
    alert_sink=None,
) -> CronDaemon:
    return CronDaemon(
        config_path=config,
        queue=queue or FakeQueue(),
        key_state=KeyStateStore(tmp_path / "state"),
        alert_sink=alert_sink,
    )


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


async def fire_and_settle(cron: CronDaemon, entry: CronEntry) -> None:
    """Fire an entry, then wait for whatever task the fire started.

    `_fire` returns as soon as the work is under way, which is the whole point of
    it, so a test that asserts on what a producer *did* has to wait for the task
    itself. Nothing in production waits like this.
    """
    await cron._fire(entry)
    await asyncio.gather(*list(cron._command_tasks), return_exceptions=True)


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


class TestUnknownKeys:
    """A key the schema does not define is an error, not a shrug.

    Ignoring one let an inert `model: sonnet` sit on a live entry: the file
    loaded, the entry fired, and nothing ever said the setting did nothing.
    """

    def test_an_unrecognised_key_is_rejected(self, tmp_path: Path) -> None:
        path = cfg(
            tmp_path,
            {"name": "digest", "cron": "0 9 * * *", "prompt": "x", "model": "sonnet"},
        )
        with pytest.raises(ValueError, match="unknown key 'model'"):
            load_config(path)

    def test_the_message_names_the_entry_and_the_valid_keys(
        self, tmp_path: Path
    ) -> None:
        """The operator edits this file by hand, so "invalid config" alone is
        unactionable: the message has to say which entry and what is allowed."""
        path = cfg(
            tmp_path,
            {"name": "digest", "cron": "0 9 * * *", "prompt": "x", "model": "sonnet"},
        )
        with pytest.raises(ValueError) as caught:
            load_config(path)
        message = str(caught.value)
        assert "(digest)" in message
        assert "valid keys: " in message
        assert "prompt_file" in message
        assert "max_concurrent" in message

    def test_every_unknown_key_is_named_at_once(self, tmp_path: Path) -> None:
        """Naming one per load would make fixing a pasted block a guessing game."""
        path = cfg(
            tmp_path,
            {
                "name": "a",
                "cron": "* * * * *",
                "prompt": "x",
                "model": "sonnet",
                "effort": "high",
            },
        )
        with pytest.raises(ValueError, match=re.escape("'effort', 'model'")):
            load_config(path)

    def test_every_documented_key_is_accepted(self, tmp_path: Path) -> None:
        """Pins the schema against the reference table: a key the docs promise
        but the validator forgot would now be rejected outright."""
        (tmp_path / "brief.md").write_text("work {{ item.key }}", encoding="utf-8")
        path = cfg(
            tmp_path,
            {
                "name": "jira",
                "cron": "* * * * *",
                "prompt_file": "./brief.md",
                "command": "true",
                "timeout": 600,
                "producer_timeout": 300,
                "max_concurrent": 2,
                "max_fires": 5,
            },
        )
        (entry,) = load_config(path)
        assert entry.kind == "producer"

    def test_a_legacy_script_entry_still_loads(self, tmp_path: Path) -> None:
        """`script` and `args` are consumed by the translation before the check,
        so tightening the schema must not lock out a pre-rename config."""
        path = cfg(
            tmp_path,
            {
                "name": "prune",
                "cron": "0 4 * * *",
                "script": "/opt/prune.sh",
                "args": ["--verbose"],
            },
        )
        (entry,) = load_config(path)
        assert entry.command == "/opt/prune.sh --verbose"


class TestProducerTimeout:
    """The producer's own limit, separate from the entry's `timeout`."""

    def test_it_defaults_to_the_module_limit(self, tmp_path: Path) -> None:
        path = cfg(
            tmp_path,
            {
                "name": "jira",
                "cron": "* * * * *",
                "command": "true",
                "prompt": "work {{ item.key }}",
            },
        )
        (entry,) = load_config(path)
        assert entry.producer_timeout == PRODUCER_TIMEOUT_S

    def test_an_entry_may_raise_its_own(self, tmp_path: Path) -> None:
        """One slow producer must not force every other entry to wait as long."""
        path = cfg(
            tmp_path,
            {
                "name": "jira",
                "cron": "* * * * *",
                "command": "true",
                "prompt": "work {{ item.key }}",
                "producer_timeout": 300,
            },
        )
        (entry,) = load_config(path)
        assert entry.producer_timeout == 300
        assert entry.timeout == 1800

    def test_bounds_are_enforced(self, tmp_path: Path) -> None:
        for bad in (0, MAX_PRODUCER_TIMEOUT_S + 1):
            path = cfg(
                tmp_path,
                {
                    "name": "jira",
                    "cron": "* * * * *",
                    "command": "true",
                    "prompt": "work {{ item.key }}",
                    "producer_timeout": bad,
                },
            )
            with pytest.raises(
                ValueError, match=re.escape("'producer_timeout' must be 1..")
            ):
                load_config(path)

    def test_it_needs_a_producer(self, tmp_path: Path) -> None:
        """A plain entry runs no command, so the key would bound nothing."""
        path = cfg(
            tmp_path,
            {
                "name": "a",
                "cron": "* * * * *",
                "prompt": "x",
                "producer_timeout": 300,
            },
        )
        with pytest.raises(ValueError, match="'producer_timeout' needs a producer"):
            load_config(path)

    def test_a_side_effect_command_cannot_carry_it(self, tmp_path: Path) -> None:
        """A bare command is bounded by the entry's own `timeout`, not this one."""
        path = cfg(
            tmp_path,
            {
                "name": "prune",
                "cron": "0 4 * * *",
                "command": "true",
                "producer_timeout": 300,
            },
        )
        with pytest.raises(ValueError, match="'producer_timeout' needs a producer"):
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
        producer_timeout=base.get("producer_timeout", PRODUCER_TIMEOUT_S),
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

        await fire_and_settle(cron, entry)

        assert [j.key for j in queue.jobs] == ["jira/ACE-1", "jira/ACE-2"]

    async def test_the_producer_gets_the_entrys_own_limit(self, tmp_path: Path) -> None:
        """Not the entry's `timeout`: that one bounds the agent run each printed
        item becomes, and is measured in tens of minutes."""
        cron = daemon(tmp_path, cfg(tmp_path))
        seen: list[float] = []

        async def record(command: str, *, timeout: float, capture: bool):
            seen.append(timeout)
            return "", 0

        cron._run_command = record  # type: ignore[method-assign]

        await fire_and_settle(cron, _producer_entry(producer_timeout=300, timeout=1800))

        assert seen == [300]

    async def test_a_producer_over_its_limit_is_killed(
        self, tmp_path: Path, caplog
    ) -> None:
        """A real subprocess, so this pins that the per-entry value reaches the
        wait rather than only reaching the dataclass."""
        queue = FakeQueue()
        cron = daemon(tmp_path, cfg(tmp_path), queue)

        await fire_and_settle(
            cron, _producer_entry(command="sleep 30", producer_timeout=1)
        )

        assert queue.jobs == []
        assert "timed out after 1s" in caplog.text

    async def test_a_failing_producer_enqueues_nothing(
        self, tmp_path: Path, caplog
    ) -> None:
        """A poller that cannot reach its tracker must not be read as "no work",
        which would let every in-flight key look finished."""
        queue = FakeQueue()
        cron = daemon(tmp_path, cfg(tmp_path), queue)

        await fire_and_settle(cron, _producer_entry(command="exit 3"))

        assert queue.jobs == []
        assert "producer exited 3" in caplog.text

    async def test_a_failing_producer_alerts(self, tmp_path: Path) -> None:
        sink = _RecordingAlertSink()
        cron = daemon(tmp_path, cfg(tmp_path), alert_sink=sink)

        await fire_and_settle(cron, _producer_entry(command="exit 3"))

        assert sink.calls == [
            (
                {"kind": "cron", "entry": "jira"},
                Result(ok=False, text="producer exited 3"),
            )
        ]

    async def test_a_successful_producer_does_not_alert(self, tmp_path: Path) -> None:
        sink = _RecordingAlertSink()
        cron = daemon(tmp_path, cfg(tmp_path), alert_sink=sink)

        await fire_and_settle(cron, _producer_entry(command="true"))

        assert sink.calls == []

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

    async def test_a_failing_side_effect_alerts(self, tmp_path: Path) -> None:
        sink = _RecordingAlertSink()
        cron = daemon(tmp_path, cfg(tmp_path), alert_sink=sink)
        entry = CronEntry(name="prune", cron="0 4 * * *", command="exit 1", timeout=30)

        await cron._run_side_effect(entry)

        assert sink.calls == [
            (
                {"kind": "cron", "entry": "prune"},
                Result(ok=False, text="command exited 1"),
            )
        ]

    async def test_a_successful_side_effect_does_not_alert(
        self, tmp_path: Path
    ) -> None:
        sink = _RecordingAlertSink()
        cron = daemon(tmp_path, cfg(tmp_path), alert_sink=sink)
        entry = CronEntry(name="prune", cron="0 4 * * *", command="exit 0", timeout=30)

        await cron._run_side_effect(entry)

        assert sink.calls == []

    async def test_a_failed_alert_does_not_take_the_daemon_down(
        self, tmp_path: Path
    ) -> None:
        cron = daemon(tmp_path, cfg(tmp_path), alert_sink=_RaisingAlertSink())
        entry = CronEntry(name="prune", cron="0 4 * * *", command="exit 1", timeout=30)

        await cron._run_side_effect(entry)  # must not raise


class TestProducerOffTheLoop:
    """A producer runs as a task, not inline in the scheduling loop.

    Awaiting it inline made every entry due in the same minute wait out the poll:
    a measured 38s producer firing four times an hour delayed the four other
    entries due at `:00` by 38s each.
    """

    def _config(self, tmp_path: Path) -> Path:
        return cfg(
            tmp_path,
            {
                "name": "slow",
                "cron": "* * * * *",
                "command": 'sleep 2; printf \'{"key":"ACE-1"}\\n\'',
                "prompt": "work {{ item.key }}",
            },
            {"name": "quick", "cron": "* * * * *", "prompt": "x"},
        )

    async def test_a_slow_producer_does_not_delay_the_entry_behind_it(
        self, tmp_path: Path
    ) -> None:
        """The one test that pins the fix. `slow` comes first in the config, so
        the loop reaches `quick` only after `_fire` returns."""
        queue = FakeQueue()
        cron = daemon(tmp_path, self._config(tmp_path), queue)
        cron._print_summary = lambda: None  # type: ignore[method-assign]
        ticks = 0

        async def one_pass_then_stop() -> None:
            nonlocal ticks
            ticks += 1
            if ticks > 1:
                cron._stop.set()

        cron._sleep_to_next_minute = one_pass_then_stop  # type: ignore[method-assign]
        cron.reload()
        for state in cron._state.values():
            state.next_fire = datetime(2000, 1, 1)

        started = time.monotonic()
        await asyncio.wait_for(cron.run(), timeout=20)
        elapsed = time.monotonic() - started

        assert [j.key for j in queue.jobs] == ["quick"]
        assert elapsed < 1, f"the scan waited for the producer ({elapsed:.1f}s)"
        assert cron._is_running("slow")

        await asyncio.gather(*list(cron._command_tasks))
        assert [j.key for j in queue.jobs] == ["quick", "slow/ACE-1"]

    async def test_the_producer_still_does_its_work_after_the_scan_moves_on(
        self, tmp_path: Path
    ) -> None:
        """Not blocking is only worth anything if the poll still lands."""
        queue = FakeQueue()
        cron = daemon(tmp_path, cfg(tmp_path), queue)

        await fire_and_settle(
            cron,
            _producer_entry(command='printf \'{"key":"ACE-9"}\\n\''),
        )

        assert [j.key for j in queue.jobs] == ["jira/ACE-9"]

    async def test_a_second_fire_is_skipped_while_the_first_still_runs(
        self, tmp_path: Path, caplog
    ) -> None:
        """Two overlapping runs of one poll would emit the same work list twice,
        and every item would be handed to `_admit` a second time."""
        queue = FakeQueue()
        cron = daemon(tmp_path, cfg(tmp_path), queue)
        entry = _producer_entry(command='sleep 2; printf \'{"key":"ACE-1"}\\n\'')

        with caplog.at_level("INFO", logger="claude_on_the_fly.cron"):
            await cron._fire(entry)
            await cron._fire(entry)

        assert len([t for t in cron._command_tasks if t.get_name() == "jira"]) == 1
        assert "still running, skipping this fire" in caplog.text

        await asyncio.gather(*list(cron._command_tasks))
        assert [j.key for j in queue.jobs] == ["jira/ACE-1"]

    async def test_a_finished_producer_does_not_block_the_next_fire(
        self, tmp_path: Path
    ) -> None:
        """The done-callback that removes a task runs a tick late, so the guard
        asks each task whether it is done rather than trusting the set."""
        queue = FakeQueue()
        cron = daemon(tmp_path, cfg(tmp_path), queue)
        entry = _producer_entry(command='printf \'{"key":"ACE-1"}\\n\'')

        await fire_and_settle(cron, entry)
        await cron._fire(entry)
        await asyncio.gather(*list(cron._command_tasks))

        assert [j.key for j in queue.jobs] == ["jira/ACE-1"]
        assert cron._queue.count_unfinished("jira") == 1

    async def test_an_exception_in_the_task_is_logged_against_its_entry(
        self, tmp_path: Path, caplog
    ) -> None:
        """`_fire` has already returned by then, so its try/except cannot catch
        this. Unhandled, it would sit in the Task until the garbage collector
        mentioned it, with no entry name attached."""
        cron = daemon(tmp_path, cfg(tmp_path), FakeQueue())

        def explode(_entry, _items):
            raise RuntimeError("queue exploded")

        cron._admit = explode  # type: ignore[method-assign]

        with caplog.at_level("ERROR", logger="claude_on_the_fly.cron"):
            await fire_and_settle(
                cron, _producer_entry(command='printf \'{"key":"ACE-1"}\\n\'')
            )

        assert "cron jira: fire failed" in caplog.text

    async def test_a_run_now_still_fires_a_producer(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """`_drain_triggers` fires through `_fire` too, so the spawn has to work
        from there as well as from the scheduled scan."""
        from claude_on_the_fly import agent

        monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
        queue = FakeQueue()
        cron = daemon(
            tmp_path,
            cfg(
                tmp_path,
                {
                    "name": "jira",
                    "cron": "0 9 * * *",
                    "command": 'printf \'{"key":"ACE-1"}\\n\'',
                    "prompt": "work {{ item.key }}",
                },
            ),
            queue,
        )
        cron.reload()
        request_run_now("jira")

        await cron._drain_triggers()
        await asyncio.gather(*list(cron._command_tasks))

        assert [j.key for j in queue.jobs] == ["jira/ACE-1"]

    async def test_stop_cancels_a_running_producer(self, tmp_path: Path) -> None:
        """A producer is a running command like any other, so a shutdown cuts it
        and names it. The work is not lost: the next fire polls again."""
        cron = daemon(tmp_path, cfg(tmp_path), FakeQueue())

        await cron._fire(_producer_entry(command="sleep 30"))
        assert cron._running_command_names() == ["jira"]
        await cron.stop()

        assert all(task.done() for task in cron._command_tasks)


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

    async def test_stop_names_the_entries_it_cancelled(
        self, tmp_path: Path, caplog
    ) -> None:
        """Without the names the daemon log shows a clean exit, and the operator
        has to open every entry's log to find which run died."""
        cron = daemon(tmp_path, cfg(tmp_path))
        cron._spawn_command(
            CronEntry(name="slow", cron="* * * * *", command="sleep 30", timeout=60)
        )
        await asyncio.sleep(0.05)

        with caplog.at_level("WARNING", logger="claude_on_the_fly.cron"):
            await cron.stop()

        assert "slow" in caplog.text

    async def test_stop_is_quiet_when_no_command_was_running(
        self, tmp_path: Path, caplog
    ) -> None:
        cron = daemon(tmp_path, cfg(tmp_path))

        with caplog.at_level("WARNING", logger="claude_on_the_fly.cron"):
            await cron.stop()

        assert caplog.text == ""

    async def test_the_heartbeat_names_the_running_commands(
        self, tmp_path: Path
    ) -> None:
        """The supervisor reads this to say what a stop will cancel, before it
        signals anything."""
        cron = daemon(tmp_path, cfg(tmp_path))
        assert cron.heartbeat_extra() == {"running_commands": []}

        cron._spawn_command(
            CronEntry(name="slow", cron="* * * * *", command="sleep 30", timeout=60)
        )
        await asyncio.sleep(0.05)

        assert cron.heartbeat_extra() == {"running_commands": ["slow"]}
        await cron.stop()


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

    def test_a_legacy_script_entry_is_carried_over_as_written(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Left as `script:`/`args:` on purpose. Rewriting them into `command:`
        would mean re-dumping the parsed entries, which deletes every comment, and
        the loader accepts the old keys so nothing is broken by leaving them."""
        from claude_on_the_fly import agent
        from claude_on_the_fly.cron import migrate_legacy_config

        monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
        (tmp_path / "schedule.yaml").write_text(
            "# keep me\n"
            "jobs:\n"
            "  - name: cleanup\n"
            '    cron: "*/30 * * * *"\n'
            "    script: /opt/cleanup.sh\n"
            '    args: ["--verbose"]\n',
            encoding="utf-8",
        )

        migrate_legacy_config()

        body = (tmp_path / "cron.yaml").read_text()
        assert "script: /opt/cleanup.sh" in body
        assert 'args: ["--verbose"]' in body
        assert "# keep me" in body
        # And it still loads, translated in memory.
        (entry,) = load_config(tmp_path / "cron.yaml")
        assert entry.kind == "command"
        assert entry.command == "/opt/cleanup.sh --verbose"

    def test_every_comment_and_line_survives(self, tmp_path: Path, monkeypatch) -> None:
        """The reason the migration is a text edit rather than a re-dump: an
        operator's comments are the part of a config that cannot be regenerated."""
        from claude_on_the_fly import agent
        from claude_on_the_fly.cron import migrate_legacy_config

        monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
        original = (
            "# Top-of-file notes I wrote.\n"
            "#\n"
            "#   an indented example: jobs:\n"
            "\n"
            "jobs:\n"
            "  # why this one exists\n"
            "  - name: hello\n"
            '    cron: "* * * * *"   # every minute, deliberately\n'
            '    prompt: "say hello"\n'
            "\n"
            "  # a second one, commented out for now\n"
            "  # - name: later\n"
            '  #   cron: "0 9 * * *"\n'
            '  #   prompt: "not yet"\n'
        )
        (tmp_path / "schedule.yaml").write_text(original, encoding="utf-8")

        migrate_legacy_config()

        body = (tmp_path / "cron.yaml").read_text()
        # Exactly one line differs from the original: the root key. Built with
        # the same column-0 anchor the code uses, because a naive replace would
        # hit the `jobs:` inside the comment above it (which is the bug this
        # guards against).
        expected = re.sub(r"^jobs:", "entries:", original, count=1, flags=re.MULTILINE)
        assert expected in body, "everything but the root key must be byte-identical"
        # The indented `jobs:` inside a comment is NOT the root key and stays put.
        assert "#   an indented example: jobs:" in body
        assert "  # why this one exists" in body
        assert "# every minute, deliberately" in body
        assert '#   prompt: "not yet"' in body

    def test_the_header_points_at_what_is_new(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from claude_on_the_fly import agent
        from claude_on_the_fly.cron import migrate_legacy_config

        monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
        (tmp_path / "schedule.yaml").write_text(
            'jobs:\n  - name: hello\n    cron: "* * * * *"\n    prompt: "hi"\n',
            encoding="utf-8",
        )

        migrate_legacy_config()

        body = (tmp_path / "cron.yaml").read_text()
        assert "prompt_file" in body, "the new template mechanism should be pointed at"
        assert "producer" in body, "as should prompt commands"
        assert "docs/how-to/cron.md" in body


# ---------------------------------------------------------------------------
# Config validation messages
# ---------------------------------------------------------------------------


class TestConfigRejections:
    """Every message names the entry and the field. A cron config is edited by hand
    and reloaded live, so "invalid config" with no location is unactionable."""

    @pytest.mark.parametrize(
        ("entry", "fragment"),
        [
            ({"name": "", "cron": "* * * * *", "prompt": "x"}, "'name'"),
            ({"name": "a", "cron": "", "prompt": "x"}, "'cron'"),
            (
                {"name": "a", "cron": "* * * * *", "prompt": "x", "timeout": -1},
                "non-negative int",
            ),
            (
                {"name": "a", "cron": "* * * * *", "prompt": "x", "max_concurrent": 0},
                "at least 1",
            ),
        ],
    )
    def test_a_bad_field_is_named(self, tmp_path: Path, entry, fragment) -> None:
        with pytest.raises(ValueError, match=re.escape(fragment)):
            load_config(cfg(tmp_path, entry))

    def test_an_entry_that_is_not_a_mapping_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "cron.yaml"
        path.write_text(
            yaml.safe_dump({"entries": ["just a string"]}), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="must be a mapping"):
            load_config(path)

    def test_a_non_mapping_root_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "cron.yaml"
        path.write_text(yaml.safe_dump(["not", "a", "mapping"]), encoding="utf-8")
        with pytest.raises(ValueError, match="root must be a mapping"):
            load_config(path)

    def test_unparseable_yaml_says_so(self, tmp_path: Path) -> None:
        path = tmp_path / "cron.yaml"
        path.write_text("entries: [unclosed", encoding="utf-8")
        with pytest.raises(ValueError, match="YAML parse error"):
            load_config(path)

    def test_a_config_that_cannot_be_read_says_so(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="cannot read"):
            load_config(tmp_path / "never-existed.yaml")

    def test_the_legacy_script_form_needs_a_path(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="'script' must be a non-empty string"):
            load_config(cfg(tmp_path, {"name": "a", "cron": "* * * * *", "script": ""}))

    def test_legacy_script_args_must_be_strings(self, tmp_path: Path) -> None:
        """They are shell-quoted individually, so a non-string would become a
        different command once a shell saw it."""
        with pytest.raises(ValueError, match="'args' must be a list of strings"):
            load_config(
                cfg(
                    tmp_path,
                    {
                        "name": "a",
                        "cron": "* * * * *",
                        "script": "poll.sh",
                        "args": "not-a-list",
                    },
                )
            )

    def test_a_prompt_file_that_cannot_be_read_names_the_entry(
        self, tmp_path: Path
    ) -> None:
        """It exists at validate time and is re-read on every fire, so an
        unreadable one has to fail with its own message rather than a bare OSError."""
        unreadable = tmp_path / "prompt.md"
        unreadable.write_text("hello {{ item.key }}", encoding="utf-8")
        unreadable.chmod(0o000)
        try:
            with pytest.raises(ValueError, match="cannot read prompt_file"):
                load_config(
                    cfg(
                        tmp_path,
                        {
                            "name": "a",
                            "cron": "* * * * *",
                            "prompt_file": str(unreadable),
                        },
                    )
                )
        finally:
            unreadable.chmod(0o644)


# ---------------------------------------------------------------------------
# Per-entry logs
# ---------------------------------------------------------------------------


class TestAppendLog:
    def test_a_log_that_cannot_be_written_does_not_drop_the_fire(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        """The daemon log still has the story; losing the per-entry copy is not worth
        dropping a fire over."""
        from claude_on_the_fly import cron as cron_mod

        monkeypatch.setattr(
            cron_mod, "log_path", lambda _name: tmp_path / "nope" / "x.log"
        )
        with caplog.at_level("WARNING", logger="claude_on_the_fly.cron"):
            cron_mod.append_log("nightly", "block\n")
        assert "could not write its log" in "\n".join(
            r.getMessage() for r in caplog.records
        )


# ---------------------------------------------------------------------------
# Live reload
# ---------------------------------------------------------------------------


class TestMaybeReload:
    def test_a_config_that_cannot_be_stated_is_logged_and_skipped(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        cron = daemon(
            tmp_path,
            cfg(tmp_path, {"name": "a", "cron": "* * * * *", "prompt": "x"}),
            FakeQueue(),
        )
        cron.reload()
        monkeypatch.setattr(
            Path, "stat", lambda self, *a, **k: (_ for _ in ()).throw(OSError("gone"))
        )
        with caplog.at_level("WARNING", logger="claude_on_the_fly.cron"):
            cron._maybe_reload()
        assert "config stat failed" in "\n".join(r.getMessage() for r in caplog.records)

    def test_an_unchanged_mtime_does_not_reload(self, tmp_path: Path) -> None:
        cron = daemon(
            tmp_path,
            cfg(tmp_path, {"name": "a", "cron": "* * * * *", "prompt": "x"}),
            FakeQueue(),
        )
        cron.reload()
        reloaded: list[int] = []
        cron.reload = lambda: (reloaded.append(1), ([], [], []))[1]  # type: ignore[method-assign]
        cron._maybe_reload()
        assert reloaded == []

    def test_a_broken_edit_keeps_the_prior_entries(
        self, tmp_path: Path, caplog
    ) -> None:
        """A live-edited config with a typo must not empty the schedule: the daemon
        keeps running what it already had."""
        path = cfg(
            tmp_path, {"name": "a", "cron": "* * * * *", "prompt": "do the thing"}
        )
        cron = daemon(tmp_path, path, FakeQueue())
        cron.reload()
        before = dict(cron._state)
        path.write_text("entries: [unclosed", encoding="utf-8")
        import os as _os

        _os.utime(path, (0, 0))
        with caplog.at_level("ERROR", logger="claude_on_the_fly.cron"):
            cron._maybe_reload()
        assert cron._state.keys() == before.keys()
        assert "keeping prior entries" in "\n".join(
            r.getMessage() for r in caplog.records
        )

    def test_a_real_edit_is_applied_and_reported(self, tmp_path: Path, caplog) -> None:
        path = cfg(tmp_path, {"name": "a", "cron": "* * * * *", "prompt": "x"})
        cron = daemon(tmp_path, path, FakeQueue())
        cron.reload()
        write_config(
            path,
            [
                {"name": "a", "cron": "* * * * *", "prompt": "x"},
                {"name": "b", "cron": "* * * * *", "prompt": "y"},
            ],
        )
        import os as _os

        _os.utime(path, (0, 0))
        with caplog.at_level("INFO", logger="claude_on_the_fly.cron"):
            cron._maybe_reload()
        assert set(cron._state) == {"a", "b"}
        assert "reloaded (+1 -0 ~0)" in "\n".join(
            r.getMessage() for r in caplog.records
        )


class TestUnknownKeyIsFatalOnlyAtStartup:
    """The two halves of the same rejection, which must not behave the same way.

    A stricter validator is only safe because a running daemon survives it. At
    startup an unknown key stops the process, so nobody gets a daemon quietly
    running a config it could not fully read. On a live edit the same error keeps
    the prior entries, so a typo saved at 3am does not empty the schedule.
    """

    def test_the_daemon_refuses_to_start(self, tmp_path: Path, monkeypatch) -> None:
        import dotenv

        from claude_on_the_fly import preflight

        monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: True)
        monkeypatch.setattr(preflight, "setup_daemon_logging", lambda _role: None)
        path = cfg(
            tmp_path,
            {"name": "a", "cron": "* * * * *", "prompt": "x", "model": "sonnet"},
        )
        monkeypatch.setattr("sys.argv", ["claude-cron", "--config", str(path)])

        with pytest.raises(SystemExit, match=r"config error: .*unknown key 'model'"):
            cron_mod.main()

    def test_a_running_daemon_keeps_its_entries(self, tmp_path: Path, caplog) -> None:
        path = cfg(tmp_path, {"name": "a", "cron": "0 9 * * *", "prompt": "x"})
        cron = daemon(tmp_path, path, FakeQueue())
        cron.reload()
        before = cron._state["a"].next_fire

        write_config(
            path,
            [{"name": "a", "cron": "0 9 * * *", "prompt": "x", "model": "sonnet"}],
        )
        import os as _os

        _os.utime(path, (0, 0))
        with caplog.at_level("ERROR", logger="claude_on_the_fly.cron"):
            cron._maybe_reload()

        assert set(cron._state) == {"a"}
        assert cron._state["a"].next_fire == before
        assert "keeping prior entries" in caplog.text
        assert "unknown key 'model'" in caplog.text

    async def test_the_kept_entries_still_fire(self, tmp_path: Path) -> None:
        """Keeping the entries is only worth anything if they still do their work,
        so this fires one after the failed reload rather than reading the dict."""
        queue = FakeQueue()
        path = cfg(tmp_path, {"name": "a", "cron": "* * * * *", "prompt": "x"})
        cron = daemon(tmp_path, path, queue)
        cron.reload()

        write_config(
            path,
            [{"name": "a", "cron": "* * * * *", "prompt": "x", "model": "sonnet"}],
        )
        import os as _os

        _os.utime(path, (0, 0))
        cron._maybe_reload()
        await cron._fire(cron._state["a"].entry)

        assert [j.key for j in queue.jobs] == ["a"]


# ---------------------------------------------------------------------------
# The daemon loop
# ---------------------------------------------------------------------------


class TestRunLoop:
    async def test_a_due_entry_fires_and_gets_a_new_next_time(
        self, tmp_path: Path
    ) -> None:
        cron = daemon(
            tmp_path,
            cfg(tmp_path, {"name": "a", "cron": "* * * * *", "prompt": "x"}),
            FakeQueue(),
        )
        fired: list[str] = []

        async def record(entry):
            fired.append(entry.name)
            await cron.stop()

        cron._fire = record  # type: ignore[method-assign]
        cron._print_summary = lambda: None  # type: ignore[method-assign]

        async def immediately(self=cron):
            return None

        cron._sleep_to_next_minute = immediately  # type: ignore[method-assign]
        cron.reload()
        for state in cron._state.values():
            state.next_fire = datetime(2000, 1, 1)
        await asyncio.wait_for(cron.run(), timeout=5)
        assert fired == ["a"]
        assert all(s.next_fire.year > 2000 for s in cron._state.values())

    async def test_a_stop_between_the_sleep_and_the_scan_ends_the_loop(
        self, tmp_path: Path
    ) -> None:
        """Otherwise a shutdown fires every due entry one more time on its way out."""
        cron = daemon(
            tmp_path,
            cfg(tmp_path, {"name": "a", "cron": "* * * * *", "prompt": "x"}),
            FakeQueue(),
        )
        cron._print_summary = lambda: None  # type: ignore[method-assign]
        fired: list[str] = []
        cron._fire = lambda entry: fired.append(entry.name)  # type: ignore[method-assign]

        async def sleep_then_stop():
            cron._stop.set()

        cron._sleep_to_next_minute = sleep_then_stop  # type: ignore[method-assign]
        cron.reload()
        for state in cron._state.values():
            state.next_fire = datetime(2000, 1, 1)
        await asyncio.wait_for(cron.run(), timeout=5)
        assert fired == []

    async def test_the_sleep_wakes_early_when_stopped(self, tmp_path: Path) -> None:
        cron = daemon(
            tmp_path,
            cfg(tmp_path, {"name": "a", "cron": "* * * * *", "prompt": "x"}),
            FakeQueue(),
        )
        cron._stop.set()
        await asyncio.wait_for(cron._sleep_to_next_minute(), timeout=2)

    async def test_a_broken_fire_is_logged_and_the_daemon_keeps_going(
        self, tmp_path: Path, caplog
    ) -> None:
        cron = daemon(
            tmp_path,
            cfg(tmp_path, {"name": "a", "cron": "* * * * *", "prompt": "x"}),
            FakeQueue(),
        )
        cron._enqueue_plain = lambda _entry: (_ for _ in ()).throw(  # type: ignore[method-assign]
            RuntimeError("queue exploded")
        )
        entry = CronEntry(name="a", cron="* * * * *", prompt="x")
        with caplog.at_level("ERROR", logger="claude_on_the_fly.cron"):
            await cron._fire(entry)
        assert "fire failed" in "\n".join(r.getMessage() for r in caplog.records)


class TestRunNow:
    """The run-now trigger: `request_run_now` writes it, the daemon drains it.

    The trigger file lives under the redirected DATA_DIR's state dir, so every
    test here monkeypatches `agent.DATA_DIR` the way the config tests do.
    """

    def _trigger(self, tmp_path: Path) -> Path:
        return tmp_path / "state" / "cron.trigger"

    def test_request_writes_the_trigger_file(self, tmp_path: Path, monkeypatch) -> None:
        from claude_on_the_fly import agent

        monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
        request_run_now("digest")
        data = json.loads(self._trigger(tmp_path).read_text())
        assert data == {"entries": ["digest"]}

    def test_request_dedupes_and_appends(self, tmp_path: Path, monkeypatch) -> None:
        from claude_on_the_fly import agent

        monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
        request_run_now("digest")
        request_run_now("digest")
        request_run_now("nightly")
        data = json.loads(self._trigger(tmp_path).read_text())
        assert data == {"entries": ["digest", "nightly"]}

    def test_request_recovers_from_a_malformed_file(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from claude_on_the_fly import agent

        monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
        self._trigger(tmp_path).parent.mkdir(parents=True)
        self._trigger(tmp_path).write_text("not json")
        request_run_now("digest")
        data = json.loads(self._trigger(tmp_path).read_text())
        assert data == {"entries": ["digest"]}

    async def test_drain_fires_a_prompt_entry_and_removes_the_file(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from claude_on_the_fly import agent

        monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
        queue = FakeQueue()
        cron = daemon(
            tmp_path,
            cfg(tmp_path, {"name": "digest", "cron": "0 9 * * *", "prompt": "hi"}),
            queue,
        )
        cron.reload()
        request_run_now("digest")

        await cron._drain_triggers()

        assert [j.key for j in queue.jobs] == ["digest"]
        assert not self._trigger(tmp_path).exists()

    async def test_drain_ignores_an_unknown_entry(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        from claude_on_the_fly import agent

        monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
        queue = FakeQueue()
        cron = daemon(
            tmp_path,
            cfg(tmp_path, {"name": "digest", "cron": "0 9 * * *", "prompt": "hi"}),
            queue,
        )
        cron.reload()
        request_run_now("ghost")

        await cron._drain_triggers()

        assert queue.jobs == []
        assert "unknown entry" in caplog.text

    async def test_drain_skips_non_string_entries(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from claude_on_the_fly import agent

        monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
        queue = FakeQueue()
        cron = daemon(
            tmp_path,
            cfg(tmp_path, {"name": "digest", "cron": "0 9 * * *", "prompt": "hi"}),
            queue,
        )
        cron.reload()
        self._trigger(tmp_path).parent.mkdir(parents=True)
        self._trigger(tmp_path).write_text(json.dumps({"entries": [1, "digest"]}))

        await cron._drain_triggers()

        assert [j.key for j in queue.jobs] == ["digest"]

    async def test_drain_ignores_a_malformed_file(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        from claude_on_the_fly import agent

        monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
        queue = FakeQueue()
        cron = daemon(
            tmp_path,
            cfg(tmp_path, {"name": "digest", "cron": "0 9 * * *", "prompt": "hi"}),
            queue,
        )
        cron.reload()
        self._trigger(tmp_path).parent.mkdir(parents=True)
        self._trigger(tmp_path).write_text(json.dumps({"foo": 1}))

        await cron._drain_triggers()

        assert queue.jobs == []
        assert "malformed" in caplog.text

    async def test_drain_ignores_an_unreadable_file(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        from claude_on_the_fly import agent

        monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
        queue = FakeQueue()
        cron = daemon(
            tmp_path,
            cfg(tmp_path, {"name": "digest", "cron": "0 9 * * *", "prompt": "hi"}),
            queue,
        )
        cron.reload()
        self._trigger(tmp_path).parent.mkdir(parents=True)
        self._trigger(tmp_path).write_text("not json")

        await cron._drain_triggers()

        assert queue.jobs == []
        assert "unreadable" in caplog.text

    async def test_drain_with_no_trigger_is_a_noop(self, tmp_path: Path) -> None:
        queue = FakeQueue()
        cron = daemon(
            tmp_path,
            cfg(tmp_path, {"name": "digest", "cron": "0 9 * * *", "prompt": "hi"}),
            queue,
        )
        cron.reload()

        await cron._drain_triggers()

        assert queue.jobs == []

    async def test_drain_survives_a_rename_race(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A request that lands between the is_file check and the rename must
        not crash the drain — the next drain picks it up."""
        from claude_on_the_fly import agent

        monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
        queue = FakeQueue()
        cron = daemon(
            tmp_path,
            cfg(tmp_path, {"name": "digest", "cron": "0 9 * * *", "prompt": "hi"}),
            queue,
        )
        cron.reload()
        request_run_now("digest")
        monkeypatch.setattr(
            "claude_on_the_fly.cron.os.replace",
            lambda _src, _dst: (_ for _ in ()).throw(FileNotFoundError()),
        )

        await cron._drain_triggers()  # must not raise

        assert queue.jobs == []

    async def test_a_crashed_drain_leftover_is_processed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A drain killed between rename and remove leaves a `.draining` file;
        the next drain must finish the job."""
        from claude_on_the_fly import agent

        monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
        queue = FakeQueue()
        cron = daemon(
            tmp_path,
            cfg(tmp_path, {"name": "digest", "cron": "0 9 * * *", "prompt": "hi"}),
            queue,
        )
        cron.reload()
        self._trigger(tmp_path).parent.mkdir(parents=True)
        self._trigger(tmp_path).with_suffix(".draining").write_text(
            json.dumps({"entries": ["digest"]})
        )

        await cron._drain_triggers()

        assert [j.key for j in queue.jobs] == ["digest"]
        assert not self._trigger(tmp_path).with_suffix(".draining").exists()

    async def test_sleep_wakes_early_for_a_trigger(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from claude_on_the_fly import agent

        monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
        cron = daemon(tmp_path, cfg(tmp_path))
        request_run_now("digest")

        start = time.monotonic()
        await asyncio.wait_for(cron._sleep_to_next_minute(), timeout=2)
        assert time.monotonic() - start < 2

    async def test_sleep_wakes_early_for_a_draining_leftover(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from claude_on_the_fly import agent

        monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
        cron = daemon(tmp_path, cfg(tmp_path))
        self._trigger(tmp_path).parent.mkdir(parents=True)
        self._trigger(tmp_path).with_suffix(".draining").write_text("{}")

        start = time.monotonic()
        await asyncio.wait_for(cron._sleep_to_next_minute(), timeout=2)
        assert time.monotonic() - start < 2

    async def test_sleep_returns_at_the_minute_boundary(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The normal path: no trigger, no stop — the loop exits when the
        deadline passes. The clock is frozen just before the minute boundary
        so the wait is a fraction of a second instead of up to 60.

        `time.monotonic` itself is not faked: the event loop reads it for its
        own timeout bookkeeping, so a fake would poison every wait_for in the
        test."""

        class _Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 1, 1, 12, 0, 59, 900_000)

        monkeypatch.setattr("claude_on_the_fly.cron.datetime", _Frozen)
        cron = daemon(tmp_path, cfg(tmp_path))

        await asyncio.wait_for(cron._sleep_to_next_minute(), timeout=5)


class TestSummary:
    def test_an_empty_schedule_prints_only_the_header(
        self, tmp_path: Path, capsys
    ) -> None:
        cron = daemon(
            tmp_path,
            cfg(tmp_path, {"name": "a", "cron": "* * * * *", "prompt": "x"}),
            FakeQueue(),
        )
        cron._print_summary()
        out = capsys.readouterr().err
        assert "Cron started" in out
        assert "0 entries" in out

    def test_the_table_lines_up_on_the_longest_name(
        self, tmp_path: Path, capsys
    ) -> None:
        cron = daemon(
            tmp_path,
            cfg(
                tmp_path,
                {"name": "short", "cron": "* * * * *", "prompt": "x"},
                {"name": "a-much-longer-name", "cron": "0 4 * * *", "prompt": "y"},
            ),
            FakeQueue(),
        )
        cron.reload()
        cron._print_summary()
        lines = [
            line
            for line in capsys.readouterr().err.splitlines()
            if line.startswith("  ")
        ]
        assert len(lines) == 2
        # Both rows pad the name to the longest one's width, so every later column
        # starts at the same offset.
        assert [line.index("next:") for line in lines].count(
            lines[0].index("next:")
        ) == 2


class TestSideEffectCommandFailures:
    async def test_a_command_that_cannot_start_is_logged_to_the_entry(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """A side-effect entry has no reply channel, so its own log is the only place
        the operator will ever see this."""
        from claude_on_the_fly import cron as cron_mod

        written: list[tuple[str, str]] = []
        monkeypatch.setattr(
            cron_mod, "append_log", lambda name, block: written.append((name, block))
        )

        async def cannot_spawn(*_args, **_kwargs):
            raise OSError("no such shell")

        monkeypatch.setattr(asyncio, "create_subprocess_shell", cannot_spawn)
        cron = daemon(
            tmp_path,
            cfg(tmp_path, {"name": "a", "cron": "* * * * *", "prompt": "x"}),
            FakeQueue(),
        )
        entry = CronEntry(name="prune", cron="0 4 * * *", command="whatever", timeout=5)
        with caplog.at_level("ERROR", logger="claude_on_the_fly.cron"):
            await cron._run_side_effect(entry)
        assert any("could not start" in block for _, block in written)
        assert "command failed to start" in "\n".join(
            r.getMessage() for r in caplog.records
        )

    async def test_a_command_that_overruns_is_killed_and_the_log_says_so(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from claude_on_the_fly import cron as cron_mod

        written: list[tuple[str, str]] = []
        monkeypatch.setattr(
            cron_mod, "append_log", lambda name, block: written.append((name, block))
        )
        monkeypatch.setattr(cron_mod, "log_path", lambda _name: tmp_path / "out.log")
        cron = daemon(
            tmp_path,
            cfg(tmp_path, {"name": "a", "cron": "* * * * *", "prompt": "x"}),
            FakeQueue(),
        )
        entry = CronEntry(name="slow", cron="0 4 * * *", command="sleep 30", timeout=1)
        object.__setattr__(entry, "timeout", 1)
        await cron._run_side_effect(entry)
        assert any("timed out after" in block for _, block in written)

    async def test_a_shutdown_mid_command_is_recorded_and_re_raised(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Re-raised so `stop()` still completes; recorded so the log does not end
        mid-run with no explanation."""
        from claude_on_the_fly import cron as cron_mod

        written: list[tuple[str, str]] = []
        monkeypatch.setattr(
            cron_mod, "append_log", lambda name, block: written.append((name, block))
        )
        monkeypatch.setattr(cron_mod, "log_path", lambda _name: tmp_path / "out.log")
        cron = daemon(
            tmp_path,
            cfg(tmp_path, {"name": "a", "cron": "* * * * *", "prompt": "x"}),
            FakeQueue(),
        )
        entry = CronEntry(name="slow", cron="0 4 * * *", command="sleep 30", timeout=60)
        task = asyncio.create_task(cron._run_side_effect(entry))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert any("cancelled during shutdown" in block for _, block in written)


class TestProducerWithNoItems:
    async def test_an_empty_producer_enqueues_nothing(
        self, tmp_path: Path, caplog
    ) -> None:
        """Distinct from a failing producer: printing nothing is a legitimate "no
        work right now"."""
        queue = FakeQueue()
        cron = daemon(
            tmp_path,
            cfg(tmp_path, {"name": "a", "cron": "* * * * *", "prompt": "x"}),
            queue,
        )
        with caplog.at_level("DEBUG", logger="claude_on_the_fly.cron"):
            await cron._fire_producer(_producer_entry(command="true"))
        assert queue.jobs == []
        assert "emitted no items" in "\n".join(r.getMessage() for r in caplog.records)


class TestRunCommand:
    async def test_producer_stderr_is_surfaced(self, tmp_path: Path, caplog) -> None:
        """Swallowing it is what makes "it just stopped finding tickets"
        unanswerable."""
        cron = daemon(
            tmp_path,
            cfg(tmp_path, {"name": "a", "cron": "* * * * *", "prompt": "x"}),
            FakeQueue(),
        )
        with caplog.at_level("WARNING", logger="claude_on_the_fly.cron"):
            out, rc = await cron._run_command(
                "echo oops >&2; echo fine", timeout=10, capture=True
            )
        assert out.strip() == "fine"
        assert rc == 0
        assert "producer stderr: oops" in "\n".join(
            r.getMessage() for r in caplog.records
        )

    async def test_a_command_that_overruns_is_killed_and_reported(
        self, tmp_path: Path
    ) -> None:
        cron = daemon(
            tmp_path,
            cfg(tmp_path, {"name": "a", "cron": "* * * * *", "prompt": "x"}),
            FakeQueue(),
        )
        out, rc = await cron._run_command("sleep 30", timeout=0.2, capture=True)
        assert "timed out after 0.2s" in out
        assert rc is None

    async def test_output_past_the_cap_is_truncated_loudly(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """A runaway producer would otherwise be parsed in full and every line of it
        admitted as an item."""
        from claude_on_the_fly import cron as cron_mod

        monkeypatch.setattr(cron_mod, "MAX_PRODUCER_BYTES", 32)
        cron = daemon(
            tmp_path,
            cfg(tmp_path, {"name": "a", "cron": "* * * * *", "prompt": "x"}),
            FakeQueue(),
        )
        with caplog.at_level("ERROR", logger="claude_on_the_fly.cron"):
            out, _rc = await cron._run_command(
                "printf 'x%.0s' $(seq 1 200)", timeout=10, capture=True
            )
        assert len(out) == 32
        assert "truncating to 32" in "\n".join(r.getMessage() for r in caplog.records)


class TestRootKeyRename:
    def test_a_root_jobs_key_is_renamed(self) -> None:
        text, changed = cron_mod_rename("jobs:\n  - name: a\n")
        assert changed is True
        assert text.startswith("entries:")

    def test_a_nested_or_commented_jobs_key_is_left_alone(self) -> None:
        """Anchored at column 0 and applied once, so an operator's comment about
        jobs, or a nested key, survives."""
        original = "# jobs: the old name\nentries:\n  - name: a\n    jobs: 2\n"
        text, changed = cron_mod_rename(original)
        assert changed is False
        assert text == original


def cron_mod_rename(text: str):
    from claude_on_the_fly.cron import _rename_root_key

    return _rename_root_key(text)


class TestMigrateLegacyConfig:
    def test_a_legacy_file_that_cannot_be_read_is_left_in_place(
        self, tmp_path: Path, caplog
    ) -> None:
        legacy = tmp_path / "jobs.yaml"
        legacy.write_text("jobs:\n  - name: a\n", encoding="utf-8")
        legacy.chmod(0o000)
        try:
            with caplog.at_level("WARNING", logger="claude_on_the_fly.cron"):
                assert (
                    migrate_legacy_config(legacy=legacy, target=tmp_path / "cron.yaml")
                    is None
                )
        finally:
            legacy.chmod(0o644)
        assert "leaving it in place" in "\n".join(
            r.getMessage() for r in caplog.records
        )

    def test_a_legacy_file_that_does_not_load_is_left_in_place(
        self, tmp_path: Path, caplog
    ) -> None:
        """Not something to rewrite silently: leave it and let the normal config
        error explain itself."""
        legacy = tmp_path / "jobs.yaml"
        legacy.write_text("jobs: [unclosed", encoding="utf-8")
        with caplog.at_level("WARNING", logger="claude_on_the_fly.cron"):
            assert (
                migrate_legacy_config(legacy=legacy, target=tmp_path / "cron.yaml")
                is None
            )
        assert "cannot migrate" in "\n".join(r.getMessage() for r in caplog.records)

    def test_nothing_to_do_when_the_target_already_exists(self, tmp_path: Path) -> None:
        legacy = tmp_path / "jobs.yaml"
        legacy.write_text("jobs:\n  - name: a\n", encoding="utf-8")
        target = tmp_path / "cron.yaml"
        target.write_text("entries: []\n", encoding="utf-8")
        assert migrate_legacy_config(legacy=legacy, target=target) is None

    def test_nothing_to_do_when_there_is_no_legacy_file(self, tmp_path: Path) -> None:
        assert (
            migrate_legacy_config(
                legacy=tmp_path / "gone.yaml", target=tmp_path / "cron.yaml"
            )
            is None
        )

    def test_the_operators_comments_survive_the_migration(self, tmp_path: Path) -> None:
        """Re-dumping the parsed entries would be tidier and would silently delete
        every comment, which is the part of a config a rewrite cannot reproduce."""
        legacy = tmp_path / "jobs.yaml"
        legacy.write_text(
            "# nightly sweep, do not remove\njobs:\n"
            '  - name: a\n    cron: "0 4 * * *"\n    prompt: do it\n',
            encoding="utf-8",
        )
        target = tmp_path / "cron.yaml"
        summary = migrate_legacy_config(legacy=legacy, target=target)
        assert summary is not None
        body = target.read_text(encoding="utf-8")
        assert "# nightly sweep, do not remove" in body
        assert "entries:" in body
        # The root key is renamed; the header the migration adds may still mention
        # the old name, so check the key itself rather than the whole file.
        assert not any(line.startswith("jobs:") for line in body.splitlines())


class TestSideEffectFiresInTheBackground:
    async def test_a_command_entry_does_not_block_the_minute_scan(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A side-effect command can run for minutes. Awaiting it inline would stall
        every other entry's fire behind it, so `_fire` spawns and moves on — and the
        task is tracked so `stop()` can cancel it."""
        from claude_on_the_fly import cron as cron_mod

        monkeypatch.setattr(cron_mod, "append_log", lambda _name, _block: None)
        monkeypatch.setattr(cron_mod, "log_path", lambda _name: tmp_path / "out.log")
        cron = daemon(
            tmp_path,
            cfg(tmp_path, {"name": "a", "cron": "* * * * *", "prompt": "x"}),
            FakeQueue(),
        )
        entry = CronEntry(name="slow", cron="0 4 * * *", command="sleep 30", timeout=60)
        await cron._fire(entry)
        assert len(cron._command_tasks) == 1, "the fire waited for the command"
        await cron.stop()
        assert cron._command_tasks == set() or all(
            t.done() for t in cron._command_tasks
        )


class TestMinToolCalls:
    """The optional per-entry floor on a run's tool calls.

    Off by default: an entry with a designed periodic no-op legitimately makes
    zero tool calls on some fires, so a blanket check would alert on each of them.
    """

    def test_it_is_unset_by_default(self, tmp_path: Path) -> None:
        path = cfg(tmp_path, {"name": "a", "cron": "* * * * *", "prompt": "x"})
        (entry,) = load_config(path)
        assert entry.min_tool_calls == 0

    def test_an_entry_may_set_one(self, tmp_path: Path) -> None:
        path = cfg(
            tmp_path,
            {
                "name": "nightly-check",
                "cron": "* * * * *",
                "prompt": "check the deploy",
                "min_tool_calls": 2,
            },
        )
        (entry,) = load_config(path)
        assert entry.min_tool_calls == 2

    def test_a_negative_floor_is_refused(self, tmp_path: Path) -> None:
        path = cfg(
            tmp_path,
            {
                "name": "a",
                "cron": "* * * * *",
                "prompt": "x",
                "min_tool_calls": -1,
            },
        )
        with pytest.raises(
            ValueError, match=re.escape("'min_tool_calls' must be a non-negative int")
        ):
            load_config(path)

    def test_a_non_integer_floor_is_refused(self, tmp_path: Path) -> None:
        for bad in ("2", 1.5, True, None, [1]):
            path = cfg(
                tmp_path,
                {
                    "name": "a",
                    "cron": "* * * * *",
                    "prompt": "x",
                    "min_tool_calls": bad,
                },
            )
            with pytest.raises(
                ValueError,
                match=re.escape("'min_tool_calls' must be a non-negative int"),
            ):
                load_config(path)

    def test_a_bare_command_cannot_carry_one(self, tmp_path: Path) -> None:
        """Same rule as `profile`: a side-effect command runs no agent, so the
        floor would bound nothing."""
        path = cfg(
            tmp_path,
            {
                "name": "prune",
                "cron": "0 4 * * *",
                "command": "true",
                "min_tool_calls": 1,
            },
        )
        with pytest.raises(ValueError, match="'min_tool_calls' needs a 'prompt'"):
            load_config(path)

    def test_a_producer_may_carry_one(self, tmp_path: Path) -> None:
        """A producer's items each become an agent run, so the floor applies."""
        path = cfg(
            tmp_path,
            {
                "name": "jira",
                "cron": "* * * * *",
                "command": "true",
                "prompt": "work {{ item.key }}",
                "min_tool_calls": 1,
            },
        )
        (entry,) = load_config(path)
        assert entry.min_tool_calls == 1

    def test_the_floor_rides_to_the_worker_on_the_job(self, tmp_path: Path) -> None:
        """The queue is the only thing between the entry and the runner, so the
        floor has to travel on the job rather than be re-read there."""
        entry = CronEntry(
            name="nightly-check",
            cron="* * * * *",
            prompt="check the deploy",
            min_tool_calls=2,
        )
        queue = FakeQueue()
        cron = daemon(tmp_path, cfg(tmp_path), queue)
        cron._enqueue(
            entry, key="nightly-check", session_key=None, prompt="check the deploy"
        )
        (job,) = queue.jobs
        assert job.min_tool_calls == 2
