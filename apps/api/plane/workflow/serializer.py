from rest_framework import serializers
from plane.db.models import WorkContinuation
from .service import serial
from .projection import allowed


class WorkContinuationField(serializers.Field):
    def __init__(self, **kwargs):
        super().__init__(source="*", read_only=True, **kwargs)

    def to_representation(self, value):
        issue_id = value.get("id") if isinstance(value, dict) else value.pk
        row = WorkContinuation.objects.filter(issue_id=issue_id).select_related("project").first()
        if row is None:
            return None
        data = serial(row)
        request = self.context.get("request")
        data["allowed_actions"] = allowed(row.project, request.user, "work", row) if request else []
        return data
