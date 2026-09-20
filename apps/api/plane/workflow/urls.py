from django.urls import path
from .views import WorkflowView, PublicWorkflowView, ProgressView, PublicProgressView


def routes(public=False):
    view, progress = (PublicWorkflowView, PublicProgressView) if public else (WorkflowView, ProgressView)
    base = "workspaces/<str:slug>/projects/<uuid:project_id>/"
    return [
        path(base + "workflow-v2/", view.as_view(http_method_names=["get"]), name="workflow-v2"),
        path(base + "workflow-v2/commands/", view.as_view(http_method_names=["post"]), name="workflow-v2-commands"),
        path(base + "progress/", progress.as_view(http_method_names=["get"]), name="project-progress"),
    ]
