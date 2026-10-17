"""
The patrol: keeps an entry's tags and prose in step with its note.

The lead writes entries whole and is answerable for them. A person edits a
note, in the project page or the sidebar, and may edit the tags by hand.
After a note edit a model has to look at the entry again: read the note and
what the entry has now, and hand back the tags and the prose, whole. That
call runs here, off the request, on the sweeper's clock, so the person is
not kept waiting on a model, and an entry whose call failed is tried again
later rather than losing the edit.

Two dates on the entry are the whole protocol: when the note last changed,
and when a model last filled the entry or looked and let it stand. The
entry waits for a look while the first is later than the second.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db.models import F, Q
from django.utils import timezone

from core.models import Character, CharacterVariant, Setting
from core.prompts import wording
from core.services import library, llm

log = logging.getLogger(__name__)

#: an entry whose call failed is left alone this long before the next try
RETRY_AFTER = timedelta(minutes=10)
#: entries per sweep -- enough to drain an afternoon's edits within minutes,
#: few enough that a bulk import does not turn the sweeper into a billing run
PER_SWEEP = 3


def brief(obj) -> str:
    """What the patrol is shown: which entry, its note, and what it has now."""
    if isinstance(obj, Setting):
        head = wording.FILL_FOR_SETTING.format(title=obj.title, category=obj.category or wording.FILL_CATEGORY_OTHER)
    elif isinstance(obj, Character):
        head = wording.FILL_FOR_CHARACTER.format(name=obj.name)
    else:
        head = wording.FILL_FOR_VARIANT.format(name=obj.character.name, title=obj.title)
    e = library.entry_dict(obj)
    now = [
        f"{wording.ENTRY_POSITIVE_LABEL}：{', '.join(e['positive']) or wording.FILL_NONE_YET}",
        f"{wording.ENTRY_NEGATIVE_LABEL}：{', '.join(e['negative']) or wording.FILL_NONE_YET}",
        f"{wording.ENTRY_PROSE_LABEL}：{e['prose'] or wording.FILL_NONE_YET}",
    ]
    return f"{head}\n{wording.ENTRY_NOTE_LABEL}：{obj.lines_text}\n\n{wording.FILL_CURRENT}\n" + "\n".join(now)


def write(obj) -> bool:
    """Fill one entry from its note. Says whether it did."""
    model = type(obj)
    tried = timezone.now()
    try:
        filled, _usage = llm.fill_entry(brief(obj))
    except Exception as exc:  # noqa: BLE001 -- the sweeper carries on; this entry waits
        log.warning("patrol failed on %s %s: %s", model.__name__, obj.id, str(exc)[:200])
        model.objects.filter(id=obj.id).update(lines_tried_at=tried)
        return False
    if not (filled["positive"] or filled["prose"]):
        log.warning("patrol gave nothing for %s %s; the entry waits", model.__name__, obj.id)
        model.objects.filter(id=obj.id).update(lines_tried_at=tried)
        return False
    obj.positive = filled["positive"]
    obj.negative = filled["negative"]
    obj.prose = filled["prose"]
    # dated when the look began: a note edited while the model was reading is
    # one it did not see, and the entry waits again
    obj.lines_ai_at = tried
    obj.lines_tried_at = tried
    obj.save(update_fields=["positive", "negative", "prose", "lines_ai_at", "lines_tried_at", "updated_at"])
    log.info("entry filled for %s %s: %d tag(s)", model.__name__, obj.id, len(obj.positive))
    return True


def stale(limit: int = PER_SWEEP) -> list:
    """The entries due for a fill, oldest edit first, failures rested."""
    cutoff = timezone.now() - RETRY_AFTER
    out: list = []
    for model in (Setting, Character, CharacterVariant):
        room = limit - len(out)
        if room <= 0:
            break
        text = "description" if model is Character else "content"
        qs = (model.objects.filter(text_edited_at__isnull=False).exclude(**{text: ""})
              .filter(Q(lines_ai_at__isnull=True) | Q(lines_ai_at__lt=F("text_edited_at")))
              .filter(Q(lines_tried_at__isnull=True) | Q(lines_tried_at__lt=cutoff))
              .order_by("text_edited_at"))
        out.extend(o for o in qs[:room * 2] if o.lines_stale)
        out = out[:limit]
    return out


def sweep(limit: int = PER_SWEEP) -> int:
    """One pass: fill what is due. Returns how many entries were written."""
    done = 0
    for obj in stale(limit):
        if write(obj):
            done += 1
    return done
