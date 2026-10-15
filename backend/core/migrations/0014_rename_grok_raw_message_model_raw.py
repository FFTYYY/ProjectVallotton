"""
grok_raw -> model_raw.

The column holds the lead model's whole answer. It was named after the vendor
that happened to be the only one at the time; there are two now and the vendor
is a setting, so the name was quietly telling every future reader something
untrue — the same way core/services/grok.py did before it became llm.py.

Written by hand rather than generated: makemigrations asks "did you rename?"
interactively, and answering it wrong produces a drop-and-add that throws away
every stored answer in the database.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("core", "0013_message_sticky_refs")]

    operations = [
        migrations.RenameField(
            model_name="message",
            old_name="grok_raw",
            new_name="model_raw",
        ),
    ]
