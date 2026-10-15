"""
The generation settings, which belong to a conversation.

出图数量, 反思轮数, 连续生成, 常驻参考. They used to be one set of values in
the browser's localStorage, shared by every conversation at once — so a run of
ten pictures armed in one chat was still armed when you opened another, and
turning reflection up for a portrait turned it up for everything. They are
decisions about a piece of work, and a piece of work is a conversation.

Stored as one JSON column rather than five, because they are read and written
as a unit and the API hands them over as a unit. What keeps that honest is
this module: nothing else decides what a missing value means or how far a
value may go, so a conversation saved before this existed reads back as the
defaults rather than as nulls.
"""

from __future__ import annotations

from django.conf import settings
from django.db import transaction

from core.models import AppSettings, Conversation

#: What a new conversation starts with, and what an old one reads back as.
#: Reflection at 1 on purpose: one second look is the setting that earns its
#: keep, and a slider that starts at 0 is a feature nobody turns on.
#: The quality tiers a conversation can be set to.
#:
#: Named here and not in the drawing service because they are a choice the app
#: offers, not a fact about a vendor: what each one means — which model, which
#: size band, what it costs — is the drawing service's to define, and swapping
#: that service out should not change what the settings page can say.
QUALITY_TIERS = ("low", "mid", "high")

#: Whether the app may approve the model's library edits without asking.
#:
#: Named here for the same reason the quality tiers are: what the app offers is
#: a setting, what each one means is services.proposals' to decide. Nothing
#: else in the app reads this — a proposal that was approved automatically and
#: one a person approved are the same row in the same state.
AUTO_LIBRARY_MODES = ("off", "edits", "all")

DEFAULTS: dict = {
    "n_images": 1,
    # mid, so a conversation saved before this existed reads back as what it
    # was actually being drawn at
    "quality": "mid",
    # off, because a conversation saved before this existed was one where every
    # edit waited for a person, and a migration must not quietly start writing
    # to anyone's library on its own
    "auto_library": "off",
    "reflection_rounds": 1,
    "continuous_rounds": 1,
    "continuous_phrase_id": None,
    "sticky_refs": False,
}


def _int(raw, low: int, high: int, fallback: int) -> int:
    try:
        return min(max(int(raw), low), high)
    except (TypeError, ValueError):
        return fallback


def clean(raw: dict | None) -> dict:
    """One complete, in-range set of settings, whatever came in."""
    raw = raw or {}
    phrase = raw.get("continuous_phrase_id")
    return {
        "n_images": _int(
            raw.get("n_images"), 1, settings.MAX_IMAGES_PER_REQUEST, DEFAULTS["n_images"]
        ),
        "reflection_rounds": _int(
            raw.get("reflection_rounds"), 0, settings.MAX_REFLECTION_ROUNDS,
            DEFAULTS["reflection_rounds"],
        ),
        "continuous_rounds": _int(
            raw.get("continuous_rounds"), 1, settings.MAX_CONTINUOUS_ROUNDS,
            DEFAULTS["continuous_rounds"],
        ),
        "continuous_phrase_id": str(phrase) if phrase else None,
        "sticky_refs": bool(raw.get("sticky_refs")),
        "quality": (
            raw["quality"] if raw.get("quality") in QUALITY_TIERS
            else DEFAULTS["quality"]
        ),
        "auto_library": (
            raw["auto_library"] if raw.get("auto_library") in AUTO_LIBRARY_MODES
            else DEFAULTS["auto_library"]
        ),
    }


def of(conv: Conversation) -> dict:
    """What this conversation is set to."""
    return clean(conv.gen_settings)


def merge(conv: Conversation, patch: dict | None) -> dict:
    """
    Apply a partial change and store it. Returns the settings as they now are.

    Partial on purpose: the right rail changes one control at a time, and a
    whole-object write would let a stale rail undo a change made in another
    tab between its read and its write.

    Under a lock because the rail sends one request per control and a hand can
    move four of them in a second: read-modify-write without it meant two of
    those four arrived together, both read the same settings, and the second
    to finish wrote away the first one's change.
    """
    with transaction.atomic():
        locked = (
            Conversation.objects.select_for_update().filter(id=conv.id).first()
        )
        if locked is None:
            return of(conv)
        merged = clean({**clean(locked.gen_settings), **(patch or {})})
        Conversation.objects.filter(id=conv.id).update(gen_settings=merged)
    conv.refresh_from_db(fields=["gen_settings"])
    return merged


# ------------------------------------------------------- the app's own defaults


def defaults() -> dict:
    """
    What a new conversation starts from.

    DEFAULTS is the floor under this: the factory setting, used until the user
    saves one of their own and whenever what they saved is unreadable.
    """
    return clean(AppSettings.get().gen_defaults)


def set_defaults(patch: dict | None) -> dict:
    """
    Change the defaults. Existing conversations keep what they already have.

    Locked for the same reason merge() is: the settings page sends one request
    per control, and two arriving together would otherwise both read the old
    defaults and the slower one would write away the other's change.
    """
    with transaction.atomic():
        row = AppSettings.get()
        locked = AppSettings.objects.select_for_update().filter(id=row.id).first()
        current = clean(locked.gen_defaults) if locked else DEFAULTS
        merged = clean({**current, **(patch or {})})
        AppSettings.objects.filter(id=1).update(gen_defaults=merged)
    return merged
