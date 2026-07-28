"""Model price-table lookup, backed by OpenRouter's public model registry.

We fetch `https://openrouter.ai/api/v1/models` on demand and cache it at
`~/.claude-on-the-fly/pricing/openrouter.json`. `cost_for(model, tokens_in,
tokens_out)` returns USD or None when the model is not in the table.

OpenRouter's keys are `<vendor>/<model>` (e.g. `deepseek/deepseek-v4-flash`).
We strip the vendor prefix into a flat `model -> (input, output)` index so a
plain name from our backends (e.g. `deepseek-v4-flash`) hits directly. Across
the current ~360 entries there are zero stripped-name collisions.

All failures fall through silently: pricing is a footer nicety, never a
critical path. The Response.cost field stays 0 on any miss.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

PRICING_URL = "https://openrouter.ai/api/v1/models"
CACHE_DIR = Path.home() / ".claude-on-the-fly" / "pricing"
CACHE_PATH = CACHE_DIR / "openrouter.json"
DEFAULT_TTL_SECONDS = 7 * 24 * 3600
FETCH_TIMEOUT_SECONDS = 5.0

# Trailing snapshot date in a model name: `-YYYY-MM-DD` or `-YYYYMMDD`.
_DATE_SUFFIX_RE = re.compile(r"-(\d{4}-\d{2}-\d{2}|\d{8})$")

# Module-level memo: (cache_mtime, stripped_index). Invalidated when the cache
# file's mtime changes. Index is `{stripped_name: (input_per_token, output_per_token)}`.
_memo: tuple[float | None, dict[str, tuple[float, float]] | None] = (None, None)


def _ttl_seconds() -> int:
    """TTL in seconds. 0 = always refresh, negative = never expire."""
    raw = os.environ.get("COTF_PRICING_TTL_SECONDS")
    if raw is None:
        return DEFAULT_TTL_SECONDS
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_TTL_SECONDS


def _strip_vendor(key: str) -> str:
    """`vendor/model` -> `model`. Returns key unchanged when there's no slash."""
    return key.split("/", 1)[1] if "/" in key else key


def _build_index(payload: dict) -> dict[str, tuple[float, float]]:
    """OpenRouter payload -> {stripped_name: (input_per_token, output_per_token)}.

    Prefers non-zero entries when two vendor-prefixed keys strip to the same
    name (defensive: today there are no such collisions across ~360 entries).
    """
    index: dict[str, tuple[float, float]] = {}
    raw_models = payload.get("data")
    if not isinstance(raw_models, list):
        return index
    for entry in raw_models:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        pricing_block = entry.get("pricing")
        if not isinstance(pricing_block, dict):
            continue
        prompt_raw = pricing_block.get("prompt")
        completion_raw = pricing_block.get("completion")
        try:
            input_price = float(prompt_raw) if prompt_raw is not None else 0.0
            output_price = float(completion_raw) if completion_raw is not None else 0.0
        except (TypeError, ValueError):
            continue
        if input_price < 0 or output_price < 0:
            continue
        if input_price != input_price or output_price != output_price:  # NaN
            continue
        stripped = _strip_vendor(model_id)
        existing = index.get(stripped)
        if existing is None or (
            (input_price > 0 or output_price > 0)
            and existing[0] == 0
            and existing[1] == 0
        ):
            index[stripped] = (input_price, output_price)
    return index


def _read_cache() -> tuple[dict[str, tuple[float, float]], float] | None:
    """Load + index the cache file. Returns (index, mtime) or None on failure."""
    global _memo
    try:
        mtime = CACHE_PATH.stat().st_mtime
    except OSError:
        return None
    memo_mtime, memo_index = _memo
    if memo_mtime == mtime and memo_index is not None:
        return (memo_index, mtime)
    try:
        payload = json.loads(CACHE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        logger.warning("pricing: cache at %s is unreadable", CACHE_PATH)
        return None
    if not isinstance(payload, dict):
        logger.warning("pricing: cache at %s is not a JSON object", CACHE_PATH)
        return None
    index = _build_index(payload)
    if not index:
        logger.warning("pricing: cache at %s produced an empty index", CACHE_PATH)
        return None
    _memo = (mtime, index)
    return (index, mtime)


def _write_cache(payload: dict) -> None:
    """Atomic write via temp + rename. Logs and continues on failure."""
    global _memo
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(CACHE_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(CACHE_PATH)
        index = _build_index(payload)
        try:
            new_mtime = CACHE_PATH.stat().st_mtime
        except OSError:
            new_mtime = time.time()
        _memo = (new_mtime, index)
    except OSError:
        logger.warning("pricing: failed to write cache to %s", CACHE_PATH)
        # Keep an in-memory index so this run still gets pricing.
        index = _build_index(payload)
        _memo = (time.time(), index)


def _fetch() -> dict | None:
    """One-shot HTTP fetch of the OpenRouter model list. Returns None on failure."""
    try:
        req = urllib.request.Request(
            PRICING_URL,
            headers={"User-Agent": "claude-on-the-fly"},
        )
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.debug("pricing: fetch failed: %s", exc)
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("pricing: fetched JSON is invalid: %s", exc)
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        logger.warning("pricing: fetched payload has unexpected shape")
        return None
    return payload


def _get_index() -> dict[str, tuple[float, float]] | None:
    """Resolve the stripped-name price index. Refreshes on miss/stale."""
    cached = _read_cache()
    ttl = _ttl_seconds()

    if cached is not None:
        index, mtime = cached
        fresh_enough = ttl < 0 or (time.time() - mtime) <= ttl
        if fresh_enough:
            return index

    fresh = _fetch()
    if fresh is not None:
        _write_cache(fresh)
        _memo_mtime, memo_index = _memo
        if memo_index is not None:
            return memo_index

    if cached is not None:
        index, mtime = cached
        logger.warning(
            "pricing: fetch failed, using stale cache (age=%.0fs)",
            time.time() - mtime,
        )
        return index

    return None


def _candidates(model: str) -> list[str]:
    """Lookup keys to try in the stripped-name index, in order of specificity.

    Generates the cross-product of (with `:cloud`, without `:cloud`) and
    (with snapshot date suffix, without), preserving insertion order and
    deduping. `:cloud` is stripped first so `foo-YYYY-MM-DD:cloud` can fold
    down to `foo` via the date regex (which is anchored to end of string).
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        if name and name not in seen:
            seen.add(name)
            out.append(name)

    bases = [model]
    if model.endswith(":cloud"):
        bases.append(model[: -len(":cloud")])
    for base in bases:
        add(base)
        date_stripped = _DATE_SUFFIX_RE.sub("", base)
        if date_stripped != base:
            add(date_stripped)
    return out


def cost_for(model: str, tokens_in: int, tokens_out: int) -> float | None:
    """Compute USD cost for one turn, or None when pricing isn't available.

    Never raises — pricing is decorative. Caller should coalesce to 0.
    """
    if not model or tokens_in < 0 or tokens_out < 0:
        return None
    try:
        index = _get_index()
    except Exception:
        logger.exception("pricing: unexpected error resolving price index")
        return None
    if index is None:
        return None
    entry = None
    for candidate in _candidates(model):
        entry = index.get(candidate)
        if entry is not None:
            break
    if entry is None:
        return None
    input_price, output_price = entry
    cost = tokens_in * input_price + tokens_out * output_price
    if cost <= 0:
        return None
    return cost
