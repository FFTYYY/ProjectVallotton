# Proposals still waiting from before entries. Their conversation-layer part
# was written when they were made; what waits is the part marked for the
# project (scope "project"): settings with a title, a text and tag lines,
# characters with a name, a description, tag lines and variants. That part is
# carried over the way 0030 carried the entries themselves: the text becomes
# the note and nothing else is said, so approving it writes the note and
# leaves the tags and prose for the patrol to fill. An item with no text (an
# edit of the lines alone) means nothing any more and is dropped; a proposal
# left with nothing is closed as rejected.

from django.db import migrations
from django.utils import timezone

PROJECT_PREFIX = "@"
VARIANT_SEP = "/"


def _text(item: dict, field: str) -> str:
    return str(item.get(field) or "").strip()


def converted(payload) -> dict | None:
    """The waiting part of an earlier proposal in the entries' shape; None
    when nothing in it can be carried over. A payload already in the entries'
    shape comes back as it is."""
    if not isinstance(payload, dict):
        return None
    items = [i for kind in ("settings", "characters") for i in payload.get(kind) or []]
    if items and all(isinstance(i, dict) and "key" in i for i in items):
        return payload
    settings = []
    for s in payload.get("settings") or []:
        if not isinstance(s, dict) or s.get("scope") != "project":
            continue
        title, note = _text(s, "title"), _text(s, "content")
        if title and note:
            settings.append({"key": PROJECT_PREFIX + title, "note": note})
    characters = []
    for c in payload.get("characters") or []:
        if not isinstance(c, dict) or c.get("scope") != "project":
            continue
        name = _text(c, "name")
        if not name:
            continue
        variants = []
        for v in c.get("variants") or []:
            title, note = _text(v, "title"), _text(v, "content")
            if isinstance(v, dict) and title and note:
                variants.append({"key": name + VARIANT_SEP + title, "note": note})
        note = _text(c, "description")
        if note or variants:
            characters.append({"key": PROJECT_PREFIX + name, "note": note, "variants": variants})
    if not settings and not characters:
        return None
    return {"settings": settings, "characters": characters}


def carry_over(apps, _schema_editor):
    proposal = apps.get_model("core", "LibraryProposal")
    for p in proposal.objects.filter(status="pending"):
        new = converted(p.payload)
        if new is None:
            proposal.objects.filter(id=p.id).update(status="rejected", resolved_at=timezone.now())
        elif new is not p.payload:
            proposal.objects.filter(id=p.id).update(payload=new)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0030_entries"),
    ]

    operations = [
        migrations.RunPython(carry_over, migrations.RunPython.noop),
    ]
