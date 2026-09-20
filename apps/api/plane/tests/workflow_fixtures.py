from types import SimpleNamespace
from uuid import uuid4
import pytest
from plane.db.models import User, Workspace, WorkspaceMember, Project, ProjectMember, State, Issue, WorkContinuation
from plane.workflow.service import execute, MODELS


@pytest.fixture
def workflow(db):
    user = User.objects.create(email=f"human-{uuid4()}@test.invalid", username=str(uuid4()))
    bot = User.objects.create(email=f"bot-{uuid4()}@test.invalid", username=str(uuid4()), is_bot=True)
    workspace = Workspace.objects.create(name="Local workflow", slug=str(uuid4()), owner=user)
    project = Project.objects.create(name="Local workflow", identifier="WF", workspace=workspace)
    for actor in (user, bot):
        WorkspaceMember.objects.create(workspace=workspace, member=actor, role=20)
    ProjectMember.objects.create(workspace=workspace, project=project, member=user, role=20)
    states = {}
    for name, group in [
        ("Backlog", "backlog"),
        ("Todo", "unstarted"),
        ("In Progress", "started"),
        ("In Review", "started"),
        ("Awaiting Human", "started"),
        ("Done", "completed"),
        ("Cancelled", "cancelled"),
    ]:
        states[name] = State.objects.create(
            workspace=workspace, project=project, name=name, group=group, default=name == "Backlog"
        )
    fixture = SimpleNamespace(user=user, bot=bot, workspace=workspace, project=project, states=states)
    result = perform(
        fixture,
        "configuration",
        "configure",
        payload={
            "enabled": True,
            "release_enabled": True,
            "state_mapping": {name: str(state.id) for name, state in states.items()},
        },
    )
    fixture.config = MODELS["configuration"].objects.get(pk=result["subject_id"])
    return fixture


def perform(fixture, kind, action, subject=None, payload=None, actor=None, **envelope):
    pk = getattr(subject, "pk", subject)
    if kind == "work" and isinstance(subject, WorkContinuation):
        pk = subject.issue_id
    if pk:
        row = MODELS[kind].objects.get(**({"issue_id": pk} if kind == "work" else {"pk": pk}))
    elif kind == "configuration":
        row = MODELS[kind].objects.filter(project=fixture.project).first()
    else:
        row = None
    return execute(
        fixture.project.id,
        actor or fixture.user,
        {
            "command_id": str(uuid4()),
            "subject_type": kind,
            "action": action,
            "subject_id": str(pk) if pk else None,
            "expected_version": row.version if row else 0,
            "workflow_version": 2,
            "payload": payload or {},
            **envelope,
        },
    )


def make_work(fixture, kind="code", name="Implementation"):
    issue = Issue.objects.create(
        project=fixture.project, workspace=fixture.workspace, name=name, state=fixture.states["Backlog"]
    )
    perform(
        fixture,
        "work",
        "enroll",
        payload={"issue_id": str(issue.id), "repository": "local/repo", "execution_kind": kind},
    )
    return WorkContinuation.objects.get(issue=issue)


def acquire(fixture, resource, actor=None):
    return perform(fixture, "lease", "acquire", payload={"resource": resource, "run_id": str(uuid4())}, actor=actor)[
        "result"
    ]


def fenced(lease):
    return {"lease_id": lease["id"], "fence": lease["fence"]}
