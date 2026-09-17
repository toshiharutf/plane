# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import uuid

import pytest
from django.http import QueryDict

from plane.app.permissions.ai_bot import (
    RELEASE_ITEM_PREFIX,
    _requests_own_release_item,
    _requests_unassigned,
)


def _form(pairs):
    """Build the ``QueryDict`` a form or multipart request body is parsed into."""
    data = QueryDict(mutable=True)
    for key, value in pairs:
        data.appendlist(key, value)
    return data


@pytest.mark.unit
class TestRequestsOwnReleaseItem:
    """Test which request bodies count as a ``[Release] - <branch>`` item of the bot itself"""

    def test_release_name_without_assignees_is_accepted(self):
        bot_id = uuid.uuid4()
        assert _requests_own_release_item({"name": "[Release] - develop"}, bot_id) is True
        assert _requests_own_release_item(_form([("name", "[Release] - develop")]), bot_id) is True

    def test_release_name_assigned_to_the_bot_alone_is_accepted(self):
        bot_id = uuid.uuid4()
        # The id is compared as text, so a UUID object and its string are the same bot.
        assert _requests_own_release_item({"name": "[Release] - main", "assignees": [str(bot_id)]}, bot_id) is True
        assert _requests_own_release_item({"name": "[Release] - main", "assignees": [bot_id]}, str(bot_id)) is True
        assert _requests_own_release_item({"name": "[Release] - main", "assignees": (str(bot_id),)}, bot_id) is True
        form = _form([("name", "[Release] - main"), ("assignees", str(bot_id))])
        assert _requests_own_release_item(form, bot_id) is True

    def test_other_or_additional_assignees_are_rejected(self):
        bot_id = uuid.uuid4()
        other_id = str(uuid.uuid4())
        name = "[Release] - develop"
        assert _requests_own_release_item({"name": name, "assignees": [other_id]}, bot_id) is False
        assert _requests_own_release_item({"name": name, "assignees": [str(bot_id), other_id]}, bot_id) is False
        assert _requests_own_release_item({"name": name, "assignees": [str(bot_id), str(bot_id)]}, bot_id) is False
        form = _form([("name", name), ("assignees", str(bot_id)), ("assignees", other_id)])
        assert _requests_own_release_item(form, bot_id) is False

    def test_empty_or_malformed_assignees_are_rejected(self):
        bot_id = uuid.uuid4()
        name = "[Release] - develop"
        # An empty list is the unassigned ticket for humans, not the bot's release item.
        assert _requests_own_release_item({"name": name, "assignees": []}, bot_id) is False
        assert _requests_own_release_item({"name": name, "assignees": None}, bot_id) is False
        assert _requests_own_release_item({"name": name, "assignees": str(bot_id)}, bot_id) is False
        assert _requests_own_release_item({"name": name, "assignees": {"id": str(bot_id)}}, bot_id) is False
        assert _requests_own_release_item(_form([("name", name), ("assignees", "")]), bot_id) is False

    def test_blank_entries_next_to_the_bot_are_ignored(self):
        bot_id = uuid.uuid4()
        form = _form([("name", "[Release] - develop"), ("assignees", ""), ("assignees", str(bot_id))])
        assert _requests_own_release_item(form, bot_id) is True

    def test_other_names_are_rejected(self):
        bot_id = uuid.uuid4()
        names = [
            None,
            "",
            "Release - develop",
            "[release] - develop",
            "[Release] develop",
            "[Release]- develop",
            " [Release] - develop",
            "Rogue [Release] - develop",
            ["[Release] - develop"],
            42,
        ]
        for name in names:
            assert _requests_own_release_item({"name": name}, bot_id) is False, name
            assert _requests_own_release_item({"name": name, "assignees": [str(bot_id)]}, bot_id) is False, name
        assert _requests_own_release_item({}, bot_id) is False
        assert _requests_own_release_item({"assignees": [str(bot_id)]}, bot_id) is False

    def test_prefix_is_the_name_the_orchestrator_uses(self):
        assert RELEASE_ITEM_PREFIX == "[Release] - "
        assert "[Release] - develop".startswith(RELEASE_ITEM_PREFIX)


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
