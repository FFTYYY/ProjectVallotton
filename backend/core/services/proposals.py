"""
Approving a library proposal, and the one optional shortcut to it.

A proposal is the lead's edit to the PROJECT's library: a setting or a
character (with its variants) written whole, keyed by name. The
conversation's own entries never come here -- the lead writes those
directly (library.write_conversation_entries); the project is the user's
library, so an edit to it waits for the button, or for the shortcut below.

Everything that writes a proposal into a library goes through `apply` — the
button in the UI and the automatic path alike. The shortcut is
`auto_approve`: it decides whether the app may press the button on the
user's behalf, and if it may not, it does nothing at all.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from core.models import (
    Character,
    CharacterVariant,
    Conversation,
    Folder,
    LibraryProposal,
    Setting,
)
from core.services import characters as characters_service, library
from core import ui_text

log = logging.getLogger(__name__)


class NameTaken(Exception):
    """A project write that would put a second entry of one name in the
    project. Nothing is written; `name` is the first name that collides."""

    def __init__(self, name: str):
        super().__init__(name)
        self.name = name


#: What the app may approve without being asked.
OFF = "off"
#: Changes to the library as it already stands, plus new costumes on characters
#: that are already in it. What waits for a person is a new ENTRY — a setting or
#: a character — because that is how a library grows a second copy of something
#: it already has under a slightly different name.
EDITS_ONLY = "edits"
ALL = "all"
MODES = (OFF, EDITS_ONLY, ALL)


def _nearest_in_chain(model, folder: Folder, **lookup):
    """The nearest existing project-layer entry matching `lookup`, up the
    folder chain: a conversation reads its whole ancestry, so the entry it
    proposes an edit to may live several levels up."""
    for f in reversed(folder.ancestors()):
        found = model.objects.filter(folder=f, conversation__isnull=True, **lookup).first()
        if found:
            return found
    return None


def _in_conversation(model, conversation: Conversation, **lookup):
    return model.objects.filter(conversation=conversation, **lookup).first()


def _key(item: dict) -> str:
    _, key, _ = library.parse_key(str(item.get("key") or ""))
    return key


def _variant_title(item: dict) -> str:
    _, _, title = library.parse_key(str(item.get("key") or ""))
    return title or str(item.get("key") or "").strip()


def creates_an_entry(proposal: LibraryProposal) -> bool:
    """
    Whether applying this would bring a new SETTING or CHARACTER into the
    PROJECT library. A new costume on a character already there is not one.
    Asked against the library as it stands right now.
    """
    folder = proposal.folder
    payload = proposal.payload or {}
    if not (payload.get("settings") or payload.get("characters")):
        return False
    if folder is None:
        return True
    for entry in payload.get("settings") or []:
        key = _key(entry)
        if key and _nearest_in_chain(Setting, folder, title=key) is None \
                and _in_conversation(Setting, proposal.conversation, title=key) is None:
            return True
    for entry in payload.get("characters") or []:
        key = _key(entry)
        if key and _nearest_in_chain(Character, folder, name=key) is None \
                and _in_conversation(Character, proposal.conversation, name=key) is None:
            return True
    return False


def _taken_in_project(payload: dict, conv: Conversation, folder: Folder) -> str | None:
    """The first entry of `payload` that is this conversation's own AND
    already named in the project chain: it cannot move up, and merging two
    entries is the user's call."""
    for entry in payload.get("settings") or []:
        key = _key(entry)
        if key and _in_conversation(Setting, conv, title=key) is not None \
                and _nearest_in_chain(Setting, folder, title=key) is not None:
            return key
    for entry in payload.get("characters") or []:
        key = _key(entry)
        if key and _in_conversation(Character, conv, name=key) is not None \
                and _nearest_in_chain(Character, folder, name=key) is not None:
            return key
    return None


LINES = ("positive", "negative", "prose")


def _write_fields(obj, item: dict, *, now) -> None:
    """The edit says the whole entry: a field it gives replaces the one
    stored; the note is kept when the edit leaves it blank. An edit that gives
    the note alone (a proposal carried over from before entries) leaves the
    tags and prose to the patrol, which fills them from the note."""
    note = str(item.get("note") or "")
    if note and not library.same_words(note, obj.lines_text):
        library.set_text(obj, note, now=now)
    if not any(field in item for field in LINES):
        return
    obj.positive = library.split_tags(item.get("positive"))
    obj.negative = library.split_tags(item.get("negative"))
    obj.prose = str(item.get("prose") or "").strip()
    library.lines_by_model(obj, now=now)


def _write_setting(entry: dict, *, folder: Folder, conv: Conversation, done: list[str]) -> Setting | None:
    key = _key(entry)
    if not key:
        return None
    now = timezone.now()
    existing = _nearest_in_chain(Setting, folder, title=key)
    own = _in_conversation(Setting, conv, title=key)
    if existing is None and own is not None:
        # promoted: the conversation's entry becomes the project's
        existing = own
        existing.conversation = None
        existing.folder = folder
        existing.origin_seq = None
    if existing is not None:
        if entry.get("category"):
            existing.category = str(entry["category"]).strip()[:40]
        _write_fields(existing, entry, now=now)
        existing.save()
        done.append(ui_text.PROPOSAL_UPDATED_SETTING.format(title=key))
        return None
    pos = (folder.settings.aggregate(m=Max("position"))["m"] or 0) + 1
    born = Setting(folder=folder, conversation=None, title=key, position=pos,
                   category=str(entry.get("category") or "").strip()[:40])
    _write_fields(born, entry, now=now)
    born.save()
    done.append(ui_text.PROPOSAL_CREATED_SETTING.format(title=key))
    return born


def _write_character(entry: dict, *, folder: Folder, conv: Conversation, done: list[str]) -> list[CharacterVariant]:
    key = _key(entry)
    if not key:
        return []
    now = timezone.now()
    born: list[CharacterVariant] = []
    existing = _nearest_in_chain(Character, folder, name=key)
    own = _in_conversation(Character, conv, name=key)
    if existing is None and own is not None:
        existing = own
        existing.conversation = None
        existing.folder = folder
        existing.origin_seq = None
    if existing is None:
        pos = (folder.characters.aggregate(m=Max("position"))["m"] or 0) + 1
        existing = Character(folder=folder, conversation=None, name=key, position=pos)
        _write_fields(existing, entry, now=now)
        existing.save()
        done.append(ui_text.PROPOSAL_CREATED_CHARACTER.format(name=key))
    elif not entry.get("carrier"):
        # a carrier brings costumes only and leaves its character as it is
        _write_fields(existing, entry, now=now)
        existing.save()
        done.append(ui_text.PROPOSAL_UPDATED_CHARACTER.format(name=key))
    for v in entry.get("variants") or []:
        title = _variant_title(v)
        if not title:
            continue
        variant = CharacterVariant.objects.filter(character=existing, title=title).first()
        if variant is None:
            vpos = (existing.variants.aggregate(m=Max("position"))["m"] or 0) + 1
            variant = CharacterVariant(character=existing, title=title, position=vpos)
            _write_fields(variant, v, now=now)
            variant.save()
            born.append(variant)
            done.append(ui_text.PROPOSAL_CREATED_VARIANT.format(name=key, title=title))
        else:
            _write_fields(variant, v, now=now)
            variant.save()
            done.append(ui_text.PROPOSAL_UPDATED_VARIANT.format(name=key, title=title))
    # a character is drawn through a costume: one with none gets the default
    if not existing.variants.exists():
        characters_service.repair(existing)
        born.extend(existing.variants.all())
    return born


def apply(proposal: LibraryProposal, *, select_new: bool = False) -> str:
    """
    Write a proposal into the project. Returns a human summary.

    `select_new` switches this conversation on to whatever gets CREATED here —
    an entry made and then left unselected is one the lead cannot see on the
    next turn.
    """
    conv = proposal.conversation
    folder = proposal.folder
    if folder is None:
        return ""
    payload = proposal.payload or {}
    taken = _taken_in_project(payload, conv, folder)
    if taken is not None:
        raise NameTaken(taken)
    done: list[str] = []
    born_settings: list[Setting] = []
    born_variants: list[CharacterVariant] = []
    for entry in payload.get("settings") or []:
        made = _write_setting(entry, folder=folder, conv=conv, done=done)
        if made:
            born_settings.append(made)
    for entry in payload.get("characters") or []:
        born_variants += _write_character(entry, folder=folder, conv=conv, done=done)
    if select_new:
        if born_settings:
            conv.selected_settings.add(*born_settings)
        if born_variants:
            conv.selected_variants.add(*born_variants)
    return ui_text.PROPOSAL_SUMMARY_SEP.join(done)


def record(conversation: Conversation, message, payload: dict) -> LibraryProposal:
    """Write a proposal down; it waits for approval, or for auto_approve."""
    return LibraryProposal.objects.create(
        conversation=conversation, message=message, folder=conversation.folder, payload=payload,
    )


def resolve(proposal_id, *, approve: bool, select_new: bool = False) -> LibraryProposal | None:
    """
    Settle one proposal. Returns it as it now stands, or None if there is none.
    """
    with transaction.atomic():
        p = LibraryProposal.objects.select_for_update().filter(id=proposal_id).first()
        if p is None:
            return None
        if p.status != "pending":
            return p
        if approve:
            if p.folder is None:
                return p  # nowhere to write it; it stays pending
            summary = apply(p, select_new=select_new)
            p.applied_summary = "；".join(x for x in (p.applied_summary, summary) if x)
            p.status = "applied"
        else:
            p.status = "rejected"
        p.resolved_at = timezone.now()
        p.save(update_fields=["status", "applied_summary", "resolved_at"])
    return p


def auto_approve(proposal: LibraryProposal, mode: str) -> bool:
    """
    Press the approve button for the user, if this setting says we may.
    Says whether it did. Returning False leaves the proposal pending.
    """
    if mode == OFF or proposal.folder is None:
        return False
    if mode == EDITS_ONLY and creates_an_entry(proposal):
        return False
    if mode not in MODES:
        return False
    if not LibraryProposal.objects.filter(id=proposal.id, status="pending").exists():
        return False
    try:
        done = resolve(proposal.id, approve=True, select_new=True)
    except NameTaken as exc:
        log.info("library proposal %s not applied automatically: project already has %r", proposal.id, exc.name)
        return False
    applied = bool(done and done.status == "applied")
    if applied:
        log.info("library proposal %s applied automatically (%s)", proposal.id, mode)
    return applied
