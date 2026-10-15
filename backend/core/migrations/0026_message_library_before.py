from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0025_entry_line_dates"),
    ]

    operations = [
        migrations.AddField(
            model_name="message",
            name="library_before",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
