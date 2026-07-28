"""Tests for pricing.py (OpenRouter-backed)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_on_the_fly import pricing


@pytest.fixture(autouse=True)
def isolated_pricing_cache(tmp_path, monkeypatch):
    """Redirect pricing module's cache paths and clear the in-process memo."""
    cache_dir = tmp_path / "pricing"
    monkeypatch.setattr(pricing, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(pricing, "CACHE_PATH", cache_dir / "openrouter.json")
    monkeypatch.setattr(pricing, "_memo", (None, None))
    monkeypatch.delenv("COTF_PRICING_TTL_SECONDS", raising=False)
    yield cache_dir


def _entry(model_id: str, prompt: float, completion: float) -> dict:
    return {
        "id": model_id,
        "pricing": {
            "prompt": str(prompt),
            "completion": str(completion),
        },
    }


def _payload(*entries: dict) -> dict:
    return {"data": list(entries)}


def _seed_cache(cache_dir: Path, payload: dict, age_seconds: float = 0) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / "openrouter.json"
    path.write_text(json.dumps(payload))
    if age_seconds:
        mtime = time.time() - age_seconds
        os.utime(path, (mtime, mtime))
    return path


SAMPLE = _payload(
    _entry("openai/gpt-5", 1.25e-06, 1e-05),
    _entry("openai/gpt-5.4", 2.5e-06, 1.5e-05),
    _entry("openai/gpt-4.1", 2e-06, 8e-06),
    _entry("deepseek/deepseek-v4-flash", 1.12e-07, 2.24e-07),
    _entry("deepseek/deepseek-v4-pro", 4.35e-07, 8.7e-07),
    _entry("anthropic/claude-sonnet-4", 3e-06, 1.5e-05),
)


# ---------------------------------------------------------------------------
# cost math
# ---------------------------------------------------------------------------


class TestCostMath:
    def test_exact_stripped_name_match(self, isolated_pricing_cache):
        _seed_cache(isolated_pricing_cache, SAMPLE)
        cost = pricing.cost_for("gpt-5", 1000, 100)
        # 1000 * 1.25e-6 + 100 * 1e-5 = 0.00225
        assert cost == pytest.approx(0.00225)

    def test_deepseek_v4_flash_priced(self, isolated_pricing_cache):
        """The motivating case: OpenRouter has it, LiteLLM didn't."""
        _seed_cache(isolated_pricing_cache, SAMPLE)
        cost = pricing.cost_for("deepseek-v4-flash", 1_000_000, 100_000)
        # 1M * 1.12e-7 + 100k * 2.24e-7 = 0.112 + 0.0224
        assert cost == pytest.approx(0.1344)

    def test_returns_none_when_model_missing(self, isolated_pricing_cache):
        _seed_cache(isolated_pricing_cache, SAMPLE)
        assert pricing.cost_for("nonexistent-xyz", 1000, 100) is None

    def test_returns_none_for_empty_model(self, isolated_pricing_cache):
        _seed_cache(isolated_pricing_cache, SAMPLE)
        assert pricing.cost_for("", 1000, 100) is None

    def test_returns_none_for_negative_tokens(self, isolated_pricing_cache):
        _seed_cache(isolated_pricing_cache, SAMPLE)
        assert pricing.cost_for("gpt-5", -1, 100) is None
        assert pricing.cost_for("gpt-5", 1, -100) is None

    def test_zero_tokens_returns_none_not_zero(self, isolated_pricing_cache):
        """Zero cost is indistinguishable from a miss; return None so the
        caller's `or 0` produces a consistent footer."""
        _seed_cache(isolated_pricing_cache, SAMPLE)
        assert pricing.cost_for("gpt-5", 0, 0) is None


# ---------------------------------------------------------------------------
# lookup variants: vendor strip + :cloud strip + date strip
# ---------------------------------------------------------------------------


class TestLookupVariants:
    def test_cloud_suffix_stripped(self, isolated_pricing_cache):
        _seed_cache(isolated_pricing_cache, SAMPLE)
        # Codex+ollama reports `deepseek-v4-flash:cloud`; index has
        # `deepseek-v4-flash` after vendor strip.
        cost = pricing.cost_for("deepseek-v4-flash:cloud", 1_000_000, 1_000)
        assert cost is not None and cost > 0

    def test_date_suffix_stripped(self, isolated_pricing_cache):
        _seed_cache(isolated_pricing_cache, SAMPLE)
        # OpenAI snapshot drift: codex says `gpt-5-2026-03-05`, table has `gpt-5`.
        cost = pricing.cost_for("gpt-5-2026-03-05", 1000, 100)
        # Same math as plain gpt-5
        assert cost == pytest.approx(0.00225)

    def test_eight_digit_date_suffix_stripped(self, isolated_pricing_cache):
        _seed_cache(isolated_pricing_cache, SAMPLE)
        cost = pricing.cost_for("gpt-5-20260305", 1000, 100)
        assert cost == pytest.approx(0.00225)

    def test_date_strip_combined_with_cloud_strip(self, isolated_pricing_cache):
        payload = _payload(_entry("foo/bar-flash", 1e-06, 2e-06))
        _seed_cache(isolated_pricing_cache, payload)
        # `bar-flash-2025-04-14:cloud` -> strip date -> strip :cloud -> `bar-flash`
        cost = pricing.cost_for("bar-flash-2025-04-14:cloud", 1000, 100)
        assert cost == pytest.approx(0.0012)

    def test_exact_match_beats_date_stripped(self, isolated_pricing_cache):
        """If both `gpt-5-2024-01-01` and `gpt-5` exist, prefer the exact one."""
        payload = _payload(
            _entry("openai/gpt-5", 1e-06, 1e-06),
            _entry("openai/gpt-5-2024-01-01", 5e-06, 5e-06),
        )
        _seed_cache(isolated_pricing_cache, payload)
        cost = pricing.cost_for("gpt-5-2024-01-01", 1000, 0)
        # Should pick the snapshot's price, not the canonical's.
        assert cost == pytest.approx(0.005)


# ---------------------------------------------------------------------------
# vendor-prefix stripping + collision handling
# ---------------------------------------------------------------------------


class TestVendorStripping:
    def test_no_slash_used_as_is(self, isolated_pricing_cache):
        payload = _payload(_entry("bare-model", 1e-06, 1e-06))
        _seed_cache(isolated_pricing_cache, payload)
        cost = pricing.cost_for("bare-model", 1000, 0)
        assert cost == pytest.approx(0.001)

    def test_nonzero_entry_wins_over_zero(self, isolated_pricing_cache):
        """Two vendor variants strip to the same name: keep the priced one."""
        payload = _payload(
            _entry("a/model-x", 0, 0),
            _entry("b/model-x", 5e-06, 1e-05),
        )
        _seed_cache(isolated_pricing_cache, payload)
        cost = pricing.cost_for("model-x", 1000, 100)
        # 1000 * 5e-6 + 100 * 1e-5 = 0.006
        assert cost == pytest.approx(0.006)


# ---------------------------------------------------------------------------
# entry hardening
# ---------------------------------------------------------------------------


class TestEntryHardening:
    def test_missing_pricing_block_skipped(self, isolated_pricing_cache):
        payload = {"data": [{"id": "foo/bar"}]}  # no pricing block
        _seed_cache(isolated_pricing_cache, payload)
        with patch.object(pricing, "_fetch", return_value=None):
            assert pricing.cost_for("bar", 1000, 100) is None

    def test_string_prices_parsed(self, isolated_pricing_cache):
        """OpenRouter ships prices as strings, not numbers — we coerce."""
        _seed_cache(isolated_pricing_cache, SAMPLE)
        # Already tested via SAMPLE which uses string prices; this confirms.
        cost = pricing.cost_for("gpt-4.1", 1000, 100)
        assert cost == pytest.approx(1000 * 2e-6 + 100 * 8e-6)

    def test_non_numeric_price_entry_skipped(self, isolated_pricing_cache):
        payload = _payload(
            {"id": "foo/bar", "pricing": {"prompt": "free", "completion": "free"}}
        )
        _seed_cache(isolated_pricing_cache, payload)
        with patch.object(pricing, "_fetch", return_value=None):
            assert pricing.cost_for("bar", 1000, 100) is None

    def test_negative_price_entry_skipped(self, isolated_pricing_cache):
        payload = _payload(
            {"id": "foo/bar", "pricing": {"prompt": "-1", "completion": "1e-06"}}
        )
        _seed_cache(isolated_pricing_cache, payload)
        with patch.object(pricing, "_fetch", return_value=None):
            # entry filtered out → returns None
            assert pricing.cost_for("bar", 1000, 100) is None

    def test_data_not_a_list_returns_empty_index(self, isolated_pricing_cache):
        # Cache file present but `data` is malformed → treat as empty
        _seed_cache(isolated_pricing_cache, {"data": "oops"})
        with patch.object(pricing, "_fetch", return_value=None):
            assert pricing.cost_for("gpt-5", 1000, 100) is None


# ---------------------------------------------------------------------------
# cache + fetch lifecycle
# ---------------------------------------------------------------------------


class TestCacheLifecycle:
    def test_no_cache_no_network_returns_none(self, isolated_pricing_cache):
        with patch.object(pricing, "_fetch", return_value=None):
            assert pricing.cost_for("gpt-5", 1000, 100) is None

    def test_no_cache_fetches_and_persists(self, isolated_pricing_cache):
        with patch.object(pricing, "_fetch", return_value=SAMPLE) as mock_fetch:
            cost = pricing.cost_for("gpt-5", 1000, 100)
        assert cost is not None
        assert mock_fetch.call_count == 1
        assert (isolated_pricing_cache / "openrouter.json").is_file()

    def test_fresh_cache_skips_fetch(self, isolated_pricing_cache):
        _seed_cache(isolated_pricing_cache, SAMPLE)
        with patch.object(pricing, "_fetch") as mock_fetch:
            pricing.cost_for("gpt-5", 1000, 100)
        mock_fetch.assert_not_called()

    def test_stale_cache_triggers_refresh(self, isolated_pricing_cache):
        _seed_cache(isolated_pricing_cache, _payload(), age_seconds=8 * 24 * 3600)
        with patch.object(pricing, "_fetch", return_value=SAMPLE) as mock_fetch:
            cost = pricing.cost_for("gpt-5", 1000, 100)
        mock_fetch.assert_called_once()
        assert cost is not None

    def test_stale_cache_used_when_refresh_fails(self, isolated_pricing_cache):
        _seed_cache(isolated_pricing_cache, SAMPLE, age_seconds=30 * 24 * 3600)
        with patch.object(pricing, "_fetch", return_value=None):
            cost = pricing.cost_for("gpt-5", 1000, 100)
        assert cost is not None  # stale beats nothing

    def test_corrupt_cache_treated_as_missing(self, isolated_pricing_cache):
        isolated_pricing_cache.mkdir(parents=True, exist_ok=True)
        (isolated_pricing_cache / "openrouter.json").write_text("not-json {][")
        with patch.object(pricing, "_fetch", return_value=SAMPLE) as mock_fetch:
            cost = pricing.cost_for("gpt-5", 1000, 100)
        mock_fetch.assert_called_once()
        assert cost is not None

    def test_cache_file_with_non_object_root_rejected(self, isolated_pricing_cache):
        isolated_pricing_cache.mkdir(parents=True, exist_ok=True)
        (isolated_pricing_cache / "openrouter.json").write_text("[]")
        with patch.object(pricing, "_fetch", return_value=None):
            assert pricing.cost_for("gpt-5", 1000, 100) is None

    def test_ttl_zero_always_refreshes(self, isolated_pricing_cache, monkeypatch):
        _seed_cache(isolated_pricing_cache, SAMPLE)
        monkeypatch.setenv("COTF_PRICING_TTL_SECONDS", "0")
        with patch.object(pricing, "_fetch", return_value=SAMPLE) as mock_fetch:
            pricing.cost_for("gpt-5", 1000, 100)
        mock_fetch.assert_called_once()

    def test_ttl_negative_never_refreshes(self, isolated_pricing_cache, monkeypatch):
        _seed_cache(isolated_pricing_cache, SAMPLE, age_seconds=365 * 24 * 3600)
        monkeypatch.setenv("COTF_PRICING_TTL_SECONDS", "-1")
        with patch.object(pricing, "_fetch") as mock_fetch:
            cost = pricing.cost_for("gpt-5", 1000, 100)
        mock_fetch.assert_not_called()
        assert cost is not None

    def test_ttl_bogus_env_falls_back_to_default(
        self, isolated_pricing_cache, monkeypatch
    ):
        monkeypatch.setenv("COTF_PRICING_TTL_SECONDS", "not-a-number")
        _seed_cache(isolated_pricing_cache, SAMPLE)
        with patch.object(pricing, "_fetch") as mock_fetch:
            pricing.cost_for("gpt-5", 1000, 100)
        mock_fetch.assert_not_called()

    def test_write_failure_uses_in_memory_index(self, isolated_pricing_cache):
        """Read-only disk: still return correct prices for this run."""
        with (
            patch.object(pricing.Path, "mkdir", side_effect=OSError("disk full")),
            patch.object(pricing, "_fetch", return_value=SAMPLE),
        ):
            cost = pricing.cost_for("gpt-5", 1000, 100)
        assert cost is not None


# ---------------------------------------------------------------------------
# memo (avoid re-parsing / re-indexing on every call)
# ---------------------------------------------------------------------------


class TestMemo:
    def test_repeated_calls_avoid_rereading_file(
        self, isolated_pricing_cache, monkeypatch
    ):
        _seed_cache(isolated_pricing_cache, SAMPLE)
        original = json.loads
        call_count = {"n": 0}

        def counting_loads(*a, **kw):
            call_count["n"] += 1
            return original(*a, **kw)

        monkeypatch.setattr(pricing.json, "loads", counting_loads)
        for _ in range(5):
            pricing.cost_for("gpt-5", 1000, 100)
        assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# Cached prompt tokens
# ---------------------------------------------------------------------------


def _cache_entry(model_id: str, **prices: str) -> dict:
    return {"id": model_id, "pricing": prices}


class TestCachePricing:
    """Cache tokens dominate a long-running chat: a thread quiet past the cache
    TTL re-establishes its whole prompt, so the turn that brings it back is
    mostly cache *writes*."""

    def _seed(self, cache, pricing_block: dict) -> None:
        _seed_cache(cache, _payload(_cache_entry("vendor/m", **pricing_block)))

    def test_cache_reads_use_their_own_rate(self, isolated_pricing_cache):
        self._seed(
            isolated_pricing_cache,
            {"prompt": "1e-06", "completion": "2e-06", "input_cache_read": "1e-07"},
        )
        with patch.object(pricing, "_fetch", return_value=None):
            cost = pricing.cost_for("m", 0, 0, cache_read_tokens=1_000_000)
        assert cost == pytest.approx(0.1), "1e-07 * 1e6, not the 1e-06 prompt rate"

    def test_cache_writes_use_their_own_rate(self, isolated_pricing_cache):
        self._seed(
            isolated_pricing_cache,
            {"prompt": "1e-06", "completion": "2e-06", "input_cache_write": "1.25e-06"},
        )
        with patch.object(pricing, "_fetch", return_value=None):
            cost = pricing.cost_for("m", 0, 0, cache_write_tokens=1_000_000)
        assert cost == pytest.approx(1.25)

    def test_an_unpublished_cache_rate_falls_back_to_prompt(
        self, isolated_pricing_cache
    ):
        """Most of the registry publishes no write rate, and for those a cache
        write really is billed as ordinary input. Falling back to zero would make
        the biggest term on a cold thread free."""
        self._seed(isolated_pricing_cache, {"prompt": "1e-06", "completion": "2e-06"})
        with patch.object(pricing, "_fetch", return_value=None):
            cost = pricing.cost_for("m", 0, 0, cache_write_tokens=1_000_000)
        assert cost == pytest.approx(1.0)

    def test_a_published_zero_cache_rate_is_honoured_as_free(
        self, isolated_pricing_cache
    ):
        """Some providers really do serve cache reads free; overwriting that with
        a fallback would invent a charge."""
        self._seed(
            isolated_pricing_cache,
            {"prompt": "1e-06", "completion": "2e-06", "input_cache_read": "0"},
        )
        with patch.object(pricing, "_fetch", return_value=None):
            # Reads free, so only the output should cost anything.
            cost = pricing.cost_for("m", 0, 10, cache_read_tokens=1_000_000)
        assert cost == pytest.approx(10 * 2e-06)

    def test_a_corrupt_cache_rate_falls_back_instead_of_dropping_the_model(
        self, isolated_pricing_cache
    ):
        """Unlike a corrupt prompt rate: losing the whole entry over a field we
        only just started reading would be a regression."""
        self._seed(
            isolated_pricing_cache,
            {"prompt": "1e-06", "completion": "2e-06", "input_cache_read": "-5"},
        )
        with patch.object(pricing, "_fetch", return_value=None):
            cost = pricing.cost_for("m", 0, 0, cache_read_tokens=1_000_000)
        assert cost == pytest.approx(1.0), "falls back to the prompt rate"

    def test_the_four_buckets_are_summed_independently(self, isolated_pricing_cache):
        self._seed(
            isolated_pricing_cache,
            {
                "prompt": "1e-06",
                "completion": "2e-06",
                "input_cache_read": "1e-07",
                "input_cache_write": "1.25e-06",
            },
        )
        with patch.object(pricing, "_fetch", return_value=None):
            cost = pricing.cost_for("m", 100, 200, 300, 400)
        expected = 100 * 1e-06 + 200 * 2e-06 + 300 * 1e-07 + 400 * 1.25e-06
        assert cost == pytest.approx(expected)

    def test_negative_cache_counts_are_rejected(self, isolated_pricing_cache):
        self._seed(isolated_pricing_cache, {"prompt": "1e-06", "completion": "2e-06"})
        with patch.object(pricing, "_fetch", return_value=None):
            assert pricing.cost_for("m", 1, 1, -1, 0) is None
            assert pricing.cost_for("m", 1, 1, 0, -1) is None


class TestCodexBillingUnchanged:
    """codex reports no cache tokens at all, so it must bill exactly as before.
    The cache arguments default to 0 to guarantee that."""

    def test_a_three_arg_call_ignores_cache_rates_entirely(
        self, isolated_pricing_cache
    ):
        _seed_cache(
            isolated_pricing_cache,
            _payload(
                _cache_entry(
                    "vendor/m",
                    prompt="1e-06",
                    completion="2e-06",
                    input_cache_read="9e-05",
                    input_cache_write="9e-05",
                )
            ),
        )
        with patch.object(pricing, "_fetch", return_value=None):
            cost = pricing.cost_for("m", 1000, 100)
        assert cost == pytest.approx(1000 * 1e-06 + 100 * 2e-06)
