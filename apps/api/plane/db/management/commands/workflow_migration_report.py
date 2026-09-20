"""Read-only compatibility inventory; no status creates integration evidence."""

import json
from django.core.management.base import BaseCommand, CommandError
from plane.db.models import Project, Issue, WorkflowConfiguration, WorkContinuation


class Command(BaseCommand):
    help = "Report workflow v2 migration exceptions without changing records or contacting external services."

    def add_arguments(self, parser):
        parser.add_argument("--project", required=True)

    def handle(self, *args, **options):
        try:
            project = Project.objects.get(pk=options["project"])
        except (Project.DoesNotExist, ValueError) as error:
            raise CommandError("Unknown project") from error
        config = WorkflowConfiguration.objects.filter(project=project).first()
        mapping = {str(value): key for key, value in (config.state_mapping if config else {}).items()}
        records = []
        for issue in Issue.objects.filter(project=project).select_related("state"):
            work = WorkContinuation.objects.filter(issue=issue).first()
            reasons = []
            if str(issue.state_id) not in mapping:
                reasons.append("unmapped_state")
            if issue.state and issue.state.group == "completed" and not (work and work.evidence.get("integration")):
                reasons.append("closed_without_independent_integration_evidence")
            if any(issue.name.casefold().startswith(prefix) for prefix in ("[system_test]", "[git push]", "[deploy]")):
                reasons.append("legacy_release_tracking_requires_explicit_source_mapping")
            if issue.state and issue.state.group == "started" and not work:
                reasons.append("in_flight_requires_quiesce_and_reconciliation")
            records.append(
                {
                    "issue_id": str(issue.id),
                    "name": issue.name,
                    "legacy_state_id": str(issue.state_id),
                    "mapped_phase": mapping.get(str(issue.state_id)),
                    "enrolled": work is not None,
                    "exceptions": reasons,
                }
            )
        self.stdout.write(
            json.dumps(
                {
                    "project_id": str(project.id),
                    "dry_run": True,
                    "workflow_version": 2,
                    "enabled": bool(config and config.enabled),
                    "records": records,
                    "exception_count": sum(bool(record["exceptions"]) for record in records),
                },
                indent=2,
            )
        )
