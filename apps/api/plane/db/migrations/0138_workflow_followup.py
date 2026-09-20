import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("db", "0137_workflow_migration_cutover")]
    operations = [
        migrations.AddField(
            model_name="workcontinuation",
            name="followup_of",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.PROTECT, related_name="workflow_followups", to="db.issue"
            ),
        ),
        migrations.AddField(
            model_name="workcontinuation", name="proposal_key", field=models.CharField(blank=True, max_length=255)
        ),
        migrations.AddConstraint(
            model_name="workcontinuation",
            constraint=models.UniqueConstraint(
                fields=["project", "followup_of", "proposal_key"],
                condition=models.Q(followup_of__isnull=False),
                name="workflow_followup_proposal",
            ),
        ),
    ]
