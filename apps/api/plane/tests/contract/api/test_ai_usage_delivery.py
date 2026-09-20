# ruff: noqa: F401, F811  # pytest fixtures are intentionally re-exported
"""Delivery retries must never double-charge or erase measured session usage."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from unittest.mock import patch

import pytest
from django.db import IntegrityError, close_old_connections, transaction
from rest_framework.test import APIClient

from plane.db.models import AIUsageRecord, Issue, State
from plane.tests.contract.api.test_work_item_ai_usage import (
    USAGE,
    _create_issue,
    _url,
    human_client,
    project,
    state,
)


@pytest.mark.django_db
def test_codex_record_prices_disjoint_categories_without_double_counting(workspace, project, state, create_user):
    issue = _create_issue(project, workspace, state, create_user, "Codex categories")
    row = AIUsageRecord.objects.create(
        issue=issue,
        project=project,
        model="gpt-6-astra",
        input_tokens=172_001,
        cache_read_tokens=100_000,
        output_tokens=10_000,
    )
    row.refresh_from_db()
    assert row.api_cost_usd == Decimal("4.390020")
    assert row.price_version == "codex-api-equiv-2026-09-20"
    assert sum(Decimal(value) for value in row.cost_breakdown.values()) == row.api_cost_usd
    assert Decimal(row.cost_breakdown["cache_read"]) == Decimal("0.2")


@pytest.mark.django_db
def test_old_delivery_cannot_lower_cumulative_usage(workspace, project, state, create_user, human_client):
    issue = _create_issue(project, workspace, state, create_user, "Cumulative usage")
    url = _url(workspace.slug, project.id, issue.id)
    assert human_client.post(url, {**USAGE, "duration_seconds": 300}, format="json").status_code == 201
    for changed in ({"output_tokens": 1}, {"duration_seconds": 1}, {"duration_seconds": None}):
        response = human_client.post(url, {**USAGE, **changed}, format="json")
        assert response.status_code == 409
        assert response.data["error"] == "stale_usage_report"
    row = AIUsageRecord.objects.get(issue=issue)
    assert row.output_tokens == USAGE["output_tokens"]
    assert row.duration_seconds == 300


@pytest.mark.django_db
def test_stale_batch_is_atomic(workspace, project, state, create_user, human_client):
    issue = _create_issue(project, workspace, state, create_user, "Atomic usage")
    url = _url(workspace.slug, project.id, issue.id)
    human_client.post(url, USAGE, format="json")
    response = human_client.post(url, [{**USAGE, "session_id": "new"}, {**USAGE, "input_tokens": 0}], format="json")
    assert response.status_code == 409
    assert AIUsageRecord.objects.filter(issue=issue).count() == 1


@pytest.mark.django_db
def test_database_enforces_delivery_identity(workspace, project, state, create_user):
    issue = _create_issue(project, workspace, state, create_user, "Unique delivery")
    kwargs = dict(issue=issue, project=project, model="unknown", session_id="same")
    AIUsageRecord.objects.create(**kwargs)
    with pytest.raises(IntegrityError), transaction.atomic():
        AIUsageRecord.objects.create(**kwargs)


@pytest.mark.django_db
def test_metadata_save_preserves_original_price(workspace, project, state, create_user):
    issue = _create_issue(project, workspace, state, create_user, "Price provenance")
    usage = AIUsageRecord.objects.create(issue=issue, project=project, model="claude-opus-5", input_tokens=1_000_000)
    original = (usage.api_cost_usd, usage.price_version)
    with patch("plane.db.models.ai_usage.compute_cost", return_value=(Decimal("100"), "future")) as compute:
        usage.issue_title = "Renamed task"
        usage.save()
        compute.assert_not_called()
        usage.refresh_from_db()
        assert (usage.api_cost_usd, usage.price_version) == original
        usage.output_tokens = 1
        usage.save()
        compute.assert_called_once()


@pytest.mark.django_db
def test_measured_history_filters_before_pagination(workspace, project, create_user, human_client):
    done = State.objects.create(name="Done", group="completed", project=project, workspace=workspace)
    issue = _create_issue(project, workspace, done, create_user, "Measured sample")
    measured = AIUsageRecord.objects.create(
        issue=issue, project=project, model="claude-opus-5", duration_seconds=60, input_tokens=100
    )
    AIUsageRecord.objects.create(issue=issue, project=project, model="unknown", duration_seconds=60)
    AIUsageRecord.objects.create(issue=issue, project=project, model="claude-opus-5", duration_seconds=None)
    response = human_client.get(f"/api/v1/workspaces/{workspace.slug}/ai-usage/history/?measured=true&per_page=1")
    assert response.status_code == 200, response.data
    assert [str(row["id"]) for row in response.data["results"]] == [str(measured.id)]


@pytest.mark.django_db
def test_unknown_cost_and_duration_are_not_reported_as_complete_totals(workspace, project, create_user, session_client):
    done = State.objects.create(name="Done", group="completed", project=project, workspace=workspace)
    issue = _create_issue(project, workspace, done, create_user, "Incomplete historical usage")
    known = AIUsageRecord.objects.create(
        issue=issue, project=project, model="claude-opus-5", duration_seconds=60, input_tokens=100
    )
    unknown = AIUsageRecord.objects.create(issue=issue, project=project, model="claude-opus-5")
    AIUsageRecord.objects.filter(pk=unknown.pk).update(api_cost_usd=None)
    response = session_client.get(f"/api/workspaces/{workspace.slug}/analytics/ai-usage/")
    assert response.status_code == 200
    record = response.data["models"][0]["work_items"][0]
    assert record["api_cost_usd"] is None
    assert record["duration_seconds"] is None
    assert record["known_api_cost_usd"] == known.api_cost_usd
    assert record["unknown_cost_count"] == record["unknown_duration_count"] == 1


@pytest.mark.django_db
def test_recorded_category_costs_sum_to_authoritative_price(workspace, project, state, create_user):
    issue = _create_issue(project, workspace, state, create_user, "Priced categories")
    row = AIUsageRecord.objects.create(
        issue=issue,
        project=project,
        model="claude-opus-5",
        input_tokens=19,
        output_tokens=7,
        cache_write_5m_tokens=11,
        cache_write_1h_tokens=3,
        cache_read_tokens=23,
    )
    assert sum(Decimal(value) for value in row.cost_breakdown.values()) == row.api_cost_usd
    assert all(Decimal(value) >= 0 for value in row.cost_breakdown.values())
    original = row.cost_breakdown
    row.issue_title = "Changed title"
    row.save()
    assert row.cost_breakdown == original


@pytest.mark.django_db
def test_historical_category_costs_remain_unknown(workspace, project, create_user, session_client):
    done = State.objects.create(name="Done", group="completed", project=project, workspace=workspace)
    issue = _create_issue(project, workspace, done, create_user, "Historical pricing")
    row = AIUsageRecord.objects.create(issue=issue, project=project, model="claude-opus-5", input_tokens=100)
    AIUsageRecord.objects.filter(pk=row.pk).update(cost_breakdown=None)
    row.refresh_from_db()
    row.issue_title = "Keep historical price"
    row.save()
    assert row.cost_breakdown is None
    result = session_client.get(f"/api/workspaces/{workspace.slug}/analytics/ai-usage/")
    record = result.data["models"][0]["work_items"][0]
    assert record["api_cost_usd"] == row.api_cost_usd
    assert record["cost_categories_usd"] is None
    assert record["unknown_category_count"] == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_first_delivery_is_stored_once(workspace, project, state, create_user, api_token):
    issue = _create_issue(project, workspace, state, create_user, "Concurrent ingestion")
    url = _url(workspace.slug, project.id, issue.id)
    token = api_token.token
    ready = Barrier(2)

    def deliver():
        close_old_connections()
        try:
            client = APIClient()
            client.credentials(HTTP_X_API_KEY=token)
            ready.wait(timeout=10)
            response = client.post(url, USAGE, format="json")
            return response.status_code, response.data
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: deliver(), range(2)))
    assert [result[0] for result in results] == [201, 201], results
    assert results[0][1]["id"] == results[1][1]["id"]
    assert AIUsageRecord.objects.filter(issue=issue).count() == 1
