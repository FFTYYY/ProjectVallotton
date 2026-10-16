"""
Approving a library proposal, and the one optional shortcut to it.

Everything that writes a proposal into a library goes through `approve` —
the button in the UI and the automatic path alike. It lived in the view, which
meant an automatic approval would have had to import a view from a service, or
grow a second implementation. There is one, and it is here.

A proposal's entries say which layer they go to. The project layer — the
folder — is the user's library: those wait for the button, or for the
shortcut below. The conversation layer is the model's own notes for this one
conversation — a character it made up for this story, the rain that falls in
it — and those are written as soon as the proposal is recorded, whatever the
setting says: they live and die with the conversation, and the sidebar lets
the user delete or promote them.

The shortcut is `auto_approve`, and it is deliberately the smallest thing that
could work: it decides whether the app may press the button on the user's
behalf, and if it may not, it does nothing at all. Nothing downstream is told
which of the two pressed it — a proposal that was auto-applied and one the user
approved are the same row in the same state, so the transcript, the projects
page and the library itself never learn this setting exists.
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
from core.prompts import wording
from core.services import characters as characters_service, library
from core import ui_text

log = logging.getLogger(__name__)


class NameTaken(Exception):
    """A project write that would put a second entry of one name in the
    project. Nothing is written; `name` is the first name that collides."""

    def __init__(self, name: str):
        super().__init__(name)
        self.name = name


def _taken_in_project(payload: dict, conv: Conversation, folder: Folder) -> str | None:
    """The first entry of `payload` that is this conversation's own AND already
    named in the project chain: it cannot move up (a name is unique within a
    layer), and merging two entries is the user's call, as it is for the
    「存到项目」 button."""
    for entry in payload.get("settings") or []:
        title = str(entry.get("title") or "").strip()
        if title and _in_conversation(Setting, conv, title=title) is not None \
                and _nearest_in_chain(Setting, folder, title=title) is not None:
            return title
    for entry in payload.get("characters") or []:
        name = str(entry.get("name") or "").strip()
        if name and _in_conversation(Character, conv, name=name) is not None \
                and _nearest_in_chain(Character, folder, name=name) is not None:
            return name
    return None

#: What the app may approve without being asked.
#:
#: Named here because they are this module's decision to make; gensettings only
#: needs to know that there are three of them and which is the default.
OFF = "off"
#: Changes to the library as it already stands, plus new costumes on characters
#: that are already in it. What waits for a person is a new ENTRY — a setting or
#: a character — because that is how a library grows a second copy of something
#: it already has under a slightly different name, which is a judgement about
#: the shape of the library rather than about this one turn.
#:
#: A costume is not that. It is a detail of an entry that already exists, and
#: the person already decided that entry belongs.
EDITS_ONLY = "edits"
ALL = "all"
MODES = (OFF, EDITS_ONLY, ALL)


def _nearest_in_chain(model, folder: Folder, **lookup):
    """
    The nearest existing project-layer entry matching `lookup`, searched up
    the folder chain.

    A conversation reads its whole ancestry, so an entry the model can see —
    and therefore an entry it will propose an edit to — may live several levels
    up. Matching only the proposal's own folder turns "update 林" into a second
    林 one level down, which is the one thing a library exists to prevent.
    """
    for f in reversed(folder.ancestors()):
        found = model.objects.filter(folder=f, conversation__isnull=True, **lookup).first()
        if found:
            return found
    return None


def _in_conversation(model, conversation: Conversation, **lookup):
    return model.objects.filter(conversation=conversation, **lookup).first()


def _is_project(entry: dict) -> bool:
    """Only an entry that says so goes to the project: that is the layer the
    user asks for by name; everything else is this conversation's own."""
    return entry.get("scope") == "project"


def split_layers(payload: dict) -> tuple[dict, dict]:
    """The proposal's entries by layer: (conversation layer, project layer)."""
    conv = {"settings": [], "characters": []}
    proj = {"settings": [], "characters": []}
    for kind in ("settings", "characters"):
        for entry in payload.get(kind) or []:
            (proj if _is_project(entry) else conv)[kind].append(entry)
    return conv, proj


def creates_an_entry(proposal: LibraryProposal) -> bool:
    """
    Whether applying this would bring a new SETTING or CHARACTER into the
    PROJECT library.

    A new costume on a character that is already in the library is not one:
    the entry it belongs to exists, someone already decided it belongs, and a
    costume cannot become the duplicate-under-a-different-name that this
    question is asked to prevent. A costume on a character that does NOT exist
    is covered anyway — the character itself answers yes. Nor is an entry on
    the conversation layer: it never reaches the project.

    Asked against the library as it stands right now, for the same reason
    `apply` looks it up rather than trusting the payload: the model says what
    it wants the library to contain, not whether that is a create or an edit,
    and the answer changes as the library does.
    """
    folder = proposal.folder
    _, proj = split_layers(proposal.payload or {})
    if not (proj["settings"] or proj["characters"]):
        return False
    if folder is None:
        return True

    for entry in proj["settings"]:
        title = str(entry.get("title") or "").strip()
        if title and _nearest_in_chain(Setting, folder, title=title) is None:
            return True

    for entry in proj["characters"]:
        name = str(entry.get("name") or "").strip()
        if name and _nearest_in_chain(Character, folder, name=name) is None:
            return True

    return False


def _dropped(old: list[dict], new: list[dict]) -> list[str]:
    """The parts of the lines an update left out."""
    kept = {ln.get("id") for ln in new}
    return [ln["part"] for ln in old if ln.get("id") and ln["id"] not in kept]


def _lines(existing, proposed, prefix: str) -> list[dict]:
    """The entry's lines after this edit: the proposed lines, with the ids of
    the lines they restate kept. A non-empty proposal REPLACES the lines --
    a line the model did not restate is gone (the lead's rule is to write
    the whole list); an empty proposal leaves the lines alone."""
    old = library.lines_of(existing.tags if existing else [])
    new = library.lines_of(proposed)
    if not new:
        return old
    return library.stamp_by(old, library.assign_ids(old, new, prefix), "ai")


def _write_setting(entry: dict, *, folder: Folder | None, conversation: Conversation | None,
                   existing: Setting | None, done: list[str],
                   base: Setting | None = None) -> Setting | None:
    """
    `base` is the project entry a conversation-layer copy is being made of:
    the edit says what changes, the project entry says the rest. Without it a
    model that sent only the changed line would leave the copy with one line
    and the project entry untouched -- the worst of both.
    """
    title = str(entry.get("title") or "").strip()
    content = str(entry.get("content") or "") or (base.content if base else "")
    if existing:
        if content:
            library.set_text(existing, content)
        before = library.lines_of(existing.tags)
        existing.tags = _lines(existing, entry.get("tags"), "s")
        gone = _dropped(before, existing.tags)
        if gone:
            done.append(ui_text.PROPOSAL_LINES_DROPPED.format(name=title, n=len(gone), parts="、".join(gone)))
        # the lead wrote this entry, lines or no lines: a model has looked
        library.lines_by_model(existing)
        if conversation is None and existing.conversation_id:
            # promoted: the conversation's entry becomes the project's
            existing.conversation = None
            existing.folder = folder
        existing.save()
        done.append(ui_text.PROPOSAL_UPDATED_SETTING.format(title=title))
        return None
    if conversation is not None:
        owner = conversation.own_settings
    else:
        assert folder is not None  # apply_layer refuses a project write with no folder
        owner = folder.settings
    pos = owner.aggregate(m=Max("position"))["m"] or 0
    lines = _lines(base, entry.get("tags"), "s")
    now = timezone.now()
    born = Setting.objects.create(
        folder=folder if conversation is None else None, conversation=conversation, title=title, content=content,
        tags=lines, position=pos + 1, text_edited_at=now, lines_ai_at=now + library.AI_AFTER_TEXT,
    )
    done.append((ui_text.PROPOSAL_CREATED_SETTING_OWN if conversation is not None
                 else ui_text.PROPOSAL_CREATED_SETTING).format(title=title))
    return born


def _own_lines(entry: dict) -> list[dict]:
    """
    The character's own lines in an edit: what it says minus what its costumes
    say. A line cannot be on both; one that also appears in a costume of the
    same edit is clothing and lives there. The model, told to add a scarf to
    an outfit, has sent the outfit's lines as the character's -- and taken
    through as written, that replaced her hair and eyes with a sweater. When
    nothing is left, the edit does not touch the character's lines at all.
    """
    worn = {(ln["part"], ln["tags"]) for v in entry.get("variants") or []
            for ln in library.lines_of(v.get("tags"))}
    return [ln for ln in library.lines_of(entry.get("tags"))
            if (ln["part"], ln["tags"]) not in worn or wording.is_part(ln["part"], "identity")]


def _write_character(entry: dict, *, folder: Folder | None, conversation: Conversation | None,
                     existing: Character | None, done: list[str],
                     base: Character | None = None) -> list[CharacterVariant]:
    """`base`: see _write_setting. Its costumes are copied whole before the
    edit's own are applied, so an edit that adds a scarf to one costume does
    not leave the copy without the others."""
    name = str(entry.get("name") or "").strip()
    description = entry.get("description")
    born: list[CharacterVariant] = []
    if existing is None:
        if conversation is not None:
            owner = conversation.own_characters
        else:
            assert folder is not None  # apply_layer refuses a project write with no folder
            owner = folder.characters
        pos = (owner.aggregate(m=Max("position"))["m"] or 0) + 1
        text = str(description or "") or (base.description if base else "")
        lines = _lines(base, _own_lines(entry), "k")
        now = timezone.now()
        dated = {"text_edited_at": now, "lines_ai_at": now + library.AI_AFTER_TEXT}
        existing = Character.objects.create(
            folder=folder if conversation is None else None, conversation=conversation, name=name, description=text,
            tags=lines, position=pos, **dated,
        )
        if base is not None:
            # The copy is what this conversation draws from now, so its
            # costumes are switched on like new ones -- but only the ones the
            # project character had on here: a character with costume A on
            # must not come back with A and B on. Each costume keeps its own
            # stamps, so one whose lines were behind stays behind.
            was_on = set(conversation.selected_variants.filter(character=base).values_list("id", flat=True)) \
                if conversation is not None else set()
            copies = []
            for v in base.variants.all().order_by("position"):
                copy = CharacterVariant.objects.create(
                    character=existing, title=v.title, content=v.content,
                    tags=list(v.tags or []), position=v.position,
                    text_edited_at=v.text_edited_at, lines_ai_at=v.lines_ai_at, lines_tried_at=v.lines_tried_at,
                )
                copies.append((v.id, copy))
            chosen = [copy for vid, copy in copies if vid in was_on]
            born.extend(chosen or [copy for _vid, copy in copies])
        done.append((ui_text.PROPOSAL_CREATED_CHARACTER_OWN if conversation is not None
                     else ui_text.PROPOSAL_CREATED_CHARACTER).format(name=name))
    else:
        changed = []
        # an omitted/blank description means "leave the description alone"
        if description and library.set_text(existing, str(description)):
            changed += ["description", "text_edited_at"]
        own = _own_lines(entry)
        if own:
            before = library.lines_of(existing.tags)
            existing.tags = _lines(existing, own, "k")
            gone = _dropped(before, existing.tags)
            if gone:
                done.append(ui_text.PROPOSAL_LINES_DROPPED.format(name=name, n=len(gone), parts="、".join(gone)))
            changed.append("tags")
        # the lead wrote this entry, lines or no lines: a model has looked
        library.lines_by_model(existing)
        changed.append("lines_ai_at")
        if conversation is None and existing.conversation_id:
            existing.conversation = None
            existing.folder = folder
            changed += ["conversation", "folder"]
        if changed:
            existing.save()
            done.append(ui_text.PROPOSAL_UPDATED_CHARACTER.format(name=name))

    # The identity line lives on the character, whatever the model wrote: a
    # costume is what she wears, not who she is.
    for v in entry.get("variants") or []:
        v_title = str(v.get("title") or "").strip()
        if not v_title:
            continue
        v_content = str(v.get("content") or "")
        v_lines = [ln for ln in library.lines_of(v.get("tags"))
                   if not wording.is_part(ln["part"], "identity")]
        idents = [ln for ln in library.lines_of(v.get("tags"))
                  if wording.is_part(ln["part"], "identity")]
        old = library.lines_of(existing.tags)
        have = {(ln["part"], ln["tags"]) for ln in old}
        fresh = [ln for ln in idents if (ln["part"], ln["tags"]) not in have]
        if fresh:
            # every identity line the costume brought and the character
            # lacks moves up -- not just the first, and not only when the
            # character has none: a second line is a second thing she is
            existing.tags = library.stamp_by(old, library.assign_ids(old, fresh + old, "k"), "ai")
            existing.save(update_fields=["tags", "updated_at"])
        variant = CharacterVariant.objects.filter(character=existing, title=v_title).first()
        if variant:
            if v_content:
                library.set_text(variant, v_content)
            if v_lines:
                old = library.lines_of(variant.tags)
                variant.tags = library.stamp_by(old, library.assign_ids(
                    old, v_lines, "v",
                    taken=library.variant_ids_taken(existing, exclude=variant.id)), "ai")
            library.lines_by_model(variant)
            variant.save()
            done.append(ui_text.PROPOSAL_UPDATED_VARIANT.format(name=name, title=v_title))
        else:
            vpos = (existing.variants.aggregate(m=Max("position"))["m"] or 0) + 1
            v_tags = library.assign_ids([], v_lines, "v", taken=library.variant_ids_taken(existing))
            now = timezone.now()
            born.append(CharacterVariant.objects.create(
                character=existing, title=v_title, content=v_content,
                tags=v_tags, position=vpos,
                text_edited_at=now, lines_ai_at=now + library.AI_AFTER_TEXT,
            ))
            done.append((ui_text.PROPOSAL_CREATED_VARIANT_OWN if existing.conversation_id
                         else ui_text.PROPOSAL_CREATED_VARIANT).format(name=name, title=v_title))
    # A character is drawn through a costume: the selection is made of
    # costumes, so one with none -- a cat, a monster, a character the lead
    # recorded without clothes -- was on paper and in nobody's picture. It
    # gets the default costume, switched on like any new one.
    if not existing.variants.exists():
        characters_service.repair(existing)
        born.extend(existing.variants.all())
    return born


def apply_layer(proposal: LibraryProposal, payload: dict, *, to_project: bool,
                select_new: bool) -> str:
    """
    Write one layer's entries. Returns a human summary.

    `select_new` switches this conversation on to whatever gets CREATED here —
    an entry made and then left unselected is one the model cannot see on the
    next turn, which is a strange thing to do with something it just asked to
    write down. Conversation-layer entries are always switched on: they exist
    for this conversation and nothing else.
    """
    conv = proposal.conversation
    folder = proposal.folder
    if to_project and folder is None:
        return ""
    conversation = None if to_project else conv
    if to_project:
        assert folder is not None
        taken = _taken_in_project(payload, conv, folder)
        if taken is not None:
            raise NameTaken(taken)
    done: list[str] = []
    born_settings: list[Setting] = []
    born_variants: list[CharacterVariant] = []

    for entry in payload.get("settings") or []:
        title = str(entry.get("title") or "").strip()
        if not title:
            continue
        existing = _in_conversation(Setting, conv, title=title)
        if existing is None and folder is not None:
            existing = _nearest_in_chain(Setting, folder, title=title)
        base = None
        if existing is not None and existing.conversation_id and to_project:
            pass        # promoted in place (_taken_in_project ruled out a clash)
        elif existing is not None and existing.conversation_id is None and not to_project:
            # shadow the project entry with a conversation one, seeded from
            # it: the project entry only changes when the user asks for that
            base, existing = existing, None
        made = _write_setting(entry, folder=folder, conversation=conversation,
                              existing=existing, base=base, done=done)
        if made:
            born_settings.append(made)

    for entry in payload.get("characters") or []:
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        existing = _in_conversation(Character, conv, name=name)
        if existing is None and folder is not None:
            existing = _nearest_in_chain(Character, folder, name=name)
        base = None
        if existing is not None and existing.conversation_id is None and not to_project:
            base, existing = existing, None
        born_variants += _write_character(entry, folder=folder, conversation=conversation,
                                          existing=existing, base=base, done=done)

    if select_new or not to_project:
        if born_settings:
            conv.selected_settings.add(*born_settings)
        # A character is in play through its costumes — there is nothing else
        # to switch on — so a new character with no variants selects nothing,
        # which is the same as what it is: nothing to draw with yet.
        if born_variants:
            conv.selected_variants.add(*born_variants)

    return ui_text.PROPOSAL_SUMMARY_SEP.join(done)


def apply(proposal: LibraryProposal, *, select_new: bool = False) -> str:
    """Write the project-layer part of a proposal. Returns a human summary."""
    _, proj = split_layers(proposal.payload or {})
    return apply_layer(proposal, proj, to_project=True, select_new=select_new)


def record(conversation: Conversation, message, payload: dict) -> LibraryProposal:
    """
    Write a proposal down, and the conversation layer with it.

    The row is created first, holding both layers, so nothing is lost if the
    write that follows fails. Then the conversation-layer entries are written
    straight away; what remains pending is the project-layer part, if any.
    """
    parked = LibraryProposal.objects.create(
        conversation=conversation, message=message, folder=conversation.folder,
        payload=payload,
    )
    conv, proj = split_layers(payload)
    summary = ""
    if conv["settings"] or conv["characters"]:
        summary = apply_layer(parked, conv, to_project=False, select_new=True)
    if not (proj["settings"] or proj["characters"]):
        parked.status = "applied"
        parked.applied_summary = summary
        parked.resolved_at = timezone.now()
        parked.save(update_fields=["status", "applied_summary", "resolved_at"])
    elif summary:
        parked.applied_summary = summary
        parked.save(update_fields=["applied_summary"])
    return parked


def resolve(
    proposal_id, *, approve: bool, select_new: bool = False
) -> LibraryProposal | None:
    """
    Settle one proposal. Returns it as it now stands, or None if there is none.

    The state machine is here rather than in the caller so that "already
    resolved" means the same thing to a second click and to the automatic path
    arriving at the same moment.

    `select_new` is passed through to `apply`. Both callers that approve --
    the button and the automatic path -- pass True: an entry the lead wrote
    down for this conversation and the user then approved is one the next
    picture has to see, and an approval that left it switched off was read by
    the drawing model as "no such entry".
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

    Says whether it did. Returning False is not a failure — it is the ordinary
    outcome, and it leaves the proposal exactly as it was: pending, waiting for
    a person, on the path that existed before this function did.
    """
    if mode == OFF or proposal.folder is None:
        return False
    if mode == EDITS_ONLY and creates_an_entry(proposal):
        return False
    if mode not in MODES:
        return False

    # Only ever reports on the settling IT did. `resolve` hands back a row
    # that was already settled just as readily as one it settled now, so
    # without this an approval the user made a second earlier would come back
    # as "applied automatically" — a true-looking answer about an event that
    # did not happen.
    if not LibraryProposal.objects.filter(id=proposal.id, status="pending").exists():
        return False

    # Whatever the app writes unattended, it switches on: an entry or a costume
    # created and left unselected is one the model cannot see next turn, which
    # is a strange thing to do with something it just asked to write down. True
    # for both modes that get this far — 「仅修改」 creates costumes.
    try:
        done = resolve(proposal.id, approve=True, select_new=True)
    except NameTaken as exc:
        # left pending: the card asks the person, who can tidy the project first
        log.info("library proposal %s not applied automatically: project already has %r", proposal.id, exc.name)
        return False
    applied = bool(done and done.status == "applied")
    if applied:
        log.info("library proposal %s applied automatically (%s)", proposal.id, mode)
    return applied
