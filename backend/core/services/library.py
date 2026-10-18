"""
The library as the models read it: two layers of entries.

An entry is a key (its name), a note (the person's words, never sent to
NovelAI), a positive tag list, a negative tag list and a prose line (English,
for NovelAI). Settings are entries; a character is an entry with variant
entries (outfits) under it. An entry lives in a folder (the project's: every
conversation under it reads it) or on a conversation (that conversation's
alone).

The lead reads every selected entry in full and writes entries itself; what
it exposes to the drawing model for one picture is a list of keys, and the
drawing model reads those entries in full too. Nothing is spliced into a
prompt without a model deciding it belongs there.

Keys. A conversation's own entries are named plainly; project entries are
shown to the lead with the PROJECT_PREFIX in front, so the two layers never
collide and the lead always knows which it is naming. A variant's key is
`character/variant`.
"""
from __future__ import annotations

import copy
import logging
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from django.db.models import Max
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core import ui_text
from core.models import Character, CharacterVariant, Conversation, Folder, Message, Setting
from core.prompts import wording
from core.services import characters as characters_service, novelai

log = logging.getLogger(__name__)

PROJECT_PREFIX = "@"
VARIANT_SEP = "/"
#: an exposure list item that takes a key away from an inherited list
REMOVE_PREFIX = "-"

#: a model's fill is stamped this much after the text it was written from,
#: so "the text changed after the fill" never ties on the clock
AI_AFTER_TEXT = timedelta(seconds=1)


# --------------------------------------------------------------- tag lists
def split_tags(raw) -> list[str]:
    """A tag list out of a comma-separated string or a list: stripped, empty
    pieces dropped, the same tag (by novelai.tag_key) kept once, in order."""
    if isinstance(raw, str):
        pieces = raw.split(",")
    elif isinstance(raw, (list, tuple)):
        pieces = [str(p) for p in raw]
    else:
        pieces = []
    out: list[str] = []
    seen: set[str] = set()
    for p in pieces:
        p = p.strip().rstrip(".").strip()
        if not p:
            continue
        key = novelai.tag_key(p)
        if not key:
            # a weight on its own (`1.5::`, `::`): never a repeat
            out.append(p)
            continue
        if key in seen:
            # the repeat goes, the weight it opens or closes stays
            rest = novelai.weight_markers(p)
            if rest:
                out.append(rest)
            continue
        seen.add(key)
        out.append(p)
    return out


def set_text(obj, text: str, *, now=None) -> bool:
    """Store an entry's note. Says whether it changed, and dates the change."""
    attr = "description" if isinstance(obj, Character) else "content"
    if text == getattr(obj, attr):
        return False
    setattr(obj, attr, text)
    now = now or timezone.now()
    # a model's fill is dated a hair after the text it came from; an edit
    # landing inside that hair is still an edit after the look
    if obj.lines_ai_at is not None and obj.lines_ai_at >= now:
        now = obj.lines_ai_at + timedelta(microseconds=1)
    obj.text_edited_at = now
    return True


def lines_by_model(obj, *, now=None) -> None:
    """A model filled the entry, or looked and let it stand: dated after the text."""
    now = now or timezone.now()
    base = max(now, obj.text_edited_at) if obj.text_edited_at else now
    obj.lines_ai_at = base + AI_AFTER_TEXT


# --------------------------------------------------------------- entry dicts
def layer_of(obj) -> str:
    return "conversation" if getattr(obj, "conversation_id", None) else "project"


def shown_key(key: str, layer: str) -> str:
    """The key as the lead sees it: project entries carry the prefix."""
    return PROJECT_PREFIX + key if layer == "project" else key


def entry_dict(obj, *, character: Character | None = None) -> dict:
    """
    One entry as every reader gets it. Variants carry their character's key
    and a combined key `character/variant`; the character is the one the
    variant belongs to (passed in when the caller has it, to spare a query).
    """
    if isinstance(obj, Setting):
        kind, key, layer = "setting", obj.title, layer_of(obj)
        owner = ""
    elif isinstance(obj, Character):
        kind, key, layer = "character", obj.name, layer_of(obj)
        owner = ""
    else:
        character = character or obj.character
        kind, layer = "variant", layer_of(character)
        owner = character.name
        key = f"{owner}{VARIANT_SEP}{obj.title}"
    out = {
        "id": str(obj.id), "kind": kind, "key": key, "layer": layer, "shown_key": shown_key(key, layer),
        "note": obj.lines_text, "positive": split_tags(obj.positive), "negative": split_tags(obj.negative),
        "prose": (obj.prose or "").strip(), "origin_seq": obj.origin_seq,
    }
    if kind == "setting":
        out["category"] = getattr(obj, "category", "") or ""
    if kind == "variant":
        out["character"] = owner
    return out


# --------------------------------------------------------------- the selection
@dataclass
class Selection:
    """
    What a conversation has switched on: the settings and the character
    variants, each layer's. The lead reads these in full and nothing else:
    what the person has not switched on, the lead does not see.
    """

    settings: list = field(default_factory=list)
    variants: list = field(default_factory=list)
    project_name: str = ""
    in_project: bool = False
    #: the drawing id of each turn that wrote one of these entries, by the
    #: turn's message seq: where an entry came from, in the id the lead knows
    draw_ids: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.by_character: dict = {}
        for v in self.variants:
            self.by_character.setdefault(v.character, []).append(v)

    def __bool__(self) -> bool:
        return bool(self.settings or self.by_character)


def selection_for(conversation: Conversation) -> Selection:
    settings = list(conversation.selected_settings.all())
    variants = list(conversation.selected_variants.select_related("character").all())
    folder = conversation.folder
    seqs = {x.origin_seq for x in [*settings, *(v.character for v in variants)] if x.origin_seq}
    draw_ids: dict[int, str] = {}
    for m in Message.objects.filter(conversation=conversation, seq__in=seqs).only("seq", "nai_prompts"):
        did = (m.nai_prompts or {}).get("draw_id")
        if did:
            draw_ids[m.seq] = str(did)
    return Selection(
        settings=settings, variants=variants,
        project_name=folder.name if folder else "",
        in_project=folder is not None,
        draw_ids=draw_ids,
    )


def visible_entries(conversation: Conversation) -> tuple[list[Setting], list[Character]]:
    """
    Everything a conversation may switch on: the folder chain's entries, then
    its own. Nearest last, so the most specific entries read at the bottom.
    """
    settings: list[Setting] = []
    characters: list[Character] = []
    folder: Folder | None = conversation.folder
    if folder is not None:
        ids = [f.id for f in folder.ancestors()]
        order: dict[uuid.UUID | None, int] = {fid: i for i, fid in enumerate(ids)}
        settings = list(Setting.objects.filter(folder_id__in=ids, conversation__isnull=True))
        characters = list(
            Character.objects.filter(folder_id__in=ids, conversation__isnull=True)
            .prefetch_related("variants")
        )
        settings.sort(key=lambda s: (order.get(s.folder_id, 0), s.position, s.created_at))
        characters.sort(key=lambda c: (order.get(c.folder_id, 0), c.position, c.created_at))
    settings += list(conversation.own_settings.all())
    characters += list(conversation.own_characters.prefetch_related("variants").all())
    return settings, characters


def rehome(conversation: Conversation) -> None:
    """
    A conversation that has just changed folder: selections of entries no
    longer in reach are dropped (they would be drawn from while appearing in
    no list). Nothing comes on by itself. The conversation's own entries come
    along wherever it goes.
    """
    settings, characters = visible_entries(conversation)
    conversation.selected_settings.set(
        conversation.selected_settings.filter(id__in=[s.id for s in settings])
    )
    allowed = [v.id for c in characters for v in c.variants.all()]
    conversation.selected_variants.set(conversation.selected_variants.filter(id__in=allowed))


# --------------------------------------------------------------- the lead's listing
def _field_lines(e: dict, indent: str) -> list[str]:
    out = []
    if e.get("note"):
        out.append(f"{indent}{wording.ENTRY_NOTE_LABEL}：{' '.join(e['note'].split())}")
    if e.get("positive"):
        out.append(f"{indent}{wording.ENTRY_POSITIVE_LABEL}：{', '.join(e['positive'])}")
    if e.get("negative"):
        out.append(f"{indent}{wording.ENTRY_NEGATIVE_LABEL}：{', '.join(e['negative'])}")
    if e.get("prose"):
        out.append(f"{indent}{wording.ENTRY_PROSE_LABEL}：{e['prose']}")
    if not out:
        out.append(f"{indent}{wording.ENTRY_EMPTY}")
    return out


def _origin_mark(e: dict, draw_ids: dict) -> str:
    """Where an entry came from: the project, the person, or the lead -- at
    the turn whose drawing id it names, when that turn drew."""
    if e["layer"] == "project":
        return wording.ENTRY_FROM_PROJECT
    if e.get("origin_seq"):
        did = draw_ids.get(e["origin_seq"])
        return wording.ENTRY_FROM_LEAD.format(id=did) if did else wording.ENTRY_FROM_LEAD_NO_DRAW
    return wording.ENTRY_FROM_USER


def as_prompt_text(selection: Selection) -> str:
    """
    The lead's rendering of the library: every selected entry in full, with
    its key as the lead must write it, what it is and where it came from.
    Entries the person has not switched on are not in it at all.
    """
    head = (
        wording.LIBRARY_IN_PROJECT.format(project=selection.project_name)
        if selection.in_project else wording.LIBRARY_NO_PROJECT
    )
    if not selection:
        return head + "\n" + wording.LIBRARY_EMPTY
    blocks: list[str] = [head]
    for s in selection.settings:
        e = entry_dict(s)
        kind = wording.ENTRY_KIND_SETTING + (f"·{e['category']}" if e.get("category") else "")
        blocks.append("\n".join([f"◆ {e['shown_key']}（{kind}，{_origin_mark(e, selection.draw_ids)}）",
                                  *_field_lines(e, "  ")]))
    for character, variants in selection.by_character.items():
        e = entry_dict(character)
        lines = [f"◆ {e['shown_key']}（{wording.ENTRY_KIND_CHARACTER}，{_origin_mark(e, selection.draw_ids)}）",
                 *_field_lines(e, "  ")]
        for v in variants:
            ve = entry_dict(v, character=character)
            lines.append(f"  ◇ {ve['shown_key']}（{wording.ENTRY_KIND_VARIANT}）")
            lines += _field_lines(ve, "    ")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


# --------------------------------------------------------------- keys
def parse_key(raw: str) -> tuple[bool, str, str]:
    """(is_project, character or setting key, variant title or '')."""
    key = (raw or "").strip()
    is_project = key.startswith(PROJECT_PREFIX)
    if is_project:
        key = key[len(PROJECT_PREFIX):].strip()
    if VARIANT_SEP in key:
        owner, title = key.split(VARIANT_SEP, 1)
        return is_project, owner.strip(), title.strip()
    return is_project, key, ""


def _own(model, conversation: Conversation, **lookup):
    return model.objects.filter(conversation=conversation, **lookup).first()


def _in_chain(model, folder: Folder | None, **lookup):
    if folder is None:
        return None
    for f in reversed(folder.ancestors()):
        found = model.objects.filter(folder=f, conversation__isnull=True, **lookup).first()
        if found:
            return found
    return None


def find_setting(conversation: Conversation, raw_key: str) -> Setting | None:
    """The setting a key names: the conversation's own unless the key says
    project; a plain key that only the project has is the project's, and a
    prefixed key that only the conversation has is the conversation's -- the
    prefix is read as a preference, never as a reason to find nothing."""
    is_project, key, _ = parse_key(raw_key)
    if not key:
        return None
    if not is_project:
        own = _own(Setting, conversation, title=key)
        if own:
            return own
    found = _in_chain(Setting, conversation.folder, title=key)
    if found is None and is_project:
        found = _own(Setting, conversation, title=key)
    return found


def find_character(conversation: Conversation, raw_key: str) -> Character | None:
    """The character a key names, by the same rule as find_setting."""
    is_project, key, _ = parse_key(raw_key)
    if not key:
        return None
    if not is_project:
        own = _own(Character, conversation, name=key)
        if own:
            return own
    found = _in_chain(Character, conversation.folder, name=key)
    if found is None and is_project:
        found = _own(Character, conversation, name=key)
    return found


def _switched_on_in_chain(qs, folder: Folder | None):
    """The nearest project entry of `qs` up the folder chain, or None."""
    if folder is None:
        return None
    for f in reversed(folder.ancestors()):
        found = qs.filter(folder=f, conversation__isnull=True).first()
        if found:
            return found
    return None


def find_switched_on_setting(conversation: Conversation, raw_key: str) -> Setting | None:
    """The setting a key names among the ones switched on for this
    conversation -- the only ones the lead sees. The prefix is a preference,
    as in find_setting."""
    is_project, key, _ = parse_key(raw_key)
    if not key:
        return None
    on = conversation.selected_settings.filter(title=key)
    own = on.filter(conversation=conversation).first()
    project = _switched_on_in_chain(on, conversation.folder)
    return (project or own) if is_project else (own or project)


def find_switched_on_character(conversation: Conversation, raw_key: str) -> Character | None:
    """The character a key names among the ones with a variant switched on."""
    is_project, key, _ = parse_key(raw_key)
    if not key:
        return None
    on = Character.objects.filter(name=key, variants__in=conversation.selected_variants.all()).distinct()
    own = on.filter(conversation=conversation).first()
    project = _switched_on_in_chain(on, conversation.folder)
    return (project or own) if is_project else (own or project)


def _switched_on_variant(conversation: Conversation, character: Character, title: str) -> CharacterVariant | None:
    return conversation.selected_variants.filter(character=character, title=title).first()


def _bare(key: str) -> str:
    """A key without the project prefix."""
    key = key.strip()
    return key[len(PROJECT_PREFIX):].strip() if key.startswith(PROJECT_PREFIX) else key


# --------------------------------------------------------------- the lead's writes
def _write_fields(obj, item: dict, *, now) -> None:
    """The lead wrote the whole entry: note, tags and prose, as given."""
    note = str(item.get("note") or "")
    if note:
        set_text(obj, note, now=now)
    obj.positive = split_tags(item.get("positive"))
    obj.negative = split_tags(item.get("negative"))
    obj.prose = str(item.get("prose") or "").strip()
    lines_by_model(obj, now=now)


def _copy_fields(obj, source) -> None:
    """A character's text and lines copied from another, as they stand."""
    if source is None:
        return
    for f in ("description", "positive", "negative", "prose", "text_edited_at", "lines_ai_at", "lines_tried_at"):
        setattr(obj, f, copy.deepcopy(getattr(source, f)))


def write_conversation_entries(conversation: Conversation, seq: int, settings: list[dict],
                               characters: list[dict]) -> list[str]:
    """
    Write the entries the lead made this turn into the conversation's layer,
    overriding any of the same key, and switch them on. Returns what was
    done, one line each, for the log.
    """
    done: list[str] = []
    now = timezone.now()
    for item in settings:
        _, key, _ = parse_key(str(item.get("key") or ""))
        if not key:
            continue
        existing = _own(Setting, conversation, title=key)
        if existing is None:
            pos = (conversation.own_settings.aggregate(m=Max("position"))["m"] or 0) + 1
            existing = Setting(folder=None, conversation=conversation, title=key, position=pos)
            done.append(f"+ {key}")
        else:
            done.append(f"~ {key}")
        existing.category = str(item.get("category") or "").strip()[:40]
        _write_fields(existing, item, now=now)
        existing.origin_seq = seq
        existing.save()
        conversation.selected_settings.add(existing)
    for item in characters:
        _, name, _ = parse_key(str(item.get("key") or ""))
        if not name:
            continue
        existing = _own(Character, conversation, name=name)
        if item.get("carrier"):
            # only costumes were written: the character stays as it is, and
            # one this conversation does not have yet starts as a copy of the
            # project's, the way the lead overrides a project entry
            if existing is None:
                pos = (conversation.own_characters.aggregate(m=Max("position"))["m"] or 0) + 1
                existing = Character(folder=None, conversation=conversation, name=name, position=pos)
                _copy_fields(existing, find_switched_on_character(conversation, name))
                existing.origin_seq = seq
                existing.save()
                done.append(f"+ {name}")
        else:
            if existing is None:
                pos = (conversation.own_characters.aggregate(m=Max("position"))["m"] or 0) + 1
                existing = Character(folder=None, conversation=conversation, name=name, position=pos)
                done.append(f"+ {name}")
            else:
                done.append(f"~ {name}")
            _write_fields(existing, item, now=now)
            existing.origin_seq = seq
            existing.save()
        made: list[CharacterVariant] = []
        for v in item.get("variants") or []:
            _, _, title = parse_key(str(v.get("key") or ""))
            title = title or str(v.get("key") or "").strip()
            if not title:
                continue
            variant = CharacterVariant.objects.filter(character=existing, title=title).first()
            if variant is None:
                vpos = (existing.variants.aggregate(m=Max("position"))["m"] or 0) + 1
                variant = CharacterVariant(character=existing, title=title, position=vpos)
                done.append("+ " + ui_text.ENTRY_VARIANT.format(name=name, title=title))
            else:
                done.append("~ " + ui_text.ENTRY_VARIANT.format(name=name, title=title))
            _write_fields(variant, v, now=now)
            variant.origin_seq = seq
            variant.save()
            made.append(variant)
        if made:
            # The stand-in outfit the app gives a character written without
            # one (characters.repair) has done its job once a real one
            # exists; left behind, it is an empty outfit in every list.
            for stub in existing.variants.filter(title__in=ui_text.in_every_language("DEFAULT_VARIANT_TITLE")).exclude(
                    id__in=[v.id for v in made]):
                if not (stub.content.strip() or stub.positive or stub.negative or stub.prose.strip()):
                    stub.delete()
        if not existing.variants.exists():
            characters_service.repair(existing)
        conversation.selected_variants.add(*(made or list(existing.variants.all())))
    return done


# --------------------------------------------------------------- a turn's writes
def _merged(earlier: dict | None, item: dict) -> dict:
    """A later write of an entry within one turn over an earlier one: the
    whole entry is the later one's, except a blank note, which keeps the
    earlier note -- as writing the library does."""
    out = dict(item)
    if earlier and not str(item.get("note") or "").strip() and earlier.get("note"):
        out["note"] = earlier["note"]
    return out


def _merged_character(earlier: dict | None, item: dict) -> dict:
    """The same for a character; its costumes merge one by one, by title. A
    carrier (costumes only) leaves the character as the turn had it."""
    if item.get("carrier"):
        out = dict(earlier) if earlier else {"key": item.get("key"), "carrier": True}
    else:
        out = _merged(earlier, item)
    costumes = dict((earlier or {}).get("variants") or {})
    for v in item.get("variants") or []:
        raw = str(v.get("key") or "").strip()
        title = parse_key(raw)[2] or raw
        if title:
            costumes[title] = _merged(costumes.get(title), v)
    out["variants"] = costumes
    return out


@dataclass
class Staged:
    """
    What a turn has written and not yet committed: the conversation's own
    entries and the edits to the project, by key, a later write over an
    earlier one. The turn draws from the library with this laid over it; the
    library itself is written once, when the turn's pictures are settled.
    Reflection rounds rewrite entries as they go, and a round whose picture
    is not kept takes its writes with it (`copy` before, `restore` after).
    """

    seq: int
    in_project: bool
    settings: dict[str, dict] = field(default_factory=dict)
    characters: dict[str, dict] = field(default_factory=dict)
    edit_settings: dict[str, dict] = field(default_factory=dict)
    edit_characters: dict[str, dict] = field(default_factory=dict)

    def copy(self) -> "Staged":
        return copy.deepcopy(self)

    def restore(self, other: "Staged") -> None:
        self.settings, self.characters = other.settings, other.characters
        self.edit_settings, self.edit_characters = other.edit_settings, other.edit_characters

    def add(self, answer: dict, conversation: Conversation | None = None) -> None:
        """Lay one answer's writes (the lead's, or a reflection round's) over
        what the turn has written so far. Outside a project an edit has
        nowhere to go but this conversation. Given the conversation, an entry
        written in the other kind's list goes where its key already is."""
        settings = list(answer.get("settings") or [])
        characters = list(answer.get("characters") or [])
        edits = answer.get("edits") or {}
        if edits and not self.in_project:
            settings += list(edits.get("settings") or [])
            characters += list(edits.get("characters") or [])
            edits = {}
        if conversation is not None:
            settings, characters = self._by_kind(conversation, settings, characters, project=False)
            if edits:
                es, ec = self._by_kind(conversation, list(edits.get("settings") or []),
                                       list(edits.get("characters") or []), project=True)
                edits = {"settings": es, "characters": ec}
        for item in settings:
            key = parse_key(str(item.get("key") or ""))[1]
            if key:
                self.settings[key] = _merged(self.settings.get(key), item)
        for item in characters:
            name = parse_key(str(item.get("key") or ""))[1]
            if name:
                self.characters[name] = _merged_character(self.characters.get(name), item)
        for item in edits.get("settings") or []:
            key = parse_key(str(item.get("key") or ""))[1]
            if key:
                self.edit_settings[key] = _merged(self.edit_settings.get(key), item)
        for item in edits.get("characters") or []:
            name = parse_key(str(item.get("key") or ""))[1]
            if name:
                self.edit_characters[name] = _merged_character(self.edit_characters.get(name), item)

    def _by_kind(self, conversation: Conversation, settings: list[dict], characters: list[dict], *,
                 project: bool) -> tuple[list[dict], list[dict]]:
        """
        A key names one entry in its layer whatever list it is written in:
        a character-list entry whose key is already a setting there, and no
        character, is that setting (keeping its category), and the other way
        round. A character written with costumes is a character.
        """
        folder = conversation.folder

        def setting_at(key: str):
            if project:
                staged = self.edit_settings.get(key)
                return staged if staged is not None else _in_chain(Setting, folder, title=key)
            staged = self.settings.get(key)
            if staged is not None:
                return staged
            return find_switched_on_setting(conversation, key) or _own(Setting, conversation, title=key)

        def character_at(key: str):
            if project:
                return self.edit_characters.get(key) or _in_chain(Character, folder, name=key)
            return (self.characters.get(key) or find_switched_on_character(conversation, key)
                    or _own(Character, conversation, name=key))

        keep_s: list[dict] = []
        keep_c: list[dict] = []
        for item in characters:
            key = parse_key(str(item.get("key") or ""))[1]
            setting = setting_at(key) if key and not item.get("variants") and not item.get("carrier") else None
            if setting is not None and character_at(key) is None:
                category = setting.get("category") if isinstance(setting, dict) else setting.category
                moved = {k: v for k, v in item.items() if k not in ("variants", "carrier")}
                keep_s.append({**moved, "category": str(item.get("category") or category or "")})
                log.info("entry %r was written among the characters but is a setting; filed as one", key)
            else:
                keep_c.append(item)
        for item in settings:
            key = parse_key(str(item.get("key") or ""))[1]
            if key and setting_at(key) is None and character_at(key) is not None:
                keep_c.append({k: v for k, v in item.items() if k != "category"})
                log.info("entry %r was written among the settings but is a character; filed as one", key)
            else:
                keep_s.append(item)
        return keep_s, keep_c

    def conversation_lists(self) -> tuple[list[dict], list[dict]]:
        """The conversation's entries in the shape write_conversation_entries takes."""
        characters = [{**c, "variants": list((c.get("variants") or {}).values())} for c in self.characters.values()]
        return list(self.settings.values()), characters

    def edits(self) -> dict | None:
        """The project edits in the shape a proposal takes, or None."""
        if not self.edit_settings and not self.edit_characters:
            return None
        return {
            "settings": [{**item, "key": PROJECT_PREFIX + key} for key, item in self.edit_settings.items()],
            "characters": [{**item, "key": PROJECT_PREFIX + name, "variants": list((item.get("variants") or {}).values())}
                           for name, item in self.edit_characters.items()],
        }

    def covers_edit(self, key: str, title: str) -> bool:
        """Whether this turn's project edits write the entry a key names."""
        if not title:
            return key in self.edit_settings or key in self.edit_characters
        return title in ((self.edit_characters.get(key) or {}).get("variants") or {})

    def _entry(self, existing, kind: str, key: str, item: dict, character: str = "") -> dict:
        """An entry as the library will hold it once this turn's write lands."""
        base = entry_dict(existing) if existing is not None else {}
        out = {
            "id": base.get("id", ""), "kind": kind, "key": key, "layer": "conversation", "shown_key": key,
            "note": str(item.get("note") or "").strip() or base.get("note", ""),
            "positive": split_tags(item.get("positive")), "negative": split_tags(item.get("negative")),
            "prose": str(item.get("prose") or "").strip(), "origin_seq": self.seq,
        }
        if kind == "setting":
            out["category"] = str(item.get("category") or "").strip()[:40]
        if kind == "variant":
            out["character"] = character
        return out

    def entries_for(self, conversation: Conversation, raw: str) -> tuple[list[dict], bool] | None:
        """
        The entries a key names among this turn's own writes -- a costume
        brings its character -- and whether the costume it names is missing;
        None when this turn has not written that key, and the library
        answers. A key with the project's prefix is the project's when the
        project has it, as when finding.
        """
        is_project, key, title = parse_key(raw)
        if not key:
            return None
        if is_project and (_in_chain(Character, conversation.folder, name=key) is not None
                           or (not title and _in_chain(Setting, conversation.folder, title=key) is not None)):
            return None
        if not title and key in self.settings:
            return [self._entry(_own(Setting, conversation, title=key), "setting", key, self.settings[key])], False
        if key not in self.characters:
            return None
        item = self.characters[key]
        existing = _own(Character, conversation, name=key)
        if item.get("carrier"):
            # costumes only: the character as the library has it
            base = existing or find_switched_on_character(conversation, key)
            if base is None:
                return None
            character = self._entry(existing, "character", key, entry_dict(base))
        else:
            character = self._entry(existing, "character", key, item)
        if not title:
            return [character], False
        stored = existing.variants.filter(title=title).first() if existing is not None else None
        costume = (item.get("variants") or {}).get(title)
        if costume is not None:
            return [character, self._entry(stored, "variant", f"{key}{VARIANT_SEP}{title}", costume, key)], False
        if stored is not None and existing is not None:
            return [character, entry_dict(stored, character=existing)], False
        return [character], True


def unwritten_exposure(conversation: Conversation, answer: dict, staged: Staged | None = None) -> list[str]:
    """
    The keys a lead answer's draw exposes that neither the library holds,
    nor this turn has written so far (`staged`), nor the answer itself
    writes -- read before the answer is laid over the turn, so it can be
    sent back. Only what is switched on counts as held: an entry the person
    has not switched on is not the lead's to use. Removals and inherited keys
    are not the answer's to account for; a costume counts only if that
    costume is there, not merely its character.
    """
    draw = answer.get("draw") or {}
    keys = [str(k).strip() for k in draw.get("expose") or []]
    keys = [k for k in keys if k and not k.startswith(REMOVE_PREFIX)]
    if not keys:
        return []
    turn = staged.copy() if staged is not None else Staged(seq=0, in_project=conversation.folder is not None)
    turn.add(answer, conversation)
    missing: list[str] = []
    for raw in keys:
        hit = turn.entries_for(conversation, raw)
        if hit is not None:
            if hit[1]:
                missing.append(raw)
            continue
        _, key, title = parse_key(raw)
        if turn.covers_edit(key, title):
            continue
        if not title:
            if (find_switched_on_setting(conversation, raw) is not None
                    or find_switched_on_character(conversation, raw) is not None):
                continue
        else:
            character = find_switched_on_character(conversation, raw)
            if character is not None and _switched_on_variant(conversation, character, title) is not None:
                continue
        missing.append(raw)
    return missing


# --------------------------------------------------------------- exposure
@dataclass
class Exposure:
    """What one picture exposes to the drawing model: the entries in full,
    in the lead's order, the keys to store, and the keys nobody could find."""

    entries: list[dict] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)


def resolve_exposure(conversation: Conversation, keys: list[str], *, inherited: list[str] | None = None,
                     overlay: dict[str, dict] | None = None, staged: Staged | None = None) -> Exposure:
    """
    Turn the lead's exposure list into entries.

    `inherited` is the stored key list of the picture this one continues;
    the lead's list then adds to it, and an item with REMOVE_PREFIX takes a
    key out. `overlay` maps a project entry's plain key to the edited version
    of it the lead proposed this turn: the edit comes before the exposure,
    approved or not. `staged` is what this turn has written to the
    conversation's own layer and not yet committed: those entries are read
    as they will be once written. A variant implies its character, placed before it.
    Only entries switched on for this conversation are found: what the person
    has not switched on, the lead does not see and cannot draw from, and
    exposing it does not switch it on.
    """
    overlay = overlay or {}
    wanted: list[str] = []
    for raw in list(inherited or []) + [str(k) for k in keys or []]:
        raw = raw.strip()
        if not raw:
            continue
        if raw.startswith(REMOVE_PREFIX):
            target = raw[len(REMOVE_PREFIX):].strip()
            if target in wanted:
                wanted = [k for k in wanted if k != target]
            else:
                # the same tolerance as finding: 「-@画风」 takes out a 「画风」
                # that is the conversation's, and the other way round
                wanted = [k for k in wanted if _bare(k) != _bare(target)]
            continue
        if raw not in wanted:
            wanted.append(raw)
    out = Exposure()
    seen: set[str] = set()

    def add(e: dict) -> None:
        if e["shown_key"] in seen:
            return
        seen.add(e["shown_key"])
        out.entries.append(e)
        out.keys.append(e["shown_key"])

    for raw in wanted:
        hit = staged.entries_for(conversation, raw) if staged is not None else None
        if hit is not None:
            for e in hit[0]:
                add(e)
            if hit[1]:
                out.unknown.append(raw)
            continue
        _, _, title = parse_key(raw)
        setting = find_switched_on_setting(conversation, raw) if not title else None
        if setting is not None:
            add(_overlaid(entry_dict(setting), overlay))
            continue
        character = find_switched_on_character(conversation, raw)
        if character is None:
            out.unknown.append(raw)
            continue
        add(_overlaid(entry_dict(character), overlay))
        if title:
            variant = _switched_on_variant(conversation, character, title)
            if variant is None:
                out.unknown.append(raw)
                continue
            add(_overlaid(entry_dict(variant, character=character), overlay))
    return out


def _overlaid(e: dict, overlay: dict[str, dict]) -> dict:
    """The entry with this turn's project edit laid over it, if there is one."""
    if e["layer"] != "project":
        return e
    edit = overlay.get(e["key"])
    if not edit:
        return e
    out = dict(e)
    for f in ("note", "prose"):
        if edit.get(f):
            out[f] = str(edit[f]).strip()
    for f in ("positive", "negative"):
        if edit.get(f) is not None:
            out[f] = split_tags(edit[f])
    if e["kind"] == "setting" and edit.get("category"):
        out["category"] = str(edit["category"]).strip()
    out["edited_this_turn"] = True
    return out


def overlay_from_edits(edits: dict | None) -> dict[str, dict]:
    """{plain key: fields} out of the lead's project edits, variants under
    `character/variant`, for resolve_exposure."""
    out: dict[str, dict] = {}
    for item in (edits or {}).get("settings") or []:
        _, key, _ = parse_key(str(item.get("key") or ""))
        if key:
            out[key] = item
    for item in (edits or {}).get("characters") or []:
        _, name, _ = parse_key(str(item.get("key") or ""))
        if not name:
            continue
        if not item.get("carrier"):
            out[name] = item
        for v in item.get("variants") or []:
            _, _, title = parse_key(str(v.get("key") or ""))
            title = title or str(v.get("key") or "").strip()
            if title:
                out[f"{name}{VARIANT_SEP}{title}"] = v
    return out


def exposure_text(entries: list[dict]) -> str:
    """The drawing model's rendering: every exposed entry in full."""
    if not entries:
        return wording.EXPOSURE_NONE
    blocks: list[str] = []
    for e in entries:
        if e["kind"] == "setting":
            kind = wording.ENTRY_KIND_SETTING + (f"·{e['category']}" if e.get("category") else "")
        elif e["kind"] == "character":
            kind = wording.ENTRY_KIND_CHARACTER
        else:
            kind = wording.ENTRY_KIND_VARIANT
        blocks.append("\n".join([f"◆ {e['key']}（{kind}）", *_field_lines(e, "  ")]))
    return "\n".join(blocks)


def exposed_keys_of(message: Message | None) -> list[str]:
    """The keys a drawn message exposed, for a picture that inherits from it;
    a picture from before exposures were stored has none."""
    if message is None or not isinstance(message.prompt, dict):
        return []
    keys = message.prompt.get("exposed_keys")
    if not isinstance(keys, list):
        return []
    return [str(k) for k in list(keys)]


# --------------------------------------------------------------- snapshots
def _stamps(obj) -> dict:
    return {k: (getattr(obj, k).isoformat() if getattr(obj, k, None) else None)
            for k in ("text_edited_at", "lines_ai_at", "lines_tried_at")}


def own_layer(conversation: Conversation) -> dict:
    """
    This conversation's own entries, every one of them, as they read now:
    what a branch made at this point needs to rebuild the layer and switch
    on the same ones. Unselected entries are in it too.
    """
    on_settings = set(conversation.selected_settings.values_list("id", flat=True))
    on_variants = set(conversation.selected_variants.values_list("id", flat=True))
    return {
        "settings": [
            {**entry_dict(s), "selected": s.id in on_settings, **_stamps(s)}
            for s in conversation.own_settings.order_by("position")
        ],
        "characters": [
            {
                **entry_dict(c), **_stamps(c),
                "variants": [
                    {**entry_dict(v, character=c), "selected": v.id in on_variants, **_stamps(v)}
                    for v in c.variants.order_by("position")
                ],
            }
            for c in conversation.own_characters.order_by("position")
        ],
    }


def rebuild_own_layer(own: dict, into: Conversation) -> tuple[list, list]:
    """
    Make an `own_layer` record this conversation's entries. Returns the
    settings and variants that record had switched on, for the caller to
    select -- a branch keeps the project layer's selection as well.
    """
    on_settings: list = []
    on_variants: list = []
    now = timezone.now()
    fallback = {"text_edited_at": now, "lines_ai_at": now + AI_AFTER_TEXT}

    def dated(rec: dict) -> dict:
        out: dict[str, Any] = dict(fallback)
        for k in ("text_edited_at", "lines_ai_at", "lines_tried_at"):
            if rec.get(k):
                when = parse_datetime(str(rec[k]))
                if when is not None:
                    out[k] = when
        if rec.get("text_edited_at") and not rec.get("lines_ai_at"):
            out["lines_ai_at"] = None
        return out

    def fields(rec: dict) -> dict:
        return {"positive": split_tags(rec.get("positive")), "negative": split_tags(rec.get("negative")),
                "prose": str(rec.get("prose") or ""), "origin_seq": rec.get("origin_seq")}

    for i, s in enumerate(own.get("settings") or [], 1):
        made = Setting.objects.create(
            folder=None, conversation=into, title=s.get("key") or s.get("title") or "",
            content=s.get("note") or s.get("content") or "", category=str(s.get("category") or "")[:40],
            position=i, **fields(s), **dated(s),
        )
        if s.get("selected"):
            on_settings.append(made)
    for i, c in enumerate(own.get("characters") or [], 1):
        made = Character.objects.create(
            folder=None, conversation=into, name=c.get("key") or c.get("name") or "",
            description=c.get("note") or c.get("description") or "", position=i, **fields(c), **dated(c),
        )
        for j, v in enumerate(c.get("variants") or [], 1):
            _, _, title = parse_key(str(v.get("key") or ""))
            variant = CharacterVariant.objects.create(
                character=made, title=title or v.get("title") or "", content=v.get("note") or v.get("content") or "",
                position=j, **fields(v), **dated(v),
            )
            if v.get("selected"):
                on_variants.append(variant)
        if not made.variants.exists():
            characters_service.repair(made)
    return on_settings, on_variants


#: what an entry says, for telling whether anyone changed it since a record
_SAID = ("key", "note", "positive", "negative", "prose", "category")


def _says(rec: dict) -> list:
    return [split_tags(rec.get(k)) if k in ("positive", "negative") else str(rec.get(k) or "").strip()
            for k in _SAID]


def _put_back(obj, rec: dict) -> None:
    """An entry made to say again what an own_layer record says, its dates
    included, so the patrol sees it as it was then."""
    note = str(rec.get("note") or "")
    if isinstance(obj, Character):
        obj.name, obj.description = str(rec.get("key") or obj.name), note
    elif isinstance(obj, CharacterVariant):
        _, _, title = parse_key(str(rec.get("key") or ""))
        obj.title, obj.content = title or obj.title, note
    else:
        obj.title, obj.content = str(rec.get("key") or obj.title), note
        obj.category = str(rec.get("category") or "")[:40]
    obj.positive = split_tags(rec.get("positive"))
    obj.negative = split_tags(rec.get("negative"))
    obj.prose = str(rec.get("prose") or "")
    obj.origin_seq = rec.get("origin_seq")
    for k in ("text_edited_at", "lines_ai_at", "lines_tried_at"):
        setattr(obj, k, parse_datetime(str(rec[k])) if rec.get(k) else None)
    obj.save()


def take_back(conversation: Conversation, before: dict, after: dict) -> list[str]:
    """
    Take back what one turn wrote into this conversation's own layer, for a
    重跑. `before` is the layer as the turn found it (its library_before),
    `after` as the turn left it (its library_snapshot); both are own_layer
    records, matched to the entries by id.

    An entry the turn made goes, one it rewrote says again what it said
    before, an outfit it removed comes back, and an entry it switched on is
    switched off again -- each only while it still reads as the turn left it.
    What a person changed since, the text or the switch, is theirs and stays:
    the turn asked again starts from the library as it is now. The project's
    entries are not touched (an edit to them went through a proposal).
    Returns what was done, one line each, for the log.
    """
    def by_id(records) -> dict[str, dict]:
        return {str(r["id"]): r for r in records or [] if isinstance(r, dict) and r.get("id")}

    def variants_of(layer: dict) -> dict[str, dict]:
        return {k: v for c in layer.get("characters") or [] for k, v in by_id(c.get("variants")).items()}

    def switch(selected, obj, now_on: bool, was: dict | None, left: dict) -> None:
        if was is not None and bool(was.get("selected")) != bool(left.get("selected")) \
                and now_on == bool(left.get("selected")):
            (selected.add if was.get("selected") else selected.remove)(obj)

    def untouched(obj, left: dict) -> bool:
        return _says(entry_dict(obj)) == _says(left)

    done: list[str] = []
    on_settings = {str(i) for i in conversation.selected_settings.values_list("id", flat=True)}
    on_variants = {str(i) for i in conversation.selected_variants.values_list("id", flat=True)}

    found = by_id(before.get("settings"))
    for sid, left in by_id(after.get("settings")).items():
        s = conversation.own_settings.filter(id=sid).first()
        if s is None:
            continue
        was = found.get(sid)
        if untouched(s, left):
            if was is None:
                done.append(f"- {s.title}")
                s.delete()
                continue
            if _says(was) != _says(left):
                _put_back(s, was)
                done.append(f"~ {s.title}")
        switch(conversation.selected_settings, s, sid in on_settings, was, left)

    found_chars = by_id(before.get("characters"))
    found_variants = variants_of(before)
    left_variants = variants_of(after)
    for cid, left in by_id(after.get("characters")).items():
        c = conversation.own_characters.filter(id=cid).first()
        if c is None:
            continue
        for v in list(c.variants.all()):
            vid = str(v.id)
            v_left = left_variants.get(vid)
            if v_left is None:
                continue  # made since, by hand
            v_was = found_variants.get(vid)
            if untouched(v, v_left):
                if v_was is None:
                    done.append("- " + ui_text.ENTRY_VARIANT.format(name=c.name, title=v.title))
                    v.delete()
                    continue
                if _says(v_was) != _says(v_left):
                    _put_back(v, v_was)
                    done.append("~ " + ui_text.ENTRY_VARIANT.format(name=c.name, title=v.title))
            switch(conversation.selected_variants, v, vid in on_variants, v_was, v_left)
        was = found_chars.get(cid)
        if was is not None:
            # an outfit the turn removed (the empty stand-in, once a real one
            # was written) comes back, switched on as it was
            for vid, v_was in by_id(was.get("variants")).items():
                if vid in left_variants or c.variants.filter(id=vid).exists():
                    continue
                _, _, title = parse_key(str(v_was.get("key") or ""))
                if not title or c.variants.filter(title=title).exists():
                    continue
                back = CharacterVariant(character=c, title=title,
                                        position=(c.variants.aggregate(m=Max("position"))["m"] or 0) + 1)
                _put_back(back, v_was)
                if v_was.get("selected"):
                    conversation.selected_variants.add(back)
        if untouched(c, left):
            if was is None and not c.variants.exists():
                done.append(f"- {c.name}")
                c.delete()
                continue
            if was is not None and _says(was) != _says(left):
                _put_back(c, was)
                done.append(f"~ {c.name}")
        if not c.variants.exists():
            characters_service.repair(c)
    return done


def snapshot(exposure: Exposure | None) -> dict | None:
    """The receipt of what a picture was drawn from: the exposed entries as
    they read at that moment. A copy, so editing an entry next month does
    not rewrite what an old picture says it was drawn from."""
    if exposure is None or not exposure.entries:
        return None
    return {"entries": [dict(e) for e in exposure.entries], "keys": list(exposure.keys)}
