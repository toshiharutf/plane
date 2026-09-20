"""The app/session and public/token APIs share exactly the same command service."""

from decimal import InvalidOperation
from django.db import IntegrityError, DataError
from django.core.exceptions import ValidationError
from django.http import Http404
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from plane.api.middleware.api_authentication import APIKeyAuthentication
from plane.authentication.session import BaseSessionAuthentication
from plane.db.models import Project
from .service import execute, Refusal
from .projection import records, progress


class WorkflowView(APIView):
    permission_classes = [IsAuthenticated]
    authentication_classes = [BaseSessionAuthentication]
    projection = "records"

    def project(self, slug, project_id):
        try:
            return Project.objects.get(pk=project_id, workspace__slug=slug)
        except Project.DoesNotExist:
            raise Http404

    def get(self, request, slug, project_id):
        project = self.project(slug, project_id)
        try:
            result = (
                progress(project.id, request.user, request.query_params)
                if self.projection == "progress"
                else records(project.id, request.user)
            )
            return Response(result)
        except Refusal as error:
            return Response({"code": error.code, "error": error.message, "details": error.details}, status=error.status)
        except (ValueError, ValidationError):
            return Response({"code": "invalid_filter", "error": "Invalid scoped filter."}, status=400)

    def post(self, request, slug, project_id):
        project = self.project(slug, project_id)
        try:
            return Response(execute(project.id, request.user, request.data))
        except Refusal as error:
            return Response({"code": error.code, "error": error.message, "details": error.details}, status=error.status)
        except (KeyError, TypeError, ValueError, ValidationError, InvalidOperation, IntegrityError, DataError):
            return Response({"code": "invalid_payload", "error": "Invalid command fields."}, status=400)


class PublicWorkflowView(WorkflowView):
    authentication_classes = [APIKeyAuthentication]


class ProgressView(WorkflowView):
    projection = "progress"


class PublicProgressView(PublicWorkflowView):
    projection = "progress"
