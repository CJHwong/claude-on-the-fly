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
        monkeypatch.setattr(
            claude_mod.transcript, "CLAUDE_PROJECTS_DIR", tmp_path / "projects"
        )
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


class TestNativeContextFields:
    """The auto-compact gate's reading in native mode. pty gets it from the
    statusline; native has to add it up."""

    def test_sums_all_three_usage_terms(self):
        out = claude_mod._native_context_fields(
            {
                "usage": {
                    "input_tokens": 2,
                    "cache_read_input_tokens": 22034,
                    "cache_creation_input_tokens": 18554,
                },
                "modelUsage": {"claude-haiku-4-5": {"contextWindow": 200000}},
            }
        )
        assert out == {"context_tokens": 40590, "context_window_size": 200000}

    def test_cache_creation_is_not_dropped(self):
        """`tokens_in` omits it on purpose, but a cold cache — the case this
        feature exists for — puts most of the prompt there."""
        out = claude_mod._native_context_fields(
            {
                "usage": {"input_tokens": 0, "cache_creation_input_tokens": 400_000},
                "modelUsage": {"m": {"contextWindow": 1_000_000}},
            }
        )
        assert out["context_tokens"] == 400_000

    def test_takes_the_widest_window_when_a_subagent_ran(self):
        """A sub-agent's narrower window would overstate how full the context is,
        and over-reading is the direction that spends money."""
        out = claude_mod._native_context_fields(
            {
                "usage": {"input_tokens": 100},
                "modelUsage": {
                    "claude-haiku-4-5": {"contextWindow": 200000},
                    "claude-sonnet-5": {"contextWindow": 1000000},
                },
            }
        )
        assert out["context_window_size"] == 1000000

    def test_no_window_means_no_reading_rather_than_zero(self):
        assert claude_mod._native_context_fields({"usage": {"input_tokens": 100}}) == {}

    def test_no_usage_means_no_reading(self):
        assert claude_mod._native_context_fields({"modelUsage": {"m": {}}}) == {}

    def test_malformed_model_usage_entry_is_ignored(self):
        out = claude_mod._native_context_fields(
            {
                "usage": {"input_tokens": 100},
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


class TestOllamaWithholdsTheContextWindow:
    """The claude CLI reports `contextWindow` from its own table even when ollama
    serves another vendor's model, so the figure describes a model that isn't
    answering. Cost gets a real substitute from OpenRouter; a window has none."""

    def _envelope(self) -> dict:
        return {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "hi",
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 38_492},
            "modelUsage": {"glm-5.2:cloud": {"contextWindow": 200000}},
            "tool_counts": {},
            "skill_counts": {},
            "compact": {},
        }

    async def _run(self, backend, tmp_path, monkeypatch):
        monkeypatch.setattr(
            claude_mod.transcript, "CLAUDE_PROJECTS_DIR", tmp_path / "projects"
        )
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
        monkeypatch.setattr(
            claude_mod.transcript, "CLAUDE_PROJECTS_DIR", tmp_path / "projects"
        )
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
