# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("db", "0122_alter_draftissue_assignees_alter_issue_assignees_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="issue",
            name="start_datetime",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="issue",
            name="target_datetime",
            field=models.DateTimeField(blank=True, null=True),
        ),
        # Backfill existing dates at 00:00 in the project timezone, so the local date stays the same
        migrations.RunSQL(
            sql="""
                UPDATE issues AS i
                SET start_datetime = CASE WHEN i.start_date IS NULL THEN NULL
                        ELSE i.start_date::timestamp AT TIME ZONE COALESCE(NULLIF(p.timezone, ''), 'UTC') END,
                    target_datetime = CASE WHEN i.target_date IS NULL THEN NULL
                        ELSE i.target_date::timestamp AT TIME ZONE COALESCE(NULLIF(p.timezone, ''), 'UTC') END
                FROM projects AS p
                WHERE p.id = i.project_id
                  AND (i.start_date IS NOT NULL OR i.target_date IS NOT NULL);
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
