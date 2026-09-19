"""Remove the Error work item state (added by 0126_add_error_state).

The work item state machine (plane.utils.work_item_state_rules) has no Error
state: a failed run now waits for a person in Awaiting Human. For every project
that has a live state named Error (any case):

- work items in Error move to the project's Awaiting Human state (nothing else
  is created; a project without Awaiting Human keeps its Error state and items);
- the Error state is soft-deleted (``deleted_at``, as deleting a state in the app
  does), after another state became the project default if Error was it.
"""

from django.db import migrations
from django.utils import timezone

ERROR_STATE_NAME = "Error"
AWAITING_HUMAN_STATE_NAME = "Awaiting Human"


def _next_default(State, project_id, excluded_id):
    states = (
        State._base_manager.filter(project_id=project_id, deleted_at__isnull=True, is_triage=False)
        .exclude(group="triage")
        .exclude(pk=excluded_id)
    )
    return (
        states.filter(name__iexact="Backlog").first()
        or states.filter(group="backlog").order_by("sequence").first()
        or states.order_by("sequence").first()
    )


def remove_error_state(apps, schema_editor):
    # ``_base_manager`` sees every row (soft-deleted ones included), historical or live model alike.
    State = apps.get_model("db", "State")
    Issue = apps.get_model("db", "Issue")
    now = timezone.now()
    error_states = State._base_manager.filter(name__iexact=ERROR_STATE_NAME, deleted_at__isnull=True).exclude(
        group="triage"
    )
    for error in error_states.iterator():
        awaiting = State._base_manager.filter(
            project_id=error.project_id, name__iexact=AWAITING_HUMAN_STATE_NAME, deleted_at__isnull=True
        ).first()
        # Every row, archived and soft-deleted work items included, so nothing points at a deleted state.
        items = Issue._base_manager.filter(state_id=error.id)
        if awaiting is not None:
            items.update(state_id=awaiting.id)
        elif items.exists():
            # Nowhere to move them without creating a state: leave this project as it is.
            continue
        if error.default:
            replacement = _next_default(State, error.project_id, error.id)
            if replacement is not None:
                State._base_manager.filter(pk=replacement.pk).update(default=True)
        State._base_manager.filter(pk=error.pk).update(deleted_at=now, default=False)


class Migration(migrations.Migration):
    dependencies = [
        ("db", "0130_merge_20260918_2037"),
    ]

    operations = [migrations.RunPython(remove_error_state, migrations.RunPython.noop)]
