"""
The generation settings move from the browser onto the conversation.

Null for every existing row, which core.services.gensettings reads back as the
defaults — so nothing has to be backfilled and no conversation loses a setting
it never had.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0016_message_run_settled")]

    operations = [
        migrations.AddField(
            model_name="conversation",
            name="gen_settings",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
