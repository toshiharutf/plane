"""Early structured refusal for legacy writes; PostgreSQL trigger is the final guard."""

from django.db import models
from plane.db.mixins import SoftDeletionQuerySet, SoftDeletionManager
from rest_framework.exceptions import APIException


class WorkflowWriteRequired(APIException):
    status_code = 409
    default_code = "workflow_command_required"
    default_detail = {
        "code": "workflow_command_required",
        "error": "This project uses guarded workflow commands. Refresh the record and submit a versioned command.",
    }


def enabled(project_id):
    from plane.db.models import WorkflowConfiguration
    from .service import command_write

    return (
        not command_write.get() and WorkflowConfiguration.objects.filter(project_id=project_id, enabled=True).exists()
    )


def check_state(project_id, current_id, target_id, creating=False):
    if current_id == target_id and not creating:
        return
    if enabled(project_id):
        if creating:
            from plane.db.models import WorkflowConfiguration

            mapping = WorkflowConfiguration.objects.get(project_id=project_id).state_mapping
            if str(target_id) in {str(mapping.get("Backlog")), str(mapping.get("Todo"))}:
                return
        raise WorkflowWriteRequired()


class GuardedIssueQuerySet(SoftDeletionQuerySet):
    def update(self, **kwargs):
        from .service import command_write

        if not command_write.get() and {"state", "state_id", "project", "project_id", "deleted_at"} & set(kwargs):
            from plane.db.models import WorkflowConfiguration

            projects = WorkflowConfiguration.objects.filter(enabled=True).values("project_id")
            protected = self.filter(project_id__in=projects)
            if {"project", "project_id", "deleted_at"} & set(kwargs):
                if protected.exists():
                    raise WorkflowWriteRequired()
            elif protected.exists():
                target = kwargs.get("state_id", kwargs.get("state"))
                target = getattr(target, "id", target)
                if target is None or protected.exclude(state_id=target).exists():
                    raise WorkflowWriteRequired()
        return super().update(**kwargs)


class GuardedIssueManager(SoftDeletionManager):
    def get_queryset(self):
        return GuardedIssueQuerySet(self.model, using=self._db).filter(deleted_at__isnull=True)


class GuardedAllIssueQuerySet(GuardedIssueQuerySet):
    def delete(self, soft=False):
        from .service import command_write
        from plane.db.models import WorkflowConfiguration

        if not command_write.get():
            projects = WorkflowConfiguration.objects.filter(enabled=True).values("project_id")
            if self.filter(project_id__in=projects).exists():
                raise WorkflowWriteRequired()
        return super().delete(soft=soft)


class GuardedAllIssueManager(models.Manager):
    def get_queryset(self):
        return GuardedAllIssueQuerySet(self.model, using=self._db)
