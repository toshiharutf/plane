# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from importlib import import_module

from django.db import migrations


def copy_remaining_work_item_ai_usage(apps, schema_editor):
    """Copy rows written to WorkItemAIUsage after 0128 ran, so none are lost by the drop.

    The 0128 copy skips rows it already copied, so running it again only adds the new ones.
    """
    import_module("plane.db.migrations.0128_aiusagerecord").copy_work_item_ai_usage(apps, schema_editor)


class Migration(migrations.Migration):
    # Runs only after 0128 created AIUsageRecord and copied every WorkItemAIUsage row.
    dependencies = [
        ("db", "0128_aiusagerecord"),
    ]

    operations = [
        migrations.RunPython(copy_remaining_work_item_ai_usage, migrations.RunPython.noop),
        migrations.DeleteModel(name="WorkItemAIUsage"),
    ]
