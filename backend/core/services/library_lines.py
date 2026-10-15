"""
The line writer: keeps an entry's tag lines in step with its text.

The lead model writes text and lines together and is answerable for both; it
is not told this writer exists. A person edits text, and may edit lines by
hand. After either, a model has to look at the entry again: read the text and
the lines as they stand -- each marked with who last changed it -- and hand
back the full list, keeping what the person wrote wherever the text lets it.
That call runs here, off the request, on the sweeper's clock, so the person
is not kept waiting on a model, and an entry whose call failed is tried again
later rather than losing the edit.

Two dates on the entry are the whole protocol: when the text last changed,
and when a model last wrote the lines or looked and let them stand. The
entry waits for a look while the first is later than the second. A person's
edit of the lines moves neither, so the writer still sees it; a look moves
the second whether or not it changed a word. Nothing is decided by whether
the entry has lines.
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

#: an entry whose call failed is left alone this long before the next try: a
#: model that is down would otherwise be asked about the same entry every
#: minute, and billed for the ones that half-answer
RETRY_AFTER = timedelta(minutes=10)
#: entries per sweep -- enough to drain an afternoon's edits within minutes,
#: few enough that a bulk import does not turn the sweeper into a billing run
PER_SWEEP = 3

_PREFIX = {Setting: "s", Character: "k", CharacterVariant: "v"}


def text_of(obj) -> str:
    return obj.lines_text


def brief(obj) -> str:
    """What the writer is shown: which entry, its text, the lines it has now
    and who last changed each of them."""
    if isinstance(obj, Setting):
        head = wording.LINES_FOR_SETTING.format(title=obj.title)
    elif isinstance(obj, Character):
        head = wording.LINES_FOR_CHARACTER.format(name=obj.name)
    else:
        head = wording.LINES_FOR_VARIANT.format(name=obj.character.name, title=obj.title)
    old = library.lines_of(obj.tags)
    lines = "\n".join(
        f"{ln['id']}  {ln['part']}：{ln['tags']}　"
        + (wording.LINE_BY_USER if ln.get("by") == library.LINE_BY_USER else wording.LINE_BY_AI)
        for ln in old) or wording.LINES_NONE_YET
    return f"{head}\n{text_of(obj)}\n\n{wording.LINES_CURRENT}\n{lines}"


def write(obj) -> bool:
    """Rewrite one entry's lines from its text. Says whether it did."""
    model = type(obj)
    prefix = _PREFIX[model]
    tried = timezone.now()
    try:
        raw, _usage = llm.write_lines(brief(obj))
        new = library.lines_of(raw)
    except Exception as exc:  # noqa: BLE001 -- the sweeper carries on; this entry waits
        log.warning("line writer failed on %s %s: %s", model.__name__, obj.id, str(exc)[:200])
        model.objects.filter(id=obj.id).update(lines_tried_at=tried)
        return False
    if not new:
        # an answer with no lines is not an answer: written as given it would
        # wipe the entry's lines, the user's own among them
        log.warning("line writer gave no lines for %s %s; the entry keeps its lines and waits",
                    model.__name__, obj.id)
        model.objects.filter(id=obj.id).update(lines_tried_at=tried)
        return False
    taken = library.variant_ids_taken(obj.character, exclude=obj.id) if model is CharacterVariant else None
    old = library.lines_of(obj.tags)
    obj.tags = library.stamp_by(old, library.assign_ids(old, new, prefix, taken=taken), "ai")
    # dated when the look began: text edited while the model was reading is
    # text it did not see, and the entry waits again
    obj.lines_ai_at = tried
    obj.lines_tried_at = tried
    obj.save(update_fields=["tags", "lines_ai_at", "lines_tried_at", "updated_at"])
    log.info("lines written for %s %s: %d line(s)", model.__name__, obj.id, len(obj.tags))
    return True


def stale(limit: int = PER_SWEEP) -> list:
    """The entries due for a rewrite, oldest edit first, failures rested."""
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
    """One pass: rewrite what is due. Returns how many entries were written."""
    done = 0
    for obj in stale(limit):
        if write(obj):
            done += 1
    return done
