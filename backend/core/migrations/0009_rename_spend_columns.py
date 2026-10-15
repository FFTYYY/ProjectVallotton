"""
Spend accounting stops being about one vendor.

RenameField rather than add/drop: the columns hold every day of usage this
install has ever recorded, and a drop would quietly throw that away.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("core", "0008_folder_archive")]

    operations = [
        migrations.RenameField(
            model_name="dailyspend", old_name="grok_ticks", new_name="llm_ticks"
        ),
        migrations.RenameField(
            model_name="dailyspend", old_name="grok_requests", new_name="llm_requests"
        ),
    ]
