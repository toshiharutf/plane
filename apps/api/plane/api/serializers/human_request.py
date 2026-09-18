# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Third party imports
from rest_framework import serializers

# Module imports
from plane.db.models import HumanRequest
from plane.utils.human_request import answer_note

from .base import BaseSerializer


class HumanRequestSerializer(BaseSerializer):
    """
    A question or approval a bot asks a human about a work item, and the answer.

    Only ``kind`` (``approval`` or ``question``) and ``question`` are written by
    the caller. ``decision`` is empty while the request is open, then ``accept``
    or ``deny`` for an approval and ``answered`` for a question. ``note`` is the
    approval answer without its leading yes/accept or no/deny word.
    """

    note = serializers.SerializerMethodField()
    is_open = serializers.BooleanField(read_only=True)

    class Meta:
        model = HumanRequest
        fields = [
            "id",
            "issue",
            "project",
            "workspace",
            "kind",
            "question",
            "requested_by",
            "requested_at",
            "state_before",
            "is_open",
            "answer",
            "decision",
            "note",
            "resolved_by",
            "resolved_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "issue",
            "project",
            "workspace",
            "requested_by",
            "requested_at",
            "state_before",
            "answer",
            "decision",
            "resolved_by",
            "resolved_at",
            "created_at",
            "updated_at",
        ]

    def get_note(self, obj) -> str:
        return answer_note(obj.kind, obj.answer)

    def validate_question(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("The question text is required")
        return value
