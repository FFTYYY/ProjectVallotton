"""
One row for the app's own preferences — the defaults a new conversation starts
from. Created on first read rather than by this migration, so there is one
place that decides what an absent row means.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0017_conversation_gen_settings")]

    operations = [
        migrations.CreateModel(
            name="AppSettings",
            fields=[
                ("id", models.PositiveSmallIntegerField(default=1, primary_key=True,
                                                        serialize=False)),
                ("gen_defaults", models.JSONField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"verbose_name_plural": "app settings"},
        ),
    ]
