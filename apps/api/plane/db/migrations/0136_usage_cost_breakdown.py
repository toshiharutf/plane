from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("db", "0135_workflow_write_guards")]
    operations = [
        migrations.AddField(
            model_name="aiusagerecord",
            name="cost_breakdown",
            field=models.JSONField(blank=True, editable=False, null=True),
        ),
    ]
