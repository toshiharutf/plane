"""Add the default work item state In Review to every project.

The work item state machine (plane.utils.work_item_state_rules) finishes work into
In Review; Done means merged. New projects get the state from ``DEFAULT_STATES``.
For every project that is not deleted and has no live state named In Review (any
case), this migration creates it right after In Progress. It writes nothing else:
no other state and no work item changes.

The reverse moves the work items in In Review to the project's In Progress state
(else another started state that is not Awaiting Human, else the default state)
and only then soft-deletes the In Review state (``deleted_at``, as deleting a
state in the app does), after another state became the project default if In
Review was it. A project whose In Review items have nowhere to go keeps the state.
"""

from django.db import migrations
from django.utils import timezone
from django.utils.text import slugify

IN_REVIEW_STATE = {"name": "In Review", "color": "#3E63DD", "group": "started"}
IN_PROGRESS_STATE_NAME = "In Progress"
AWAITING_HUMAN_STATE_NAME = "Awaiting Human"


def _is_named(state, name):
    return state.name.casefold() == name.casefold()


def in_review_state_sequence(states):
    """Halfway between In Progress and the next state; ``states`` are the project's live states without triage.

    Without an In Progress state: after the last started state that is not Awaiting Human,
    else after the last backlog or unstarted state. Without a following state: plus 15000.
    """
    in_progress = [state.sequence for state in states if _is_named(state, IN_PROGRESS_STATE_NAME)]
    started = [
        state.sequence
        for state in states
        if state.group == "started" and not _is_named(state, AWAITING_HUMAN_STATE_NAME)
    ]
    early = [state.sequence for state in states if state.group in ("backlog", "unstarted")]
    after = max(in_progress or started or early or [0])
    following = [state.sequence for state in states if state.sequence > after]
    return (after + min(following)) / 2 if following else after + 15000


def add_in_review_state(apps, schema_editor):
    # ``_base_manager`` sees every row (soft-deleted ones included), historical or live model alike.
    Project = apps.get_model("db", "Project")
    State = apps.get_model("db", "State")
    name = IN_REVIEW_STATE["name"]
    new_states = []
    for project in Project._base_manager.filter(deleted_at__isnull=True).iterator():
        states = list(State._base_manager.filter(project_id=project.id, deleted_at__isnull=True))
        if any(_is_named(state, name) for state in states):
            continue
        new_states.append(
            State(
                name=name,
                slug=slugify(name),
                color=IN_REVIEW_STATE["color"],
                group=IN_REVIEW_STATE["group"],
                sequence=in_review_state_sequence([state for state in states if state.group != "triage"]),
                project_id=project.id,
                workspace_id=project.workspace_id,
                created_by_id=project.created_by_id,
            )
        )
    State._base_manager.bulk_create(new_states, batch_size=500)


def _next_default(states):
    return (
        states.filter(name__iexact="Backlog").first()
        or states.filter(group="backlog").order_by("sequence").first()
        or states.order_by("sequence").first()
    )


def remove_in_review_state(apps, schema_editor):
    State = apps.get_model("db", "State")
    Issue = apps.get_model("db", "Issue")
    now = timezone.now()
    in_review_states = State._base_manager.filter(
        name__iexact=IN_REVIEW_STATE["name"], deleted_at__isnull=True
    ).exclude(group="triage")
    for in_review in in_review_states.iterator():
        others = (
            State._base_manager.filter(project_id=in_review.project_id, deleted_at__isnull=True, is_triage=False)
            .exclude(group="triage")
            .exclude(pk=in_review.pk)
        )
        default = others.filter(default=True).first()
        if default is None and in_review.default:
            default = _next_default(others)
        target = (
            others.filter(name__iexact=IN_PROGRESS_STATE_NAME).order_by("sequence").first()
            or others.filter(group="started")
            .exclude(name__iexact=AWAITING_HUMAN_STATE_NAME)
            .order_by("sequence")
            .first()
            or default
        )
        # Every row, archived and soft-deleted work items included, so nothing points at a deleted state.
        items = Issue._base_manager.filter(state_id=in_review.id)
        if target is not None:
            items.update(state_id=target.id)
        elif items.exists():
            # Nowhere to move them: leave this project as it is.
            continue
        if in_review.default and default is not None:
            State._base_manager.filter(pk=default.pk).update(default=True)
        State._base_manager.filter(pk=in_review.pk).update(deleted_at=now, default=False)


class Migration(migrations.Migration):
    dependencies = [
        ("db", "0131_remove_error_state"),
    ]

    operations = [migrations.RunPython(add_in_review_state, remove_in_review_state)]
