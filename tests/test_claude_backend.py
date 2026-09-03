"""Tests for Claude backend implementation details."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_on_the_fly.backends import claude as claude_mod


class TestExecPty:
    async def test_cancellation_reaps_the_process_group(self) -> None:
        """Frontends cancel a running turn to implement $stop."""
        started = asyncio.Event()

        async def never_finishes() -> tuple[bytes, bytes]:
            started.set()
            await asyncio.Event().wait()
            return b"", b""  # pragma: no cover

        proc = MagicMock()
        proc.returncode = None
        proc.communicate = never_finishes

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch.object(
                claude_mod.agent, "_kill_process_tree", new_callable=AsyncMock
            ) as kill_process_tree,
        ):
            task = asyncio.create_task(
                claude_mod._exec_pty(Path("/tmp"), ["claude-pty", "hello"])
            )
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        kill_process_tree.assert_awaited_once_with(proc)


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------


def _compact_stream(outcome: str = "success", error: str | None = None) -> dict:
    """What `_exec` hands back for a `/compact` run: the status events folded in,
    and an empty `result` — the same shape a turn that produced nothing has."""
    compact: dict = {"started": True, "result": outcome}
    if error:
        compact["error"] = error
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "" if outcome == "success" else (error or ""),
        "compact": compact,
        "tool_counts": {},
        "skill_counts": {},
    }


def _write_transcript(path: Path, *, pre: int, post: int, ms: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "message": {"content": "hi"}}),
                json.dumps(
                    {
                        "type": "system",
                        "subtype": "compact_boundary",
                        "compactMetadata": {
                            "trigger": "manual",
                            "preTokens": pre,
                            "postTokens": post,
                            "durationMs": ms,
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )


class TestLastCompactBoundary:
    def test_reads_the_metadata(self, tmp_path):
        path = tmp_path / "s.jsonl"
        _write_transcript(path, pre=48939, post=5162, ms=10842)
        assert claude_mod._last_compact_boundary(path)["preTokens"] == 48939

    def test_takes_the_newest_of_several(self, tmp_path):
        """A long-lived thread compacts more than once; only the last one is this
        turn's."""
        path = tmp_path / "s.jsonl"
        _write_transcript(path, pre=100, post=10, ms=1)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "system",
                        "subtype": "compact_boundary",
                        "compactMetadata": {"preTokens": 999, "postTokens": 99},
                    }
                )
                + "\n"
            )
        assert claude_mod._last_compact_boundary(path)["preTokens"] == 999

    def test_missing_file_is_not_an_error(self, tmp_path):
        assert claude_mod._last_compact_boundary(tmp_path / "nope.jsonl") == {}

    def test_none_path_is_not_an_error(self):
        assert claude_mod._last_compact_boundary(None) == {}

    def test_malformed_lines_are_skipped(self, tmp_path):
        path = tmp_path / "s.jsonl"
        path.write_text('{"compact_boundary" oops\n', encoding="utf-8")
        assert claude_mod._last_compact_boundary(path) == {}


class TestCompactionFrom:
    def test_success_carries_the_transcript_numbers(self, tmp_path):
        path = tmp_path / "s.jsonl"
        _write_transcript(path, pre=48939, post=5162, ms=10842)
        outcome = claude_mod._compaction_from(_compact_stream(), path)
        assert (outcome.ok, outcome.pre_tokens, outcome.post_tokens) == (
            True,
            48939,
            5162,
        )
        assert outcome.duration == pytest.approx(10.842)

    def test_failure_keeps_the_clis_reason(self, tmp_path):
        outcome = claude_mod._compaction_from(
            _compact_stream("failed", "Not enough messages to compact."), None
        )
        assert outcome.ok is False
        assert outcome.error == "Not enough messages to compact."

    def test_absent_status_events_read_as_failure_not_success(self):
        """A claude build that stops emitting them must not be reported as a
        compaction that worked."""
        outcome = claude_mod._compaction_from({"result": "", "compact": {}}, None)
        assert outcome.ok is False


class TestClaudeBackendCompact:
    async def test_pty_mode_compacts_through_pty_not_native(self, tmp_path):
        """pty exists so an operator can attach to a live turn, and a compaction
        is the longest thing a thread does — the worst one to run out of band.
        Needs claude-interactive-p's PostCompact hook; `check_pty_hooks` warns
        when it is absent, because without it this call hangs."""
        session = tmp_path / "s.jsonl"
        _write_transcript(session, pre=100, post=10, ms=500)
        backend = claude_mod.ClaudeBackend.__new__(claude_mod.ClaudeBackend)
        backend.launcher = None
        backend.pty = True
        backend._pty_path = "/somewhere/claude-pty"
        backend.model = ""
        backend.effort = ""

        with (
            patch.object(backend, "session_log_path", return_value=session),
            patch.object(
                claude_mod.checks, "pty_postcompact_hook_wired", return_value=True
            ),
            patch.object(
                claude_mod.agent, "_exec", new_callable=AsyncMock
            ) as native_exec,
            patch.object(
                claude_mod,
                "_exec_pty",
                new_callable=AsyncMock,
                return_value=_compact_stream(),
            ) as pty_exec,
        ):
            outcome = await backend.compact(tmp_path, "sid")

        native_exec.assert_not_awaited()
        argv = pty_exec.await_args[0][1]
        assert argv[0] == "/somewhere/claude-pty"
        assert "-p" not in argv
        assert argv[-3:] == ["--resume", "sid", "/compact"]
        assert outcome is not None and outcome.ok is True

    async def test_native_mode_compacts_through_native_argv(self, tmp_path):
        session = tmp_path / "s.jsonl"
        _write_transcript(session, pre=100, post=10, ms=500)
        backend = claude_mod.ClaudeBackend()
        with (
            patch.object(backend, "session_log_path", return_value=session),
            patch.object(
                claude_mod.agent,
                "_exec",
                new_callable=AsyncMock,
                return_value=_compact_stream(),
            ) as native_exec,
            patch.object(claude_mod, "_exec_pty", new_callable=AsyncMock) as pty_exec,
        ):
            await backend.compact(tmp_path, "sid")

        pty_exec.assert_not_awaited()
        assert native_exec.await_args[0][1][:2] == ["claude", "-p"]

    async def test_resumes_rather_than_starting_a_session(self, tmp_path):
        session = tmp_path / "s.jsonl"
        _write_transcript(session, pre=100, post=10, ms=500)
        backend = claude_mod.ClaudeBackend()
        with (
            patch.object(backend, "session_log_path", return_value=session),
            patch.object(
                claude_mod.agent,
                "_exec",
                new_callable=AsyncMock,
                return_value=_compact_stream(),
            ) as native_exec,
        ):
            await backend.compact(tmp_path, "sid")
        argv = native_exec.await_args[0][1]
        assert "--session-id" not in argv


class TestRunDoesNotNudgeACompaction:
    async def test_compaction_prompt_skips_the_nudge_retry(self, tmp_path, monkeypatch):
        """A successful compaction returns `result: ""`. The nudge would spend a
        second billed turn asking for a reply that was never owed."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        session = (
            tmp_path
            / "projects"
            / claude_mod._workspace_to_claude_hash(tmp_path)
            / "sid.jsonl"
        )
        session.parent.mkdir(parents=True)
        session.write_text('{"type":"user"}\n', encoding="utf-8")

        backend = claude_mod.ClaudeBackend()
        with patch.object(
            claude_mod.agent,
            "_exec",
            new_callable=AsyncMock,
            return_value=_compact_stream(),
        ) as native_exec:
            response = await backend.run(tmp_path, "sid", "/compact", "slack")

        assert native_exec.await_count == 1
        assert response.body == "Compacted the conversation."


class TestRunPassesThroughABlockOnlyReply:
    async def test_a_suggestions_only_body_is_not_retried(self, tmp_path, monkeypatch):
        """A well-formed <suggestions> block means the turn reached the end of
        its instructions and chose to say nothing. That is a completed turn,
        not a dead one, so it is passed through for the orchestrator to strip
        rather than nudged."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        session = (
            tmp_path
            / "projects"
            / claude_mod._workspace_to_claude_hash(tmp_path)
            / "sid.jsonl"
        )
        session.parent.mkdir(parents=True)
        session.write_text('{"type":"user"}\n', encoding="utf-8")

        backend = claude_mod.ClaudeBackend()
        first = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": '<suggestions>["x?"]</suggestions>',
            "tool_counts": {},
            "skill_counts": {},
        }
        with patch.object(
            claude_mod.agent,
            "_exec",
            new_callable=AsyncMock,
            side_effect=[first],
        ) as native_exec:
            response = await backend.run(
                tmp_path, "sid", "hi", "slack", nudge_prompt="nudge with template"
            )

        assert native_exec.await_count == 1
        # Handed on verbatim; the orchestrator owns the placeholder.
        assert response.body == '<suggestions>["x?"]</suggestions>'

    async def test_a_block_only_body_falls_back_to_the_last_real_text(
        self, tmp_path, monkeypatch
    ):
        """A turn that ends with only a <suggestions> block did say something
        earlier: the block is the protocol token, not a reply. The last real
        text replaces it so the user sees the answer instead of the
        orchestrator's placeholder — still without a second billed turn."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        session = (
            tmp_path
            / "projects"
            / claude_mod._workspace_to_claude_hash(tmp_path)
            / "sid.jsonl"
        )
        session.parent.mkdir(parents=True)
        session.write_text('{"type":"user"}\n', encoding="utf-8")

        backend = claude_mod.ClaudeBackend()
        first = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": '<suggestions>["x?"]</suggestions>',
            "last_assistant_text": "the real summary",
            "tool_counts": {},
            "skill_counts": {},
        }
        with patch.object(
            claude_mod.agent,
            "_exec",
            new_callable=AsyncMock,
            side_effect=[first],
        ) as native_exec:
            response = await backend.run(
                tmp_path, "sid", "hi", "slack", nudge_prompt="nudge with template"
            )

        assert native_exec.await_count == 1
        assert response.body == "the real summary"


class TestNativeContextFields:
    """The auto-compact gate's reading in native mode. pty gets it from the
    statusline; native has to add it up — from the last assistant message, not
    the envelope's top-level `usage`, which is the whole turn's aggregate."""

    def test_sums_all_three_usage_terms(self):
        out = claude_mod._native_context_fields(
            {
                "last_assistant_usage": {
                    "input_tokens": 2,
                    "cache_read_input_tokens": 22034,
                    "cache_creation_input_tokens": 18554,
                },
                "modelUsage": {"claude-haiku-4-5": {"contextWindow": 200000}},
            }
        )
        assert out == {"context_tokens": 40590, "context_window_size": 200000}

    def test_top_level_usage_is_the_turn_aggregate_and_is_ignored(self):
        """The envelope's top-level `usage` sums every API call in the turn, so
        it over-reads the prompt by about the call count. A 2-call turn here
        reports ~2x the final prompt — the reading must come from the last
        assistant message only."""
        out = claude_mod._native_context_fields(
            {
                "usage": {
                    "input_tokens": 91_449,  # aggregate, what a buggy read would use
                },
                "last_assistant_usage": {"input_tokens": 46_447},
                "modelUsage": {"claude-sonnet-5": {"contextWindow": 200000}},
            }
        )
        assert out["context_tokens"] == 46_447

    def test_cache_creation_is_not_dropped(self):
        """`tokens_in` omits it on purpose, but a cold cache — the case this
        feature exists for — puts most of the prompt there."""
        out = claude_mod._native_context_fields(
            {
                "last_assistant_usage": {
                    "input_tokens": 0,
                    "cache_creation_input_tokens": 400_000,
                },
                "modelUsage": {"m": {"contextWindow": 1_000_000}},
            }
        )
        assert out["context_tokens"] == 400_000

    def test_takes_the_widest_window_when_a_subagent_ran(self):
        """A sub-agent's narrower window would overstate how full the context is,
        and over-reading is the direction that spends money."""
        out = claude_mod._native_context_fields(
            {
                "last_assistant_usage": {"input_tokens": 100},
                "modelUsage": {
                    "claude-haiku-4-5": {"contextWindow": 200000},
                    "claude-sonnet-5": {"contextWindow": 1000000},
                },
            }
        )
        assert out["context_window_size"] == 1000000

    def test_no_window_means_no_reading_rather_than_zero(self):
        assert (
            claude_mod._native_context_fields(
                {"last_assistant_usage": {"input_tokens": 100}}
            )
            == {}
        )

    def test_no_usage_means_no_reading(self):
        assert (
            claude_mod._native_context_fields(
                {"last_assistant_usage": {}, "modelUsage": {"m": {}}}
            )
            == {}
        )

    def test_malformed_model_usage_entry_is_ignored(self):
        out = claude_mod._native_context_fields(
            {
                "last_assistant_usage": {"input_tokens": 100},
                "modelUsage": {"a": "not-a-dict", "b": {"contextWindow": 200000}},
            }
        )
        assert out["context_window_size"] == 200000


class TestCompactRefusals:
    """The three ways a compaction declines, and why they must not read alike.

    `None` means the backend has no compaction at all (codex). Everything else
    is a `Compaction(ok=False)` carrying a reason, because a claude user whose
    thread is merely empty must not be told their backend can't compact.
    """

    def _pty_backend(self):
        backend = claude_mod.ClaudeBackend.__new__(claude_mod.ClaudeBackend)
        backend.launcher, backend.pty = None, True
        backend._pty_path = "/somewhere/claude-pty"
        backend.model = ""
        backend.effort = ""
        return backend

    async def test_no_session_is_not_reported_as_unsupported(self, tmp_path):
        backend = claude_mod.ClaudeBackend()
        with patch.object(backend, "session_log_path", return_value=None):
            outcome = await backend.compact(tmp_path, "sid")

        assert outcome is not None, "None would claim claude cannot compact at all"
        assert outcome.ok is False
        assert "no session" in outcome.error

    async def test_pty_without_the_hook_refuses_instead_of_hanging(self, tmp_path):
        """The frontends pass no timeout, and `_exec_pty` reads that as no
        deadline — so spawning here would wedge the chat's serial drain until
        someone sent $stop."""
        session = tmp_path / "s.jsonl"
        session.write_text('{"type":"user"}\n', encoding="utf-8")
        backend = self._pty_backend()

        with (
            patch.object(backend, "session_log_path", return_value=session),
            patch.object(
                claude_mod.checks, "pty_postcompact_hook_wired", return_value=False
            ),
            patch.object(claude_mod, "_exec_pty", new_callable=AsyncMock) as pty_exec,
        ):
            outcome = await backend.compact(tmp_path, "sid")

        pty_exec.assert_not_awaited()
        assert outcome is not None and outcome.ok is False
        assert "PostCompact" in outcome.error
        assert "curl" in outcome.error, "the message has to say how to fix it"

    async def test_pty_with_the_hook_proceeds(self, tmp_path):
        session = tmp_path / "s.jsonl"
        _write_transcript(session, pre=100, post=10, ms=500)
        backend = self._pty_backend()

        with (
            patch.object(backend, "session_log_path", return_value=session),
            patch.object(
                claude_mod.checks, "pty_postcompact_hook_wired", return_value=True
            ),
            patch.object(
                claude_mod,
                "_exec_pty",
                new_callable=AsyncMock,
                return_value=_compact_stream(),
            ) as pty_exec,
        ):
            outcome = await backend.compact(tmp_path, "sid")

        pty_exec.assert_awaited_once()
        assert outcome is not None and outcome.ok is True

    async def test_native_does_not_consult_the_pty_hook(self, tmp_path):
        """Native compaction has nothing to do with claude-pty's wiring."""
        session = tmp_path / "s.jsonl"
        _write_transcript(session, pre=100, post=10, ms=500)
        backend = claude_mod.ClaudeBackend()

        with (
            patch.object(backend, "session_log_path", return_value=session),
            patch.object(
                claude_mod.checks, "pty_postcompact_hook_wired", return_value=False
            ) as wired,
            patch.object(
                claude_mod.agent,
                "_exec",
                new_callable=AsyncMock,
                return_value=_compact_stream(),
            ),
        ):
            outcome = await backend.compact(tmp_path, "sid")

        wired.assert_not_called()
        assert outcome is not None and outcome.ok is True


class TestCompactTimeout:
    async def test_a_caller_passing_none_still_gets_a_deadline(self, tmp_path):
        """`timeout_for` defaults to None on every chat frontend, and the
        executors read None as "wait with no deadline". A compaction is one
        summarization pass, and the drain loop is serial per chat."""
        session = tmp_path / "s.jsonl"
        _write_transcript(session, pre=100, post=10, ms=500)
        backend = claude_mod.ClaudeBackend()

        with (
            patch.object(backend, "session_log_path", return_value=session),
            patch.object(
                claude_mod.agent,
                "_exec",
                new_callable=AsyncMock,
                return_value=_compact_stream(),
            ) as native_exec,
        ):
            await backend.compact(tmp_path, "sid", timeout=None)

        assert native_exec.await_args.kwargs["timeout"] == claude_mod.COMPACT_TIMEOUT

    async def test_an_explicit_timeout_still_wins(self, tmp_path):
        session = tmp_path / "s.jsonl"
        _write_transcript(session, pre=100, post=10, ms=500)
        backend = claude_mod.ClaudeBackend()

        with (
            patch.object(backend, "session_log_path", return_value=session),
            patch.object(
                claude_mod.agent,
                "_exec",
                new_callable=AsyncMock,
                return_value=_compact_stream(),
            ) as native_exec,
        ):
            await backend.compact(tmp_path, "sid", timeout=30)

        assert native_exec.await_args.kwargs["timeout"] == 30


class TestOllamaContextWindow:
    """The claude CLI reports `contextWindow` from its own table even when ollama
    serves another vendor's model, so the figure describes a model that isn't
    answering. The engine will not invent one, but it will use one the operator
    declares through `agent.ollama.context_window`."""

    def _envelope(self) -> dict:
        return {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "hi",
            # Top-level `usage` is the whole turn's aggregate — twice the last
            # message here. The reading must come from the per-message figure.
            "usage": {"input_tokens": 20, "cache_read_input_tokens": 76_984},
            "last_assistant_usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": 38_492,
            },
            "modelUsage": {"glm-5.2:cloud": {"contextWindow": 200000}},
            "tool_counts": {},
            "skill_counts": {},
            "compact": {},
        }

    async def _run(self, backend, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        session = (
            tmp_path
            / "projects"
            / claude_mod._workspace_to_claude_hash(tmp_path)
            / "sid.jsonl"
        )
        session.parent.mkdir(parents=True)
        session.write_text('{"type":"user"}\n', encoding="utf-8")
        with patch.object(
            claude_mod.agent,
            "_exec",
            new_callable=AsyncMock,
            return_value=self._envelope(),
        ):
            return await backend.run(tmp_path, "sid", "hi", "slack")

    async def test_native_reports_the_reading(self, tmp_path, monkeypatch):
        response = await self._run(claude_mod.ClaudeBackend(), tmp_path, monkeypatch)
        assert response.context_tokens == 38_502
        assert response.context_window_size == 200000

    async def test_ollama_reports_none(self, tmp_path, monkeypatch):
        backend = claude_mod.ClaudeBackend(
            launcher=claude_mod.OllamaLauncher("glm-5.2:cloud")
        )
        response = await self._run(backend, tmp_path, monkeypatch)
        assert response.context_tokens is None
        assert response.context_window_size is None, (
            "a made-up denominator is worse than none"
        )

    async def test_declared_window_is_used(self, tmp_path, monkeypatch):
        """A declared window beats the CLI's own figure, which describes the
        wrong model. 321000 is deliberately not the 200000 in the envelope."""
        backend = claude_mod.ClaudeBackend(
            launcher=claude_mod.OllamaLauncher("glm-5.2:cloud"),
            ollama_context_window=321_000,
        )
        response = await self._run(backend, tmp_path, monkeypatch)
        assert response.context_tokens == 38_502
        assert response.context_window_size == 321_000

    async def test_declared_window_does_not_leak_into_native(
        self, tmp_path, monkeypatch
    ):
        """Native derives a real window, so the override is ignored there."""
        backend = claude_mod.ClaudeBackend(ollama_context_window=321_000)
        response = await self._run(backend, tmp_path, monkeypatch)
        assert response.context_window_size == 200000

    def test_declared_window_reaches_the_footer(self):
        """The footer derives `ctx N%` from the pair, so a declared window is
        what makes the field appear in ollama mode."""
        response = claude_mod.Response(
            body="hi",
            cost=0.0,
            duration=1.0,
            tokens_in=1,
            tokens_out=1,
            model="glm-5.2:cloud",
            context_tokens=160_500,
            context_window_size=321_000,
        )
        assert "ctx 50%" in response.format_stats()


class TestBillableUsage:
    """Pricing needs the usage buckets kept apart; the footer wants them merged.
    Two jobs reading the same block."""

    def test_buckets_do_not_overlap(self):
        cli_output = {
            "usage": {
                "input_tokens": 2,
                "output_tokens": 7,
                "cache_read_input_tokens": 24_273,
                "cache_creation_input_tokens": 37_570,
            }
        }
        assert claude_mod._billable_usage(cli_output) == (2, 7, 24_273, 37_570)

    def test_it_is_not_the_display_figure(self):
        """`tokens_in` folds cache reads in for the footer's ↑N. Passing that as
        the pricing input alongside cache_read would bill those twice, at the
        dearer rate."""
        cli_output = {
            "usage": {
                "input_tokens": 2,
                "output_tokens": 7,
                "cache_read_input_tokens": 24_273,
                "cache_creation_input_tokens": 37_570,
            }
        }
        display_in, _ = claude_mod.ClaudeBackend()._extract_tokens(cli_output)
        billable_in, _, cache_read, _ = claude_mod._billable_usage(cli_output)
        assert display_in == 2 + 24_273
        assert billable_in == 2, "plain input only"
        assert billable_in + cache_read == display_in

    def test_absent_usage_is_all_zeros(self):
        assert claude_mod._billable_usage({}) == (0, 0, 0, 0)


class TestOllamaCostUsesCacheRates:
    def _envelope(self) -> dict:
        return {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "hi",
            "total_cost_usd": 999.0,  # the CLI's Anthropic-table figure
            "usage": {
                "input_tokens": 2,
                "output_tokens": 7,
                "cache_read_input_tokens": 24_273,
                "cache_creation_input_tokens": 37_570,
            },
            "modelUsage": {"glm-5.2:cloud": {"contextWindow": 200000}},
            "tool_counts": {},
            "skill_counts": {},
            "compact": {},
        }

    async def _run(self, backend, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        session = (
            tmp_path
            / "projects"
            / claude_mod._workspace_to_claude_hash(tmp_path)
            / "sid.jsonl"
        )
        session.parent.mkdir(parents=True)
        session.write_text('{"type":"user"}\n', encoding="utf-8")
        with patch.object(
            claude_mod.agent,
            "_exec",
            new_callable=AsyncMock,
            return_value=self._envelope(),
        ):
            return await backend.run(tmp_path, "sid", "hi", "slack")

    async def test_all_four_buckets_reach_the_price_table(self, tmp_path, monkeypatch):
        backend = claude_mod.ClaudeBackend(
            launcher=claude_mod.OllamaLauncher("glm-5.2:cloud")
        )
        with patch.object(
            claude_mod.pricing, "cost_for", return_value=0.03
        ) as cost_for:
            response = await self._run(backend, tmp_path, monkeypatch)

        args = cost_for.call_args[0]
        assert args[1:] == (2, 7, 24_273, 37_570), (
            "plain input, output, cache read, cache write — kept apart"
        )
        assert response.cost == 0.03

    async def test_native_still_takes_the_clis_own_figure(self, tmp_path, monkeypatch):
        """Native and pty bill through Anthropic, whose own number already prices
        cache correctly. The registry must not be consulted there."""
        backend = claude_mod.ClaudeBackend()
        with patch.object(claude_mod.pricing, "cost_for") as cost_for:
            response = await self._run(backend, tmp_path, monkeypatch)

        cost_for.assert_not_called()
        assert response.cost == 999.0


# ---------------------------------------------------------------------------
# Skill probing
# ---------------------------------------------------------------------------


class TestProbeSkills:
    """The probe launches the real CLI and reads one line. Every failure mode has
    to degrade to an empty list, because a broken probe must cost the skill picker
    its contents and nothing else."""

    def _proc(self, line: bytes) -> MagicMock:
        proc = MagicMock()
        proc.returncode = None
        proc.stdout = MagicMock()
        proc.stdout.readline = AsyncMock(return_value=line)
        return proc

    async def test_an_init_event_yields_names_and_plugins(self) -> None:
        event = {
            "type": "system",
            "subtype": "init",
            "skills": ["review", "commit"],
            "plugins": [{"name": "gf", "path": "/plugins/gf"}, "not-a-dict"],
        }
        with (
            patch(
                "asyncio.create_subprocess_exec",
                return_value=self._proc(json.dumps(event).encode()),
            ),
            patch.object(
                claude_mod.agent, "_kill_process_tree", new_callable=AsyncMock
            ),
        ):
            names, plugins = await claude_mod._probe_skills([], ["claude"])
        assert names == ["commit", "review"]
        assert plugins == [{"name": "gf", "path": "/plugins/gf"}]

    async def test_a_cli_that_never_answers_gives_up(self, caplog) -> None:
        proc = MagicMock()
        proc.returncode = None
        proc.stdout = MagicMock()

        async def never(*_args, **_kwargs):
            await asyncio.Event().wait()

        proc.stdout.readline = never
        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch.object(
                claude_mod.agent, "_kill_process_tree", new_callable=AsyncMock
            ) as kill,
            patch.object(claude_mod.asyncio, "wait_for", side_effect=TimeoutError),
            caplog.at_level("WARNING", logger="claude_on_the_fly.backends.claude"),
        ):
            assert await claude_mod._probe_skills([], ["claude"]) == ([], [])
        # Reaped even on the give-up path, or the probe leaks a CLI per query.
        kill.assert_awaited_once()
        assert "timed out waiting for init event" in "\n".join(
            r.getMessage() for r in caplog.records
        )

    @pytest.mark.parametrize(
        "line",
        [
            b"not json at all\n",
            b"",
            b'{"type": "system", "subtype": "other"}\n',
            b'{"type": "assistant"}\n',
        ],
    )
    async def test_anything_other_than_an_init_event_yields_nothing(self, line) -> None:
        with (
            patch("asyncio.create_subprocess_exec", return_value=self._proc(line)),
            patch.object(
                claude_mod.agent, "_kill_process_tree", new_callable=AsyncMock
            ),
        ):
            assert await claude_mod._probe_skills([], ["claude"]) == ([], [])


class TestSkillDescriptions:
    """Descriptions come from SKILL.md front-matter because the init event does not
    carry them, so this walks real directories and every read can fail."""

    def test_user_and_plugin_skills_are_both_scanned(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
        user_skill = tmp_path / "config" / "skills" / "review" / "SKILL.md"
        user_skill.parent.mkdir(parents=True)
        user_skill.write_text("---\nname: review\ndescription: Review a diff\n---\n")
        plugin_skill = tmp_path / "plugin" / "skills" / "deploy" / "SKILL.md"
        plugin_skill.parent.mkdir(parents=True)
        plugin_skill.write_text("---\ndescription: Ship it\n---\n")

        out = claude_mod._skill_descriptions(
            [{"name": "gf", "path": str(tmp_path / "plugin")}]
        )
        assert out["review"] == "Review a diff"
        # No name in the front matter, so the directory name stands in.
        assert out["deploy"] == "Ship it"

    def test_a_root_that_cannot_be_globbed_is_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))

        def glob_fails(self, _pattern):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "glob", glob_fails)
        assert claude_mod._skill_descriptions([]) == {}

    def test_a_skill_file_that_cannot_be_read_is_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
        first = tmp_path / "config" / "skills" / "broken" / "SKILL.md"
        first.parent.mkdir(parents=True)
        first.write_text("---\ndescription: unreadable\n---\n")
        second = tmp_path / "config" / "skills" / "fine" / "SKILL.md"
        second.parent.mkdir(parents=True)
        second.write_text("---\ndescription: readable\n---\n")
        real_read = Path.read_text

        def read_fails(self, *args, **kwargs):
            if self.parent.name == "broken":
                raise OSError("permission denied")
            return real_read(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read_fails)
        out = claude_mod._skill_descriptions([])
        assert "broken" not in out
        assert out["fine"] == "readable"

    def test_a_plugin_without_a_path_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
        assert claude_mod._skill_descriptions([{"name": "gf"}]) == {}

    def test_the_first_definition_of_a_name_wins(self, tmp_path, monkeypatch):
        """User skills are scanned first on purpose: an operator's own version of a
        name should not be relabelled by a plugin that ships the same name."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
        mine = tmp_path / "config" / "skills" / "review" / "SKILL.md"
        mine.parent.mkdir(parents=True)
        mine.write_text("---\nname: review\ndescription: Mine\n---\n")
        theirs = tmp_path / "plugin" / "skills" / "review" / "SKILL.md"
        theirs.parent.mkdir(parents=True)
        theirs.write_text("---\nname: review\ndescription: Theirs\n---\n")
        out = claude_mod._skill_descriptions(
            [{"name": "gf", "path": str(tmp_path / "plugin")}]
        )
        assert out["review"] == "Mine"


# ---------------------------------------------------------------------------
# pty envelope failures
# ---------------------------------------------------------------------------


class TestPtyEnvelopeFailures:
    def _proc(self, stdout: bytes, stderr: bytes = b"", rc: int = 0) -> MagicMock:
        proc = MagicMock()
        proc.returncode = rc
        proc.communicate = AsyncMock(return_value=(stdout, stderr))
        return proc

    async def _run(self, proc, **kwargs):
        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch.object(
                claude_mod.agent, "_kill_process_tree", new_callable=AsyncMock
            ),
        ):
            return await claude_mod._exec_pty(Path("/tmp"), ["claude-pty"], **kwargs)

    async def test_a_timeout_names_the_limit_it_hit(self) -> None:
        proc = MagicMock()
        proc.returncode = None

        async def never(*_args, **_kwargs):
            await asyncio.Event().wait()

        proc.communicate = never
        with pytest.raises(RuntimeError, match=r"timed out after 0\.01s"):
            await self._run(proc, timeout=0.01)

    async def test_a_nonzero_exit_with_no_envelope_is_classified(self) -> None:
        """`_classify` is what turns a raw CLI complaint into the right exception
        type, so the frontend can tell "not installed" from "the model refused"."""
        proc = self._proc(b"", b"Credit balance is too low", rc=1)
        with pytest.raises(Exception) as caught:
            await self._run(proc)
        assert "Credit balance" in str(caught.value)

    async def test_silence_points_at_the_stop_hook(self) -> None:
        """The most common cause is a Claude Code upgrade breaking pty's hook, and
        the error has to say so or the operator has nothing to act on."""
        with pytest.raises(RuntimeError, match="troubleshooting"):
            await self._run(self._proc(b"", b"", rc=0))

    async def test_malformed_json_shows_the_first_bytes(self) -> None:
        """Without the excerpt this is unfixable: the whole question is what the
        wrapper printed instead of an envelope."""
        with pytest.raises(RuntimeError, match="malformed JSON"):
            await self._run(self._proc(b"<html>error page</html>"))

    async def test_an_error_envelope_is_classified_not_returned(self) -> None:
        envelope = {"is_error": True, "result": "usage limit reached"}
        with pytest.raises(Exception) as caught:
            await self._run(self._proc(json.dumps(envelope).encode()))
        assert "usage limit" in str(caught.value)

    async def test_an_error_subtype_is_classified_too(self) -> None:
        envelope = {"subtype": "error_during_execution", "result": "tool blew up"}
        with pytest.raises(Exception) as caught:
            await self._run(self._proc(json.dumps(envelope).encode()))
        assert "tool blew up" in str(caught.value)

    async def test_a_good_envelope_gets_the_count_defaults(self) -> None:
        """Downstream indexes these unconditionally."""
        envelope = await self._run(self._proc(b'{"result": "done"}'))
        assert envelope["tool_counts"] == {}
        assert envelope["skill_counts"] == {}


# ---------------------------------------------------------------------------
# pty statusline -> Response
# ---------------------------------------------------------------------------


class TestStatuslineResponseFields:
    def test_an_empty_statusline_yields_nothing(self) -> None:
        """native and ollama publish no statusline, and Response's own defaults
        must stand rather than being overwritten with zeros."""
        assert claude_mod._statusline_response_fields({}) == {}

    def test_the_absolutes_the_compaction_gate_reads_are_carried(self) -> None:
        """A bare percentage is not enough: `total_input_tokens` is the whole
        prompt, so it never drops below the system prompt and tool schemas however
        hard the conversation is compacted. The gate needs the window size next to
        it to know where the floor is."""
        fields = claude_mod._statusline_response_fields(
            {
                "context_window": {
                    "used_percentage": 65,
                    "total_input_tokens": 650_000,
                    "context_window_size": 1_000_000,
                }
            }
        )
        assert fields == {
            "context_window_pct": 65,
            "context_tokens": 650_000,
            "context_window_size": 1_000_000,
        }

    def test_a_partial_context_window_only_reports_what_it_has(self) -> None:
        """A missing key must stay missing rather than becoming a zero the gate
        would read as an empty context."""
        fields = claude_mod._statusline_response_fields(
            {"context_window": {"total_input_tokens": 650_000}}
        )
        assert fields == {"context_tokens": 650_000}

    def test_rate_limits_and_flags_come_through(self) -> None:
        fields = claude_mod._statusline_response_fields(
            {
                "rate_limits": {
                    "five_hour": {"used_percentage": 12, "resets_at": 1000},
                    "seven_day": {"used_percentage": 80, "resets_at": 2000},
                },
                "exceeds_200k_tokens": True,
                "fast_mode": False,
            }
        )
        assert fields == {
            "rate_limits_5h_pct": 12,
            "rate_limits_5h_resets_at": 1000,
            "rate_limits_7d_pct": 80,
            "rate_limits_7d_resets_at": 2000,
            "exceeds_200k": True,
            "fast_mode": False,
        }


class TestPtySpawnTrustsItsWorkspace:
    async def test_the_workspace_is_trusted_before_the_spawn(
        self, tmp_path, monkeypatch
    ):
        """Order matters: claude stops on the trust dialog rather than failing,
        so trusting after the spawn would still burn the whole turn."""
        events: list[str] = []

        monkeypatch.setattr(
            claude_mod.pty_install,
            "ensure_workspace_trusted",
            lambda ws: events.append(f"trusted:{ws.name}") or True,
        )

        class _Proc:
            returncode = 0
            pid = 1234

            def __init__(self):
                events.append("spawned")
                self.stdout = None
                self.stderr = None

            async def communicate(self):
                return b'{"result": "hi"}', b""

        async def fake_spawn(*_args, **_kwargs):
            return _Proc()

        with (
            patch("asyncio.create_subprocess_exec", fake_spawn),
            patch.object(claude_mod.agent, "track_agent_process"),
            patch.object(claude_mod.agent, "_kill_process_tree", AsyncMock()),
        ):
            await claude_mod._exec_pty(tmp_path / "ws", ["claude-pty"], timeout=None)

        assert events == ["trusted:ws", "spawned"]
