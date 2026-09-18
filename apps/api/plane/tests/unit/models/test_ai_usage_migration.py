# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta
from decimal import Decimal
from importlib import import_module

import pytest
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import override_settings
from django.utils import timezone

from plane.db.models import AIUsageRecord, Issue, Project, Workspace
from plane.utils.ai_pricing import PRICE_VERSION

migration = import_module("plane.db.migrations.0128_aiusagerecord")
drop_migration = import_module("plane.db.migrations.0129_drop_workitemaiusage")


@pytest.fixture
def legacy_apps(db):
    """Models as of 0128, with the dropped ``work_item_ai_usages`` table recreated for the test."""
    # pytest runs with --nomigrations, which hides the migration modules from the loader.
    with override_settings(MIGRATION_MODULES={}):
        old_apps = MigrationLoader(None).project_state(("db", "0128_aiusagerecord")).apps
    with connection.schema_editor() as editor:
        editor.create_model(old_apps.get_model("db", "WorkItemAIUsage"))
    return old_apps


def _legacy_usage(legacy_apps, issue, **fields):
    WorkItemAIUsage = legacy_apps.get_model("db", "WorkItemAIUsage")
    return WorkItemAIUsage.objects.create(
        issue_id=issue.id, project_id=issue.project_id, workspace_id=issue.workspace_id, **fields
    )


@pytest.fixture
def project(create_user):
    workspace = Workspace.objects.create(name="Usage Workspace", slug="usage-workspace", owner=create_user)
    return Project.objects.create(name="Usage", identifier="USG", workspace=workspace, created_by=create_user)


@pytest.fixture
def issue(project):
    return Issue.objects.create(name="Build the usage table", project=project, workspace=project.workspace)


@pytest.mark.unit
@pytest.mark.django_db
class TestAIUsageRecordModel:
    def test_ac1_stores_all_fields(self, issue, project):
        record = AIUsageRecord.objects.create(
            issue=issue,
            project=project,
            session_id="s-1",
            model="claude-opus-5",
            effort="high",
            duration_seconds=620,
            input_tokens=1,
            cache_write_5m_tokens=2,
            cache_write_1h_tokens=3,
            cache_read_tokens=4,
            output_tokens=5,
            usage_5h_pct=12.5,
            usage_weekly_pct=3.25,
        )
        record.refresh_from_db()
        assert record.issue_title == "Build the usage table"
        assert record.workspace_id == project.workspace_id
        assert (record.effort, record.duration_seconds, record.usage_5h_pct, record.usage_weekly_pct) == (
            "high",
            620,
            12.5,
            3.25,
        )
        assert record.split_unknown is False
        index_fields = [index.fields for index in AIUsageRecord._meta.indexes]
        assert ["created_at"] in index_fields

    def test_work_item_is_optional_and_title_survives_deletion(self, issue, project):
        AIUsageRecord.objects.create(project=project, model="claude-opus-5")
        record = AIUsageRecord.objects.create(issue=issue, project=project, model="claude-opus-5")
        Issue.all_objects.filter(id=issue.id).delete()
        record.refresh_from_db()
        assert record.issue_id is None
        assert record.issue_title == "Build the usage table"

    def test_ac2_save_computes_cost_and_price_version(self, project):
        record = AIUsageRecord.objects.create(
            project=project, model="claude-opus-5", input_tokens=1_000_000, output_tokens=100_000
        )
        record.refresh_from_db()
        assert record.api_cost_usd == Decimal("7.50")
        assert record.price_version == "2026-09"

        record.output_tokens = 200_000
        record.save(update_fields=["output_tokens"])
        record.refresh_from_db()
        assert record.api_cost_usd == Decimal("10.00")

    def test_unknown_model_keeps_cost_null(self, project):
        record = AIUsageRecord.objects.create(project=project, model="mystery-model", input_tokens=5)
        record.refresh_from_db()
        assert record.api_cost_usd is None
        assert record.price_version == PRICE_VERSION


@pytest.mark.unit
@pytest.mark.django_db
class TestCopyWorkItemAIUsageMigration:
    def test_ac3_copies_rows_with_cache_writes_as_1h(self, legacy_apps, issue, project):
        old = _legacy_usage(
            legacy_apps,
            issue,
            model="claude-opus-5",
            session_id="old-session",
            input_tokens=1_000_000,
            output_tokens=100_000,
            cache_creation_input_tokens=10_000,
            cache_read_input_tokens=20_000,
            external_source="claude-code",
            external_id="ext-1",
        )
        created_at = timezone.now() - timedelta(days=30)
        type(old).objects.filter(id=old.id).update(created_at=created_at)

        migration.copy_work_item_ai_usage(legacy_apps, None)

        record = AIUsageRecord.objects.get(id=old.id)
        assert record.cache_write_1h_tokens == 10_000
        assert record.cache_write_5m_tokens == 0
        assert record.split_unknown is True
        assert record.cache_read_tokens == 20_000
        assert (record.input_tokens, record.output_tokens) == (1_000_000, 100_000)
        assert record.issue_id == issue.id
        assert record.issue_title == "Build the usage table"
        assert (record.session_id, record.external_source, record.external_id) == (
            "old-session",
            "claude-code",
            "ext-1",
        )
        assert record.created_at == created_at
        # $5 input + $2.50 output + 10k 1h writes at $10/M + 20k hits at $0.50/M
        assert record.api_cost_usd == Decimal("7.61")
        assert record.price_version == PRICE_VERSION

    def test_copies_every_row_once(self, legacy_apps, issue, project):
        _legacy_usage(legacy_apps, issue, model="claude-opus-5", session_id="a")
        deleted = _legacy_usage(legacy_apps, issue, model="claude-sonnet-5", session_id="b")
        type(deleted).objects.filter(id=deleted.id).update(deleted_at=timezone.now())

        migration.copy_work_item_ai_usage(legacy_apps, None)
        drop_migration.copy_remaining_work_item_ai_usage(legacy_apps, None)

        assert AIUsageRecord.all_objects.count() == 2
        assert AIUsageRecord.all_objects.get(id=deleted.id).deleted_at is not None


@pytest.mark.unit
def test_ac5_drop_runs_after_the_copy_migration():
    operations = drop_migration.Migration.operations
    assert ("db", "0128_aiusagerecord") in drop_migration.Migration.dependencies
    assert operations[-1].name == "WorkItemAIUsage"
    assert operations[0].code is drop_migration.copy_remaining_work_item_ai_usage
