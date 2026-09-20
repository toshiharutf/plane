# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Versioned repository price snapshots for recording API-equivalent usage.

Prices are USD per million tokens (first-party Anthropic API rates). Cache
writes cost 1.25x the input price for the 5-minute TTL and 2x for the 1-hour
TTL; cache hits cost 0.1x, except where a model overrides ``cache_read``.
Bump ``PRICE_VERSION`` whenever a price changes so stored costs stay traceable.
Codex estimates use the same retained JSON snapshot as the local skills. These
are historical budgeting rates, not a live price quote or subscription charge.
"""

# Python imports
import re
import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

PRICE_VERSION = "2026-09"
CODEX_SNAPSHOT = json.loads(Path(__file__).with_name("model-pricing.json").read_text())

CACHE_WRITE_5M_MULTIPLIER = Decimal("1.25")
CACHE_WRITE_1H_MULTIPLIER = Decimal("2")
CACHE_READ_MULTIPLIER = Decimal("0.1")

MILLION = Decimal(1_000_000)
COST_QUANTUM = Decimal("0.000001")

# Model id (without date suffix) -> USD per million tokens.
PRICES = {
    "claude-fable-5-1": {"input": "10", "output": "50", "cache_read": "0.25"},
    "claude-mythos-5-1": {"input": "10", "output": "50"},
    "claude-fable-5": {"input": "10", "output": "50"},
    "claude-mythos-5": {"input": "10", "output": "50"},
    "claude-opus-5": {"input": "5", "output": "25"},
    "claude-opus-4-8": {"input": "5", "output": "25"},
    "claude-opus-4-7": {"input": "5", "output": "25"},
    "claude-opus-4-6": {"input": "5", "output": "25"},
    "claude-opus-4-5": {"input": "5", "output": "25"},
    "claude-opus-4-1": {"input": "15", "output": "75"},
    "claude-opus-4": {"input": "15", "output": "75"},
    "claude-sonnet-5": {"input": "2", "output": "10"},
    "claude-sonnet-4-6": {"input": "3", "output": "15"},
    "claude-sonnet-4-5": {"input": "3", "output": "15"},
    "claude-sonnet-4": {"input": "3", "output": "15"},
    "claude-haiku-4-5": {"input": "1", "output": "5"},
}

_DATE_SUFFIX = re.compile(r"(-|@)\d{8}$")


def normalize_model(model):
    """Map a reported model name to a ``PRICES`` key.

    Accepts provider prefixes (``anthropic.``, ``us.anthropic.``), context
    suffixes (``[1m]``), date snapshots (``-20250929``, ``@20251101``) and
    short names without the ``claude-`` prefix (``opus-5``).
    """
    name = (model or "").strip().lower()
    if name.startswith("openai/"):
        name = name.removeprefix("openai/")
    if name.startswith("gpt-"):
        name = _DATE_SUFFIX.sub("", name)
        return re.sub(r"-(low|medium|high|xhigh|max|ultra)$", "", name)
    name = re.sub(r"\[[^\]]*\]$", "", name)
    name = re.sub(r"^(?:[a-z]+\.)?anthropic\.", "", name)
    name = re.sub(r"-v\d+(:\d+)?$", "", name)
    name = _DATE_SUFFIX.sub("", name)
    if name and not name.startswith("claude-"):
        name = f"claude-{name}"
    return name


def model_prices(model, *, input_tokens=0, cache_read_tokens=0):
    """Return the per-million-token prices for ``model`` or None when unknown."""
    normalized = normalize_model(model)
    codex = CODEX_SNAPSHOT["models"].get(normalized)
    if codex is not None:
        # The common ingestion schema uses uncached input and cache reads as
        # disjoint categories. Raw Codex input includes cache reads; normalize
        # that on the reporting side before submitting either usage API.
        multipliers = CODEX_SNAPSHOT["long_context"]
        long_context = input_tokens + cache_read_tokens > multipliers["input_threshold_exclusive"]
        return {
            "input": Decimal(str(codex["input"]))
            * (Decimal(str(multipliers["input_multiplier"])) if long_context else 1),
            "cache_read": Decimal(str(codex["cached_input"]))
            * (Decimal(str(multipliers["cached_input_multiplier"])) if long_context else 1),
            "output": Decimal(str(codex["output"]))
            * (Decimal(str(multipliers["output_multiplier"])) if long_context else 1),
            "cache_write_5m": Decimal(0),
            "cache_write_1h": Decimal(0),
        }
    entry = PRICES.get(normalized)
    if entry is None:
        return None
    input_price = Decimal(entry["input"])
    return {
        "input": input_price,
        "cache_write_5m": input_price * CACHE_WRITE_5M_MULTIPLIER,
        "cache_write_1h": input_price * CACHE_WRITE_1H_MULTIPLIER,
        "cache_read": Decimal(entry["cache_read"]) if "cache_read" in entry else input_price * CACHE_READ_MULTIPLIER,
        "output": Decimal(entry["output"]),
    }


def compute_cost(
    model,
    input_tokens=0,
    cache_write_5m_tokens=0,
    cache_write_1h_tokens=0,
    cache_read_tokens=0,
    output_tokens=0,
):
    """Return ``(api_cost_usd, PRICE_VERSION)``; the cost is None for an unknown model."""
    codex = normalize_model(model) in CODEX_SNAPSHOT["models"]
    version = CODEX_SNAPSHOT["price_version"] if codex else PRICE_VERSION
    prices = model_prices(model, input_tokens=input_tokens, cache_read_tokens=cache_read_tokens)
    if prices is None:
        return None, version
    if codex and (cache_write_5m_tokens or cache_write_1h_tokens):
        # The retained snapshot defines no cache-write price for this provider.
        return None, version
    tokens = {
        "input": input_tokens,
        "cache_write_5m": cache_write_5m_tokens,
        "cache_write_1h": cache_write_1h_tokens,
        "cache_read": cache_read_tokens,
        "output": output_tokens,
    }
    total = sum(Decimal(tokens[key] or 0) * prices[key] for key in tokens) / MILLION
    return total.quantize(COST_QUANTUM, rounding=ROUND_HALF_UP), version
