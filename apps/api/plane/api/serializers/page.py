# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Third party imports
from lxml import html
from rest_framework import serializers

# Module imports
from plane.db.models import Page
from plane.utils.content_validator import validate_html_content

from .base import BaseSerializer


class PageSerializer(BaseSerializer):
    """
    Page (wiki) serializer for the public API list view.

    Exposes page metadata without the document body. Use ``PageDetailSerializer``
    for reads and writes that include ``description_html``.
    """

    class Meta:
        model = Page
        fields = [
            "id",
            "name",
            "access",
            "color",
            "parent",
            "is_locked",
            "archived_at",
            "owned_by",
            "workspace",
            "logo_props",
            "external_id",
            "external_source",
            "created_at",
            "updated_at",
            "created_by",
            "updated_by",
        ]
        read_only_fields = [
            "id",
            "is_locked",
            "archived_at",
            "owned_by",
            "workspace",
            "created_at",
            "updated_at",
            "created_by",
            "updated_by",
        ]

    def validate_parent(self, parent):
        if parent is None:
            return parent
        project_id = self.context.get("project_id")
        if not Page.objects.filter(
            pk=parent.id,
            workspace_id=self.context.get("workspace_id"),
            project_pages__project_id=project_id,
            project_pages__deleted_at__isnull=True,
        ).exists():
            raise serializers.ValidationError("Parent is not a page of this project")
        if self.instance is not None and parent.id == self.instance.id:
            raise serializers.ValidationError("A page cannot be its own parent")
        return parent


class PageDetailSerializer(PageSerializer):
    """Page serializer including the HTML document body."""

    description_html = serializers.CharField(required=False, allow_blank=True)

    class Meta(PageSerializer.Meta):
        fields = PageSerializer.Meta.fields + ["description_html"]

    def validate_description_html(self, value):
        if value is None or value == "":
            return "<p></p>"
        try:
            parsed = html.fromstring(value)
            value = html.tostring(parsed, encoding="unicode")
        except Exception:
            raise serializers.ValidationError("Invalid HTML passed")

        is_valid, _error, sanitized_html = validate_html_content(value)
        if not is_valid:
            raise serializers.ValidationError("html content is not valid")
        return sanitized_html if sanitized_html is not None else value
