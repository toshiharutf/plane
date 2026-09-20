from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("db", "0136_usage_cost_breakdown")]
    operations = [
        migrations.AddField(model_name="workflowconfiguration", name="migration", field=models.JSONField(default=dict)),
        migrations.AddField(
            model_name="workflowconfiguration",
            name="legacy_scheduler_disabled",
            field=models.BooleanField(default=False),
        ),
    ]
