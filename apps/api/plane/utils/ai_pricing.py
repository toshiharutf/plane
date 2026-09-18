# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Claude API list prices, used to put a dollar value on AI usage records.

Prices are USD per million tokens (first-party Anthropic API rates). Cache
writes cost 1.25x the input price for the 5-minute TTL and 2x for the 1-hour
TTL; cache hits cost 0.1x, except where a model overrides ``cache_read``.
Bump ``PRICE_VERSION`` whenever a price changes so stored costs stay traceable.
"""

# Python imports
import re
from decimal import ROUND_HALF_UP, Decimal

PRICE_VERSION = "2026-09"

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
    name = re.sub(r"\[[^\]]*\]$", "", name)
    name = re.sub(r"^(?:[a-z]+\.)?anthropic\.", "", name)
    name = re.sub(r"-v\d+(:\d+)?$", "", name)
    name = _DATE_SUFFIX.sub("", name)
    if name and not name.startswith("claude-"):
        name = f"claude-{name}"
    return name


def model_prices(model):
    """Return the per-million-token prices for ``model`` or None when unknown."""
    entry = PRICES.get(normalize_model(model))
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
    prices = model_prices(model)
    if prices is None:
        return None, PRICE_VERSION
    tokens = {
        "input": input_tokens,
        "cache_write_5m": cache_write_5m_tokens,
        "cache_write_1h": cache_write_1h_tokens,
        "cache_read": cache_read_tokens,
        "output": output_tokens,
    }
    total = sum(Decimal(tokens[key] or 0) * prices[key] for key in tokens) / MILLION
    return total.quantize(COST_QUANTUM, rounding=ROUND_HALF_UP), PRICE_VERSION
