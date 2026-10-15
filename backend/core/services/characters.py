"""
One rule about characters, kept by the system rather than by its callers.

**A character is reachable only through a variant**, so a character with none
is a row that shows up in every list and can never be switched on: the toggle
lights up, the server answers with an empty set, and the toggle goes out again.

The obvious way to hold that up is to refuse the delete that would cause it —
and that way is wrong twice over. It puts the rule at one entrance, so every
other way in (the model's library edits, MCP, the admin, a shell) walks past
it; and it makes the caller responsible for knowing the rule, which means the
browser needs a count of the variants to decide whether to offer the button.
That count goes stale the moment a second tab deletes one, and the user is left
pressing a control that looks available and is not.

So the rule is not a guard, it is a repair: **a character with no variants is
given one**, whenever anybody looks. Deleting the last variant is allowed and
leaves a 默认 behind it; a character created without variants gets the same
thing; and neither the order the requests arrive in nor how many tabs are open
can produce a state that lasts.

`repair` is the mechanism. The call right after a delete is not a second rule —
it is the same one, run early so the answer already carries the new variant
instead of appearing a moment later.
"""

from __future__ import annotations

import logging

from django.db.models import Count

from core.models import Character, CharacterVariant
from core import ui_text

log = logging.getLogger(__name__)

#: What an otherwise empty character is given. The name a person would have
#: typed, because that is what they will see.
DEFAULT_TITLE = ui_text.DEFAULT_VARIANT_TITLE


def repair(characters) -> int:
    """
    Give a variant to any of these characters that has none.

    Takes whatever names some characters — a queryset, a list, a single one —
    so a caller never has to build the right shape to ask.
    """
    if isinstance(characters, Character):
        ids = [characters.id]
    elif hasattr(characters, "values_list"):
        ids = list(characters.values_list("id", flat=True))
    else:
        ids = [c.id for c in characters]
    if not ids:
        return 0

    empty = list(
        Character.objects.filter(id__in=ids)
        .annotate(n=Count("variants"))
        .filter(n=0)
        .values_list("id", flat=True)
    )
    if not empty:
        return 0

    # ignore_conflicts so two callers repairing the same character at once
    # leave it with one variant rather than one of them failing
    CharacterVariant.objects.bulk_create(
        [
            CharacterVariant(character_id=cid, title=DEFAULT_TITLE, content="", position=1)
            for cid in empty
        ],
        ignore_conflicts=True,
    )
    log.info("gave %d character(s) the variant they were missing", len(empty))
    return len(empty)


def repair_in(folder_ids) -> int:
    """The same, for everything in these folders — what a read passes through."""
    return repair(Character.objects.filter(folder_id__in=list(folder_ids)))
