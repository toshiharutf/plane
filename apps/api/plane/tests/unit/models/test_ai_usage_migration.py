# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta
from decimal import Decimal
from importlib import import_module

import pytest
from django.apps import apps
from django.utils import timezone

from plane.db.models import AIUsageRecord, Issue, Project, WorkItemAIUsage, Workspace
from plane.utils.ai_pricing import PRICE_VERSION

migration = import_module("plane.db.migrations.0128_aiusagerecord")


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
    def test_ac3_copies_rows_with_cache_writes_as_1h(self, issue, project):
        old = WorkItemAIUsage.objects.create(
            issue=issue,
            project=project,
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
        WorkItemAIUsage.objects.filter(id=old.id).update(created_at=created_at)

        migration.copy_work_item_ai_usage(apps, None)

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

    def test_copies_every_row_once(self, issue, project):
        WorkItemAIUsage.objects.create(issue=issue, project=project, model="claude-opus-5", session_id="a")
        deleted = WorkItemAIUsage.objects.create(issue=issue, project=project, model="claude-sonnet-5", session_id="b")
        WorkItemAIUsage.objects.filter(id=deleted.id).update(deleted_at=timezone.now())

        migration.copy_work_item_ai_usage(apps, None)
        migration.copy_work_item_ai_usage(apps, None)

        assert AIUsageRecord.all_objects.count() == 2
        assert AIUsageRecord.all_objects.get(id=deleted.id).deleted_at is not None
