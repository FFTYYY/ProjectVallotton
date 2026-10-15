"""
A turn can only be counted by its continuous run once.

Written by hand for the same reason 0014 was: a plain default=False on an
existing table is all this needs, and makemigrations is not worth the prompt.

Existing rows default to False, which is correct — every one of them belongs
to a run that has already ended, and advance() ignores a conversation with no
run at all, so none of them can be settled a second time.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0015_conversation_continuous_run")]

    operations = [
        migrations.AddField(
            model_name="message",
            name="run_settled",
            field=models.BooleanField(default=False),
        ),
    ]
