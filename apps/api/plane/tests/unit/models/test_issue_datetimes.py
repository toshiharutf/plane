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
        assert issue.start_date == issue.start_datetime.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Tokyo")).date()
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

    def test_completed_clears_future_start(self, issue, states):
        issue.start_datetime = timezone.now() + timedelta(days=5)
        issue.save()
        issue = Issue.objects.get(pk=issue.pk)
        issue.state = states["completed"]
        issue.save()
        issue.refresh_from_db()
        assert issue.target_datetime is not None and issue.target_date is not None
        assert issue.start_date is None and issue.start_datetime is None

    def test_completed_keeps_earlier_start(self, issue, states):
        start = timezone.now().replace(microsecond=0) - timedelta(days=2)
        issue.start_datetime = start
        issue.save()
        issue = Issue.objects.get(pk=issue.pk)
        issue.state = states["completed"]
        issue.save()
        issue.refresh_from_db()
        assert issue.start_datetime == start
        assert issue.target_datetime >= start

    def test_explicit_target_with_completed_state_wins(self, issue, states):
        explicit = datetime(2026, 2, 3, 18, 45, 10, tzinfo=dt_timezone.utc)
        issue.state = states["completed"]
        issue.target_datetime = explicit
        issue.save()
        issue.refresh_from_db()
        assert issue.target_datetime == explicit
        # 18:45 UTC is already the next day in Asia/Tokyo
        assert issue.target_date == date(2026, 2, 4)

    def test_started_keeps_future_target(self, issue, states):
        target = timezone.now().replace(microsecond=0) + timedelta(days=10)
        issue.target_datetime = target
        issue.save()
        issue = Issue.objects.get(pk=issue.pk)
        issue.state = states["started"]
        issue.save()
        issue.refresh_from_db()
        assert issue.start_datetime is not None
        assert issue.target_datetime == target

    def test_naive_datetime_string_is_read_as_utc(self, issue):
        issue.start_datetime = "2026-07-01T23:10:05"
        issue.save()
        issue.refresh_from_db()
        assert issue.start_datetime == datetime(2026, 7, 1, 23, 10, 5, tzinfo=dt_timezone.utc)
        assert issue.start_date == date(2026, 7, 2)

    def test_clearing_datetime_clears_date(self, issue):
        issue.start_datetime = datetime(2026, 1, 1, 1, 2, 3, tzinfo=dt_timezone.utc)
        issue.save()
        issue = Issue.objects.get(pk=issue.pk)
        issue.start_datetime = None
        issue.save()
        issue.refresh_from_db()
        assert issue.start_date is None and issue.start_datetime is None

    def test_row_with_date_only_gets_datetime_on_next_save(self, issue):
        # Rows written without save() (bulk_create, imports) can carry a date without a datetime
        Issue.objects.filter(pk=issue.pk).update(target_date=date(2026, 8, 9), target_datetime=None)
        issue = Issue.objects.get(pk=issue.pk)
        issue.name = "Renamed"
        issue.save()
        issue.refresh_from_db()
        assert issue.target_date == date(2026, 8, 9)
        assert issue.target_datetime == datetime(2026, 8, 8, 15, 0, tzinfo=dt_timezone.utc)

    def test_unknown_project_timezone_falls_back_to_utc(self, issue, project):
        Project.objects.filter(pk=project.pk).update(timezone="Not/AZone")
        issue = Issue.objects.get(pk=issue.pk)
        issue.start_datetime = datetime(2026, 1, 1, 20, 30, tzinfo=dt_timezone.utc)
        issue.save()
        issue.refresh_from_db()
        assert issue.start_date == date(2026, 1, 1)

    def test_sync_reports_changed_fields_without_saving(self, issue):
        issue.start_date = date(2026, 9, 1)
        assert issue.sync_start_target_datetimes() == {"start_datetime"}
        assert issue.start_datetime == datetime(2026, 8, 31, 15, 0, tzinfo=dt_timezone.utc)
        assert Issue.objects.get(pk=issue.pk).start_datetime is None
