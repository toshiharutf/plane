from django.db import migrations
from django.utils.text import slugify

ERROR_STATE = {"name": "Error", "color": "#E5484D", "group": "started"}


def error_state_sequence(states):
    """Place Error right after the last started state, before the next state."""
    started = [state.sequence for state in states if state.group == "started"]
    if not started:
        return max((state.sequence for state in states), default=0) + 15000
    after = max(started)
    following = [state.sequence for state in states if state.sequence > after]
    return (after + min(following)) / 2 if following else after + 15000


def add_error_state(apps, schema_editor):
    Project = apps.get_model("db", "Project")
    State = apps.get_model("db", "State")
    new_states = []
    for project in Project.objects.filter(deleted_at__isnull=True).iterator():
        states = list(State.objects.filter(project_id=project.id, deleted_at__isnull=True))
        if any(state.name.casefold() == ERROR_STATE["name"].casefold() for state in states):
            continue
        new_states.append(
            State(
                name=ERROR_STATE["name"],
                slug=slugify(ERROR_STATE["name"]),
                color=ERROR_STATE["color"],
                group=ERROR_STATE["group"],
                sequence=error_state_sequence(states),
                project_id=project.id,
                workspace_id=project.workspace_id,
                created_by_id=project.created_by_id,
            )
        )
    State.objects.bulk_create(new_states, batch_size=500)


class Migration(migrations.Migration):
    dependencies = [
        ("db", "0125_merge_0123_issue_ai_model_0124_merge_20260916_2323"),
    ]

    operations = [migrations.RunPython(add_error_state, migrations.RunPython.noop)]
