# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Third party imports
from rest_framework import serializers

# Module imports
from plane.db.models import AIUsageRecord

from .base import BaseSerializer


class WorkItemAIUsageSerializer(BaseSerializer):
    """
    Token usage reported for one AI model on a work item, stored as an ``AIUsageRecord``.

    Token counts must be non-negative integers. The legacy fields keep working:
    ``cache_creation_input_tokens`` (input cache miss) is stored as 1h cache
    writes with ``split_unknown`` set unless ``cache_write_5m_tokens`` or
    ``cache_write_1h_tokens`` is sent, and ``cache_read_input_tokens`` (input
    cache hit) is ``cache_read_tokens``. Both are returned as the sum of the
    new fields. ``api_cost_usd`` and ``price_version`` are computed on save.
    """

    cache_creation_input_tokens = serializers.IntegerField(min_value=0, required=False, write_only=True)
    cache_read_input_tokens = serializers.IntegerField(min_value=0, required=False, write_only=True)

    class Meta:
        model = AIUsageRecord
        fields = [
            "id",
            "issue",
            "issue_title",
            "project",
            "workspace",
            "model",
            "effort",
            "duration_seconds",
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "cache_write_5m_tokens",
            "cache_write_1h_tokens",
            "cache_read_tokens",
            "usage_5h_pct",
            "usage_weekly_pct",
            "api_cost_usd",
            "price_version",
            "split_unknown",
            "session_id",
            "external_source",
            "external_id",
            "created_by",
            "updated_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "issue",
            "issue_title",
            "project",
            "workspace",
            "api_cost_usd",
            "price_version",
            "split_unknown",
            "created_by",
            "updated_by",
            "created_at",
            "updated_at",
        ]

    def validate(self, attrs):
        cache_creation = attrs.pop("cache_creation_input_tokens", None)
        cache_read = attrs.pop("cache_read_input_tokens", None)
        if "cache_write_5m_tokens" in attrs or "cache_write_1h_tokens" in attrs:
            attrs["split_unknown"] = False
        elif cache_creation is not None:
            attrs["cache_write_5m_tokens"] = 0
            attrs["cache_write_1h_tokens"] = cache_creation
            attrs["split_unknown"] = True
        if cache_read is not None and "cache_read_tokens" not in attrs:
            attrs["cache_read_tokens"] = cache_read
        return attrs

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["cache_creation_input_tokens"] = instance.cache_write_5m_tokens + instance.cache_write_1h_tokens
        data["cache_read_input_tokens"] = instance.cache_read_tokens
        return data


class AIUsageHistorySerializer(BaseSerializer):
    """One AI session on a completed work item, with its API price."""

    project_identifier = serializers.CharField(source="project.identifier", read_only=True)
    sequence_id = serializers.IntegerField(source="issue.sequence_id", read_only=True, default=None)

    class Meta:
        model = AIUsageRecord
        fields = [
            "id",
            "issue",
            "issue_title",
            "project",
            "project_identifier",
            "sequence_id",
            "session_id",
            "model",
            "effort",
            "duration_seconds",
            "input_tokens",
            "cache_write_5m_tokens",
            "cache_write_1h_tokens",
            "cache_read_tokens",
            "output_tokens",
            "usage_5h_pct",
            "usage_weekly_pct",
            "api_cost_usd",
            "price_version",
            "split_unknown",
            "created_at",
        ]
        read_only_fields = fields
