"""Join the existing In Review migration and local workflow v2 branches."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("db", "0132_in_review_state"),
        ("db", "0138_workflow_followup"),
    ]

    operations = []
