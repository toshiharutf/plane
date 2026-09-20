from django.db import migrations, models
from django.db.models import Count
from django.utils import timezone


def preserve_duplicates(apps, schema_editor):
    usage = apps.get_model("db", "AIUsageRecord")
    rows = usage.objects.using(schema_editor.connection.alias)
    groups = (
        rows.filter(deleted_at__isnull=True, issue__isnull=False)
        .exclude(session_id="")
        .values("issue_id", "session_id", "model")
        .annotate(deliveries=Count("id"))
        .filter(deliveries__gt=1)
    )
    for group in groups.iterator():
        key = {name: group[name] for name in ("issue_id", "session_id", "model")}
        delivered = list(rows.filter(**key, deleted_at__isnull=True).order_by("-updated_at", "-created_at", "id"))
        # Reports are cumulative snapshots, not additive charges. Keep the most
        # recently accepted snapshot and retain earlier rows for audit/reversal.
        rows.filter(pk__in=[row.pk for row in delivered[1:]]).update(
            duplicate_of=delivered[0].pk, deleted_at=timezone.now()
        )


def restore_duplicates(apps, schema_editor):
    usage = apps.get_model("db", "AIUsageRecord")
    usage.objects.using(schema_editor.connection.alias).filter(duplicate_of__isnull=False).update(
        duplicate_of=None, deleted_at=None
    )


class Migration(migrations.Migration):
    dependencies = [("db", "0133_workflow_v2")]
    operations = [
        migrations.AddField(
            model_name="aiusagerecord",
            name="duplicate_of",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.RunPython(preserve_duplicates, restore_duplicates),
        migrations.AddConstraint(
            model_name="aiusagerecord",
            constraint=models.UniqueConstraint(
                fields=("issue", "session_id", "model"),
                condition=models.Q(deleted_at__isnull=True, issue__isnull=False) & ~models.Q(session_id=""),
                name="ai_usage_issue_session_model_uniq",
            ),
        ),
    ]
