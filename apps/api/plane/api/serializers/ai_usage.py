# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Module imports
from plane.db.models import WorkItemAIUsage

from .base import BaseSerializer


class WorkItemAIUsageSerializer(BaseSerializer):
    """
    Token usage reported for one AI model on a work item.

    Token counts must be non-negative integers. ``cache_creation_input_tokens``
    is the input cache miss (write) and ``cache_read_input_tokens`` the input
    cache hit.
    """

    class Meta:
        model = WorkItemAIUsage
        fields = [
            "id",
            "issue",
            "project",
            "workspace",
            "model",
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
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
            "project",
            "workspace",
            "created_by",
            "updated_by",
            "created_at",
            "updated_at",
        ]
