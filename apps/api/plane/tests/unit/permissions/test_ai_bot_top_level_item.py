# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import uuid

import pytest
from django.http import QueryDict

from plane.app.permissions.ai_bot import (
    HUMAN_ITEM_PREFIX,
    OWN_ITEM_PATCH_FIELDS,
    _is_human_ticket_name,
    _requested_assignees,
    _requests_only_self_assigned,
    _requests_top_level_item,
    _requests_unassigned,
)


def _form(pairs):
    """Build the ``QueryDict`` a form or multipart request body is parsed into."""
    data = QueryDict(mutable=True)
    for key, value in pairs:
        data.appendlist(key, value)
    return data


@pytest.mark.unit
class TestRequestsOnlySelfAssigned:
    """Test which request bodies leave a new work item assigned to the bot alone"""

    def test_missing_assignees_is_accepted(self):
        bot_id = uuid.uuid4()
        assert _requests_only_self_assigned({"name": "[Release] - develop"}, bot_id) is True
        assert _requests_only_self_assigned(_form([("name", "Planned")]), bot_id) is True

    def test_the_bot_alone_is_accepted(self):
        bot_id = uuid.uuid4()
        # The id is compared as text, so a UUID object and its string are the same bot.
        assert _requests_only_self_assigned({"assignees": [str(bot_id)]}, bot_id) is True
        assert _requests_only_self_assigned({"assignees": [bot_id]}, str(bot_id)) is True
        assert _requests_only_self_assigned({"assignees": (str(bot_id),)}, bot_id) is True
        assert _requests_only_self_assigned(_form([("assignees", str(bot_id))]), bot_id) is True
        # Blank form entries next to the bot are ignored.
        assert _requests_only_self_assigned(_form([("assignees", ""), ("assignees", str(bot_id))]), bot_id) is True

    def test_other_additional_or_malformed_assignees_are_rejected(self):
        bot_id = uuid.uuid4()
        other_id = str(uuid.uuid4())
        for assignees in (
            [other_id],
            [str(bot_id), other_id],
            [str(bot_id), str(bot_id)],
            [],
            None,
            str(bot_id),
            {"id": str(bot_id)},
        ):
            assert _requests_only_self_assigned({"assignees": assignees}, bot_id) is False, assignees
        form = _form([("assignees", str(bot_id)), ("assignees", other_id)])
        assert _requests_only_self_assigned(form, bot_id) is False

    def test_requested_assignees(self):
        bot_id = str(uuid.uuid4())
        assert _requested_assignees({}) is None
        assert _requested_assignees({"assignees": []}) == []
        assert _requested_assignees({"assignees": [bot_id, ""]}) == [bot_id]
        assert _requested_assignees({"assignees": bot_id}) is False
        assert _requested_assignees(_form([("assignees", "")])) == []


@pytest.mark.unit
class TestRequestsTopLevelItem:
    """Test which top-level work items a bot may create"""

    def test_unassigned_items_are_accepted_for_any_name(self):
        bot_id = uuid.uuid4()
        for name in ("Planned", "[Human] approve release", "[Release] - develop", None):
            assert _requests_top_level_item({"name": name, "assignees": []}, bot_id) is True, name

    def test_items_for_the_bot_are_accepted_unless_they_are_human_tickets(self):
        bot_id = uuid.uuid4()
        for name in ("Planned", "[Release] - develop", "[release] develop", "Rogue [Human] note", None):
            assert _requests_top_level_item({"name": name}, bot_id) is True, name
            assert _requests_top_level_item({"name": name, "assignees": [str(bot_id)]}, bot_id) is True, name
        for name in ("[Human] approve release", "[human] approve", "  [HUMAN] approve"):
            assert _requests_top_level_item({"name": name}, bot_id) is False, name
            assert _requests_top_level_item({"name": name, "assignees": [str(bot_id)]}, bot_id) is False, name
            assert _requests_top_level_item(_form([("name", name)]), bot_id) is False, name

    def test_items_for_others_are_rejected(self):
        bot_id = uuid.uuid4()
        other_id = str(uuid.uuid4())
        assert _requests_top_level_item({"name": "Planned", "assignees": [other_id]}, bot_id) is False
        assert _requests_top_level_item({"name": "Planned", "assignees": [str(bot_id), other_id]}, bot_id) is False
        assert _requests_top_level_item({"name": "Planned", "assignees": None}, bot_id) is False

    def test_human_ticket_name(self):
        assert HUMAN_ITEM_PREFIX == "[Human]"
        assert _is_human_ticket_name("[Human] Approve release of develop abc123") is True
        assert _is_human_ticket_name("Human: decide") is False
        assert _is_human_ticket_name(None) is False
        assert _is_human_ticket_name(42) is False

    def test_own_item_patch_fields(self):
        assert OWN_ITEM_PATCH_FIELDS == {
            "name",
            "description_html",
            "priority",
            "estimate_point",
            "labels",
            "state",
            "assignees",
            "start_date",
            "target_date",
            "ai_model",
            "parent",
        }


@pytest.mark.unit
class TestRequestsUnassigned:
    """Test that only an explicit empty ``assignees`` list asks for an unassigned ticket"""

    def test_explicit_empty_list(self):
        assert _requests_unassigned({"assignees": []}) is True
        assert _requests_unassigned(_form([("assignees", "")])) is True

    def test_missing_or_filled_assignees(self):
        bot_id = str(uuid.uuid4())
        assert _requests_unassigned({}) is False
        assert _requests_unassigned({"assignees": None}) is False
        assert _requests_unassigned({"assignees": [bot_id]}) is False
        assert _requests_unassigned(_form([("name", "Ticket")])) is False
        assert _requests_unassigned(_form([("assignees", bot_id)])) is False
