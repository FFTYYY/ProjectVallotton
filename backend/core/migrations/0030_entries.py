# Entries: an entry is a note, a positive tag list, a negative tag list and a
# prose line. The tag lines (numbered, picked by id, spliced byte for byte) are
# gone. What an entry already had stays as its note; its tags and prose start
# empty and are marked for the patrol to fill from the note.

from django.db import migrations, models
from django.utils import timezone


def mark_for_the_patrol(apps, _schema_editor):
    now = timezone.now()
    for name, text in (("Setting", "content"), ("Character", "description"), ("CharacterVariant", "content")):
        model = apps.get_model("core", name)
        for row in model.objects.exclude(**{text: ""}):
            model.objects.filter(id=row.id).update(text_edited_at=now, lines_ai_at=None, lines_tried_at=None)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0029_entry_one_owner"),
    ]

    operations = [
        migrations.AddField(model_name="setting", name="positive", field=models.JSONField(blank=True, default=list)),
        migrations.AddField(model_name="setting", name="negative", field=models.JSONField(blank=True, default=list)),
        migrations.AddField(model_name="setting", name="prose", field=models.TextField(blank=True, default="")),
        migrations.AddField(model_name="setting", name="origin_seq", field=models.BigIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="setting", name="category", field=models.CharField(blank=True, default="", max_length=40)),
        migrations.AddField(model_name="character", name="positive", field=models.JSONField(blank=True, default=list)),
        migrations.AddField(model_name="character", name="negative", field=models.JSONField(blank=True, default=list)),
        migrations.AddField(model_name="character", name="prose", field=models.TextField(blank=True, default="")),
        migrations.AddField(model_name="character", name="origin_seq", field=models.BigIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="charactervariant", name="positive", field=models.JSONField(blank=True, default=list)),
        migrations.AddField(model_name="charactervariant", name="negative", field=models.JSONField(blank=True, default=list)),
        migrations.AddField(model_name="charactervariant", name="prose", field=models.TextField(blank=True, default="")),
        migrations.AddField(model_name="charactervariant", name="origin_seq", field=models.BigIntegerField(blank=True, null=True)),
        migrations.RunPython(mark_for_the_patrol, migrations.RunPython.noop),
        migrations.RemoveField(model_name="setting", name="tags"),
        migrations.RemoveField(model_name="character", name="tags"),
        migrations.RemoveField(model_name="charactervariant", name="tags"),
    ]
