# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import date, datetime, timedelta, timezone as dt_timezone

import pytest
from django.utils import timezone

from plane.db.models import Issue, Project, State


@pytest.fixture
def project(db, workspace, create_user):
    return Project.objects.create(
        name="Datetime Project",
        identifier="DTP",
        workspace=workspace,
        created_by=create_user,
        timezone="Asia/Tokyo",
    )


@pytest.fixture
def states(project):
    def make(name, group, default=False):
        return State.objects.create(
            name=name, project=project, workspace=project.workspace, group=group, default=default
        )

    return {
        "backlog": make("Backlog", "backlog", default=True),
        "started": make("In Progress", "started"),
        "review": make("In Review", "started"),
        "completed": make("Done", "completed"),
    }


@pytest.fixture
def issue(project, states):
    return Issue.objects.create(name="Item", project=project, workspace=project.workspace, state=states["backlog"])


@pytest.mark.unit
class TestIssueStartTargetDatetimes:
    def test_new_issue_has_no_datetimes(self, issue):
        issue.refresh_from_db()
        assert issue.start_datetime is None
        assert issue.target_datetime is None

    def test_datetime_sets_local_date(self, issue):
        # 2026-01-01 20:30:15 UTC is 2026-01-02 in Asia/Tokyo
        issue.start_datetime = datetime(2026, 1, 1, 20, 30, 15, tzinfo=dt_timezone.utc)
        issue.save()
        issue.refresh_from_db()
        assert issue.start_date == date(2026, 1, 2)
        assert issue.start_datetime == datetime(2026, 1, 1, 20, 30, 15, tzinfo=dt_timezone.utc)

    def test_date_only_change_keeps_local_time(self, issue):
        issue.start_datetime = datetime(2026, 1, 1, 1, 2, 3, tzinfo=dt_timezone.utc)  # 10:02:03 Tokyo
        issue.save()
        issue = Issue.objects.get(pk=issue.pk)
        issue.start_date = date(2026, 3, 10)
        issue.save()
        issue.refresh_from_db()
        assert issue.start_datetime == datetime(2026, 3, 10, 1, 2, 3, tzinfo=dt_timezone.utc)

    def test_date_without_datetime_uses_midnight(self, issue):
        issue.target_date = "2026-05-05"
        issue.save()
        issue.refresh_from_db()
        # 00:00 Tokyo is 15:00 UTC on the previous day
        assert issue.target_datetime == datetime(2026, 5, 4, 15, 0, tzinfo=dt_timezone.utc)

    def test_clearing_date_clears_datetime(self, issue):
        issue.target_datetime = timezone.now() + timedelta(days=3)
        issue.save()
        issue = Issue.objects.get(pk=issue.pk)
        issue.target_date = None
        issue.save()
        issue.refresh_from_db()
        assert issue.target_datetime is None

    def test_started_state_sets_start(self, issue, states):
        before = timezone.now().replace(microsecond=0)
        issue.state = states["started"]
        issue.save()
        issue.refresh_from_db()
        assert issue.start_datetime is not None and issue.start_datetime >= before
        assert issue.start_date == issue.start_datetime.astimezone(
            __import__("zoneinfo").ZoneInfo("Asia/Tokyo")
        ).date()
        assert issue.target_datetime is None

    def test_moving_between_started_states_keeps_start(self, issue, states):
        issue.state = states["started"]
        issue.save()
        first_start = Issue.objects.get(pk=issue.pk).start_datetime
        issue = Issue.objects.get(pk=issue.pk)
        issue.start_datetime = first_start - timedelta(hours=2)
        issue.save()
        issue = Issue.objects.get(pk=issue.pk)
        issue.state = states["review"]
        issue.save()
        issue.refresh_from_db()
        assert issue.start_datetime == first_start - timedelta(hours=2)

    def test_completed_state_sets_target(self, issue, states):
        issue.state = states["started"]
        issue.save()
        issue = Issue.objects.get(pk=issue.pk)
        issue.state = states["completed"]
        issue.save()
        issue.refresh_from_db()
        assert issue.target_datetime is not None
        assert issue.start_datetime <= issue.target_datetime
        assert issue.completed_at is not None

    def test_reopen_keeps_values(self, issue, states):
        issue.state = states["completed"]
        issue.save()
        target = Issue.objects.get(pk=issue.pk).target_datetime
        issue = Issue.objects.get(pk=issue.pk)
        issue.state = states["backlog"]
        issue.save()
        issue.refresh_from_db()
        assert issue.target_datetime == target

    def test_started_clears_past_target(self, issue, states):
        issue.target_date = date(2020, 1, 1)
        issue.save()
        issue = Issue.objects.get(pk=issue.pk)
        issue.state = states["started"]
        issue.save()
        issue.refresh_from_db()
        assert issue.start_datetime is not None
        assert issue.target_date is None and issue.target_datetime is None

    def test_explicit_start_with_state_change_wins(self, issue, states):
        explicit = datetime(2026, 2, 2, 8, 0, tzinfo=dt_timezone.utc)
        issue.state = states["started"]
        issue.start_datetime = explicit
        issue.save()
        issue.refresh_from_db()
        assert issue.start_datetime == explicit

    def test_create_in_started_state_sets_start(self, project, states):
        created = Issue.objects.create(
            name="Started", project=project, workspace=project.workspace, state=states["started"]
        )
        created.refresh_from_db()
        assert created.start_datetime is not None and created.start_date is not None

    def test_update_fields_include_synced_fields(self, issue, states):
        issue.state = states["started"]
        issue.save(update_fields=["state"])
        issue.refresh_from_db()
        assert issue.start_datetime is not None and issue.start_date is not None
