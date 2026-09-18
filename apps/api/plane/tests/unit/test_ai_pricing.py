# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from decimal import Decimal

import pytest

from plane.utils.ai_pricing import PRICE_VERSION, compute_cost, model_prices, normalize_model


@pytest.mark.unit
class TestAIPricing:
    def test_ac2_opus_5_input_and_output(self):
        cost, version = compute_cost("claude-opus-5", input_tokens=1_000_000, output_tokens=100_000)
        assert cost == Decimal("7.50")
        assert version == PRICE_VERSION == "2026-09"

    def test_cache_multipliers(self):
        prices = model_prices("claude-opus-5")
        assert prices["cache_write_5m"] == Decimal("6.25")
        assert prices["cache_write_1h"] == Decimal("10")
        assert prices["cache_read"] == Decimal("0.5")
        cost, _ = compute_cost(
            "claude-opus-5",
            cache_write_5m_tokens=1_000_000,
            cache_write_1h_tokens=1_000_000,
            cache_read_tokens=1_000_000,
        )
        assert cost == Decimal("16.75")

    def test_fable_5_1_cache_read_override(self):
        assert model_prices("claude-fable-5-1")["cache_read"] == Decimal("0.25")

    @pytest.mark.parametrize(
        "raw",
        [
            "claude-sonnet-4-5-20250929",
            "claude-sonnet-4-5[1m]",
            "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "claude-sonnet-4-5@20250929",
            "sonnet-4-5",
            " Claude-Sonnet-4-5 ",
        ],
    )
    def test_normalize_model_variants(self, raw):
        assert normalize_model(raw) == "claude-sonnet-4-5"

    def test_unknown_model_has_no_cost(self):
        assert compute_cost("gpt-5", input_tokens=10) == (None, PRICE_VERSION)
        assert compute_cost("", input_tokens=10) == (None, PRICE_VERSION)

    def test_small_costs_round_to_micro_dollars(self):
        cost, _ = compute_cost("claude-haiku-4-5", input_tokens=1, output_tokens=1)
        assert cost == Decimal("0.000006")
