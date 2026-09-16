# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Django imports
from django.db.models import Q

# Third party imports
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.response import Response

# Module imports
from plane.api.serializers import PageDetailSerializer, PageSerializer
from plane.app.permissions import ProjectEntityOrAIBotPagePermission
from plane.bgtasks.page_transaction_task import page_transaction
from plane.db.models import Page, Project, ProjectPage
from plane.utils.openapi import (
    CURSOR_PARAMETER,
    EXPAND_PARAMETER,
    FIELDS_PARAMETER,
    FORBIDDEN_RESPONSE,
    INVALID_REQUEST_RESPONSE,
    NOT_FOUND_RESPONSE,
    PER_PAGE_PARAMETER,
    PROJECT_ID_PARAMETER,
    UNAUTHORIZED_RESPONSE,
    WORKSPACE_SLUG_PARAMETER,
    create_paginated_response,
)

from .base import BaseAPIView

PAGE_ID_PARAMETER = OpenApiParameter(
    name="pk",
    type=str,
    location=OpenApiParameter.PATH,
    description="Page ID",
    required=True,
)

ARCHIVED_PARAMETER = OpenApiParameter(
    name="archived",
    type=bool,
    location=OpenApiParameter.QUERY,
    description="Include archived pages when true",
    required=False,
)

PAGE_DOC_DEFAULTS = {
    "tags": ["Pages"],
    "parameters": [WORKSPACE_SLUG_PARAMETER, PROJECT_ID_PARAMETER],
    "responses": {
        401: UNAUTHORIZED_RESPONSE,
        403: FORBIDDEN_RESPONSE,
        404: NOT_FOUND_RESPONSE,
    },
}


def page_docs(**kwargs):
    merged = dict(PAGE_DOC_DEFAULTS)
    merged["parameters"] = PAGE_DOC_DEFAULTS["parameters"] + kwargs.pop("parameters", [])
    merged["responses"] = {**PAGE_DOC_DEFAULTS["responses"], **kwargs.pop("responses", {})}
    merged.update(kwargs)
    return extend_schema(**merged)


def visible_pages(slug, project_id, user):
    """Pages of a project the user may see: every public page plus the user's own private pages."""
    return (
        Page.objects.filter(
            workspace__slug=slug,
            project_pages__project_id=project_id,
            project_pages__deleted_at__isnull=True,
            project_pages__project__archived_at__isnull=True,
        )
        .filter(Q(access=Page.PUBLIC_ACCESS) | Q(owned_by=user))
        .select_related("workspace", "owned_by")
        .distinct()
    )


class PageListCreateAPIEndpoint(BaseAPIView):
    """Project page (wiki) list and create endpoint."""

    model = Page
    serializer_class = PageSerializer
    permission_classes = [ProjectEntityOrAIBotPagePermission]
    use_read_replica = True

    def get_queryset(self):
        queryset = visible_pages(self.kwargs.get("slug"), self.kwargs.get("project_id"), self.request.user)
        if self.request.GET.get("archived", "false").lower() != "true":
            queryset = queryset.filter(archived_at__isnull=True)
        return queryset.order_by("-created_at")

    @page_docs(
        operation_id="list_pages",
        summary="List pages",
        description="Retrieve a paginated list of the project's pages visible to the caller: public pages and the caller's own private pages. Archived pages are excluded unless `archived=true`.",  # noqa: E501
        parameters=[CURSOR_PARAMETER, PER_PAGE_PARAMETER, ARCHIVED_PARAMETER, FIELDS_PARAMETER, EXPAND_PARAMETER],
        responses={
            200: create_paginated_response(
                PageSerializer,
                "PaginatedPageResponse",
                "Paginated list of pages",
                "Paginated Pages",
            ),
        },
    )
    def get(self, request, slug, project_id):
        """List pages

        Retrieve a paginated list of the project's pages visible to the caller.
        """
        return self.paginate(
            request=request,
            queryset=self.get_queryset(),
            on_results=lambda pages: PageSerializer(pages, many=True, fields=self.fields, expand=self.expand).data,
        )

    @page_docs(
        operation_id="create_page",
        summary="Create page",
        description="Create a new page in the project. The caller becomes the page owner.",
        request=PageDetailSerializer,
        responses={
            201: OpenApiResponse(description="Page created successfully", response=PageDetailSerializer),
            400: INVALID_REQUEST_RESPONSE,
        },
    )
    def post(self, request, slug, project_id):
        """Create page

        Create a new page in the project. The caller becomes the page owner.
        """
        project = Project.objects.get(pk=project_id, workspace__slug=slug)
        serializer = PageDetailSerializer(
            data=request.data,
            context={"project_id": project_id, "workspace_id": project.workspace_id},
        )
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        page = serializer.save(owned_by=request.user, workspace_id=project.workspace_id)
        ProjectPage.objects.create(
            workspace_id=page.workspace_id,
            project_id=project_id,
            page_id=page.id,
            created_by_id=page.created_by_id,
            updated_by_id=page.updated_by_id,
        )
        # Record mentions and embeds so the page shows up in the web app like an editor-created page.
        page_transaction.delay(
            new_description_html=page.description_html,
            old_description_html=None,
            page_id=str(page.id),
        )
        return Response(PageDetailSerializer(page).data, status=status.HTTP_201_CREATED)


class PageDetailAPIEndpoint(BaseAPIView):
    """Project page (wiki) detail endpoint."""

    model = Page
    serializer_class = PageDetailSerializer
    permission_classes = [ProjectEntityOrAIBotPagePermission]
    use_read_replica = True

    def get_queryset(self):
        return visible_pages(self.kwargs.get("slug"), self.kwargs.get("project_id"), self.request.user)

    @page_docs(
        operation_id="retrieve_page",
        summary="Retrieve page",
        description="Retrieve a page including its HTML body. Private pages are only visible to their owner.",
        parameters=[PAGE_ID_PARAMETER, FIELDS_PARAMETER, EXPAND_PARAMETER],
        responses={200: OpenApiResponse(description="Page details", response=PageDetailSerializer)},
    )
    def get(self, request, slug, project_id, pk):
        """Retrieve page

        Retrieve a page including its HTML body.
        """
        page = self.get_queryset().get(pk=pk)
        serializer = PageDetailSerializer(page, fields=self.fields, expand=self.expand)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @page_docs(
        operation_id="update_page",
        summary="Update page",
        description="Partially update a page. Locked and archived pages cannot be edited, only the owner may change `access`, and updating `description_html` replaces the document body.",  # noqa: E501
        parameters=[PAGE_ID_PARAMETER],
        request=PageDetailSerializer,
        responses={
            200: OpenApiResponse(description="Page updated successfully", response=PageDetailSerializer),
            400: INVALID_REQUEST_RESPONSE,
        },
    )
    def patch(self, request, slug, project_id, pk):
        """Update page

        Partially update a page. Locked and archived pages cannot be edited.
        """
        page = self.get_queryset().get(pk=pk)

        if page.is_locked:
            return Response({"error": "Page is locked"}, status=status.HTTP_400_BAD_REQUEST)
        if page.archived_at:
            return Response({"error": "Page is archived"}, status=status.HTTP_400_BAD_REQUEST)
        access_changed = "access" in request.data and str(request.data.get("access")) != str(page.access)
        if access_changed and page.owned_by_id != request.user.id:
            return Response(
                {"error": "Access cannot be updated since this page is owned by someone else"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = PageDetailSerializer(
            page,
            data=request.data,
            partial=True,
            context={"project_id": project_id, "workspace_id": page.workspace_id},
        )
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        old_description_html = page.description_html
        new_description_html = serializer.validated_data.get("description_html")
        description_changed = new_description_html is not None and new_description_html != old_description_html

        page = serializer.save()
        if description_changed:
            # The collaborative editor treats the binary document as the source of truth and only
            # rebuilds it from HTML when it is empty, so clear it to make the HTML update visible.
            page.description_binary = None
            page.description_json = {}
            page.save(update_fields=["description_binary", "description_json"])
            page_transaction.delay(
                new_description_html=new_description_html,
                old_description_html=old_description_html,
                page_id=str(page.id),
            )

        return Response(PageDetailSerializer(page).data, status=status.HTTP_200_OK)
