"""
The library as the models read it: two layers, entries with tag lines.

An entry lives in a folder (the project's: every conversation under it reads
it) or on a conversation (that conversation's alone). The same name in both
layers means the conversation's wins — a near entry shadows a far one, the
rule the folder chain already uses.

Each entry carries tag lines: `[{"id": "k3", "part": "hair", "tags": "..."}]`.
The lines are the entry's truth table — everything that is true of the
character. What a picture takes from them (`use`, `patches`) is that picture's
own table and lives on the message. The ids are the app's: a line keeps its id across edits
of the entry, because pictures already drawn refer to it.

Two consumers, two renderings:
  * the lead model gets the entries appended to the newest user message —
    prose, lines, which layer — because it is the one that writes entries;
  * the drawing model gets the lines only, with ids, because it picks lines
    and never rewords them.
"""

from __future__ import annotations

import uuid
from typing import Any

import re
from datetime import timedelta

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from dataclasses import dataclass, field

from core.prompts import wording

from core.models import Character, CharacterVariant, Conversation, Folder, Setting


# --------------------------------------------------------------- tag lines


def lines_of(raw) -> list[dict]:
    """
    The tag lines of an entry, shape-checked. Anything else is dropped.

    A line a person wrote or changed by hand carries `by: user`; the key is
    absent on a line a model wrote, which is the common case.
    """
    out: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        tags = str(item.get("tags") or "").strip()
        if not tags:
            continue
        line = {
            "id": str(item.get("id") or "").strip(),
            "part": str(item.get("part") or "").strip() or wording.part_word("other"),
            "tags": tags,
        }
        if item.get("by") == LINE_BY_USER:
            line["by"] = LINE_BY_USER
        out.append(line)
    return out


LINE_BY_USER = "user"
#: a model's lines are stamped this much after the text they were written
#: from, so "the text changed after the lines" never ties on the clock
AI_AFTER_TEXT = timedelta(seconds=1)


def stamp_by(old: list[dict], new: list[dict], who: str) -> list[dict]:
    """
    Mark who last changed each of `new`'s lines: a line that reads exactly as
    the old line of the same id keeps its mark, every other line is `who`'s.
    So a person's line that a model copied out verbatim stays the person's.
    """
    before = {ln["id"]: ln for ln in old if ln.get("id")}
    out = []
    for ln in new:
        ln = dict(ln)
        was = before.get(ln.get("id") or "")
        if was is not None and (was["part"], was["tags"]) == (ln["part"], ln["tags"]):
            by = was.get("by")
        else:
            by = LINE_BY_USER if who == LINE_BY_USER else None
        ln.pop("by", None)
        if by == LINE_BY_USER:
            ln["by"] = LINE_BY_USER
        out.append(ln)
    return out


def set_text(obj, text: str, *, now=None) -> bool:
    """Store an entry's text. Says whether it changed, and dates the change."""
    field = "description" if isinstance(obj, Character) else "content"
    if text == getattr(obj, field):
        return False
    setattr(obj, field, text)
    now = now or timezone.now()
    # a model's lines are dated a hair after the text they came from; an edit
    # landing inside that hair is still an edit after the look
    if obj.lines_ai_at is not None and obj.lines_ai_at >= now:
        now = obj.lines_ai_at + timedelta(microseconds=1)
    obj.text_edited_at = now
    return True


def lines_by_model(obj, *, now=None) -> None:
    """A model wrote the lines, or looked and let them stand: dated after the text."""
    now = now or timezone.now()
    base = max(now, obj.text_edited_at) if obj.text_edited_at else now
    obj.lines_ai_at = base + AI_AFTER_TEXT


def _split(text: str) -> list[str]:
    return [t.strip() for t in (text or "").split(",") if t.strip()]


def _tagset(line: dict) -> set[str]:
    return {t.lower() for t in _split(line.get("tags") or "")}


def assign_ids(old: list[dict], new: list[dict], prefix: str,
               taken: set[str] | None = None) -> list[dict]:
    """
    Give every new line an id, keeping the old id for the same thing.

    Pictures already drawn store line ids. If recording a character re-proposed
    the whole entry and every line got a fresh id, those pictures would point at
    nothing — and an edit of one would quietly drop the lines it cannot find.
    An id, once handed out, is kept: first by the line's name, then by what it
    says (tag overlap ≥ 0.5) when the name changed. Only a line that is really
    gone loses its id.

    `taken` is ids a new line may not take besides the old lines' own: a
    costume's lines share their id space with the character's other costumes
    (see variant_ids_taken), because a picture names a line by id alone.
    """
    taken = set(taken or ()) | {ln["id"] for ln in old if ln.get("id")}
    by_part: dict[str, dict] = {}
    for ln in old:
        by_part.setdefault(ln.get("part") or "", ln)
    spare = {ln["id"]: ln for ln in old if ln.get("id")}

    out = [dict(ln) for ln in new]
    for ln in out:
        ln["id"] = ""
        match = by_part.get(ln["part"])
        if match and match["id"] in spare:
            ln["id"] = spare.pop(match["id"])["id"]
    for ln in out:
        if ln["id"]:
            continue
        mine = _tagset(ln)
        best, score = None, 0.0
        for lid, cand in spare.items():
            theirs = _tagset(cand)
            if not mine or not theirs:
                continue
            overlap = len(mine & theirs) / len(mine | theirs)
            if overlap > score:
                best, score = lid, overlap
        if best is not None and score >= 0.5:
            ln["id"] = spare.pop(best)["id"]
    n = 0
    for ln in out:
        if ln["id"]:
            continue
        while True:
            n += 1
            cand = f"{prefix}{n}"
            if cand not in taken:
                break
        taken.add(cand)
        ln["id"] = cand
    return out


def variant_ids_taken(character, exclude=None) -> set[str]:
    """
    Line ids in use by the character's costumes other than `exclude`.

    A picture's `use` names a costume's line by id alone, so two costumes of
    one character may not both have a v1 -- which, numbered on their own, they
    did: a second costume's lines shadowed the first's in the truth table.
    """
    out: set[str] = set()
    for v in character.variants.all():
        if exclude is not None and v.id == exclude:
            continue
        out |= {ln["id"] for ln in lines_of(v.tags) if ln.get("id")}
    return out


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", text.lower()))


def _nearest_piece(key: str, by_key: dict, taken: set[str]) -> str | None:
    """
    The source piece that says what `key` says, for a sentence: tags are
    matched word for word, but a sentence the model wrote about the picture
    will not be the sentence the picture was drawn from. Word overlap of at
    least half, over pieces of three words or more, is close enough; the
    piece is then copied as the picture had it.
    """
    mine = _words(key)
    if len(mine) < 3:
        return None
    best, score = None, 0.0
    for cand in by_key:
        if cand in taken:
            continue
        theirs = _words(cand)
        if len(theirs) < 3:
            continue
        overlap = len(mine & theirs) / len(mine | theirs)
        if overlap > score:
            best, score = cand, overlap
    return best if score >= 0.5 else None


def copied_lines(source: str, raw, taken: set[str] | None = None,
                 keep_rest: bool = True) -> tuple[list[dict], list[str]]:
    """
    Lines for an entry made from a picture already drawn: (lines, dropped).

    The user approved that picture, not the model's memory of it. So the model
    only says how to split the lines; every word comes from the picture. A word
    the picture has and the model left out goes into a last line rather than
    being lost; a word the model made up is dropped and reported.

    `taken` is shared between a character and its outfit when both are cut from
    the same picture, so each word is claimed once.
    """
    have = _split(source)
    # an underscore and a space are the same tag: Danbooru writes the one,
    # NovelAI the other, and the model may write either
    norm = lambda t: t.lower().replace("_", " ")          # noqa: E731
    by_key = {norm(t): t for t in have}
    order = {norm(t): i for i, t in enumerate(have)}
    taken = set() if taken is None else taken
    dropped: list[str] = []
    lines: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        part = str(item.get("part") or "").strip() or wording.part_word("other")
        keys = []
        for t in _split(str(item.get("tags") or "")):
            key = norm(t)
            if key in by_key and key not in taken:
                taken.add(key)
                keys.append(key)
            elif key not in by_key:
                # a sentence is never repeated word for word: the picture's
                # piece that says the same thing stands in for it
                near = _nearest_piece(key, by_key, taken)
                if near is None:
                    dropped.append(t)
                else:
                    taken.add(near)
                    keys.append(near)
        if keys:
            keys.sort(key=lambda k: order[k])
            lines.append({"id": "", "part": part, "tags": ", ".join(by_key[k] for k in keys)})
    if keep_rest:
        rest = [t for t in have if norm(t) not in taken]
        if rest:
            lines.append({"id": "", "part": wording.part_word("other"), "tags": ", ".join(rest)})
            taken |= {norm(t) for t in rest}
    return lines, dropped


# --------------------------------------------------------------- the selection


@dataclass
class Selection:
    """
    What a conversation draws on this turn: the settings and character
    variants switched on, with the conversation layer shadowing the project.
    """

    settings: list = field(default_factory=list)
    variants: list = field(default_factory=list)
    #: whether the conversation sits in a project, and which
    project_name: str = ""
    in_project: bool = False
    #: entries the conversation could switch on but has not -- the rest of
    #: the project. The lead is shown their names so it can draw on one by
    #: naming it (see switch_on) instead of making it up again.
    off_settings: list = field(default_factory=list)
    off_characters: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self.by_character: dict = {}
        for v in self.variants:
            self.by_character.setdefault(v.character, []).append(v)

    def __bool__(self) -> bool:
        return bool(self.settings or self.by_character)


def _shadowed(items, key):
    """Keep the nearest of each name: conversation-layer entries win."""
    out: dict = {}
    for it in items:
        k = key(it)
        prev = out.get(k)
        if prev is None or (prev.conversation_id is None and it.conversation_id is not None):
            out[k] = it
    return list(out.values())


def selection_for(conversation: Conversation) -> Selection:
    settings = _shadowed(conversation.selected_settings.all(), lambda s: s.title)
    variants = list(conversation.selected_variants.select_related("character").all())
    # a character selected in both layers: the conversation's copy shadows
    chars = _shadowed({v.character for v in variants}, lambda c: c.name)
    keep = {c.id for c in chars}
    variants = [v for v in variants if v.character_id in keep]
    folder = conversation.folder
    on_settings = {s.title for s in settings}
    on_chars = {c.name for c in chars}
    all_settings, all_chars = visible_entries(conversation)
    return Selection(
        settings=settings, variants=variants,
        project_name=folder.name if folder else "",
        in_project=folder is not None,
        off_settings=[s for s in all_settings if s.title not in on_settings],
        off_characters=[c for c in all_chars if c.name not in on_chars],
    )


def switch_on(conversation: Conversation, subjects: list[dict]) -> list[str]:
    """
    Switch on the project characters a picture names that are not on yet.

    The lead is shown the whole project, and draws a character that is off
    by naming it -- the user said 「画阿岚」 and should not have to open the
    sidebar first. The costume is the one `outfit` names, or the character's
    first when it names none. Returns what was switched on, for the log.
    """
    sel = selection_for(conversation)
    off = {c.name: c for c in sel.off_characters}
    done: list[str] = []
    for sj in subjects or []:
        who = str((sj or {}).get("who") or "").strip()
        character = off.get(who)
        if character is None:
            continue
        variants = list(character.variants.order_by("position", "created_at"))
        if not variants:
            continue
        outfit = str(sj.get("outfit") or "").strip()
        chosen = next((v for v in variants if v.title == outfit), variants[0])
        conversation.selected_variants.add(chosen)
        done.append(f"{who}·{chosen.title}")
        off.pop(who, None)
    return done


def project_settings(folder: Folder) -> list[Setting]:
    """The project layer's settings, the folder chain's: what a conversation
    that starts in this project begins with."""
    ids = [f.id for f in folder.ancestors()]
    return list(Setting.objects.filter(folder_id__in=ids, conversation__isnull=True))


def rehome(conversation: Conversation) -> None:
    """
    A conversation that has just changed folder: the new project's settings
    come on, and selections of entries no longer in reach are dropped (they
    would be drawn from while appearing in no list). The conversation's own
    entries come along wherever it goes.
    """
    if conversation.folder is not None:
        conversation.selected_settings.add(*project_settings(conversation.folder))
    settings, characters = visible_entries(conversation)
    conversation.selected_settings.set(
        conversation.selected_settings.filter(id__in=[s.id for s in settings])
    )
    allowed = [v.id for c in characters for v in c.variants.all()]
    conversation.selected_variants.set(conversation.selected_variants.filter(id__in=allowed))


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


# --------------------------------------------------------------- renderings


def _scope_mark(entry) -> str:
    return wording.LIBRARY_CONVERSATION_MARK if entry.conversation_id else ""


def as_prompt_text(selection: Selection) -> str:
    """
    The lead model's rendering: prose, lines with ids, which layer.

    The first line says whether the conversation is in a project — the lead
    proposes which layer an entry goes to, and cannot propose the project
    layer when there is none.
    """
    head = (
        wording.LIBRARY_IN_PROJECT.format(project=selection.project_name)
        if selection.in_project else wording.LIBRARY_NO_PROJECT
    )
    if not selection and not (selection.off_characters or selection.off_settings):
        return head + "\n" + wording.LIBRARY_EMPTY
    blocks: list[str] = [head]
    if not selection:
        blocks.append(wording.LIBRARY_NOTHING_ON)
    for character, variants in selection.by_character.items():
        lines = [f"◆ {wording.LIBRARY_CHARACTER_HEADING.format(name=character.name)}{_scope_mark(character)}"]
        if character.description:
            lines.append(f"  {character.description}")
        for ln in lines_of(character.tags):
            lines.append(f"    {ln['id']}  {ln['part']}：{ln['tags']}")
        for v in variants:
            lines.append("  " + wording.LIBRARY_VARIANT_LABEL.format(title=v.title) + f"：{v.content}")
            for ln in lines_of(v.tags):
                lines.append(f"    {ln['id']}  {ln['part']}：{ln['tags']}")
        blocks.append("\n".join(lines))
    for s in selection.settings:
        lines = [f"◆ {wording.LIBRARY_SETTING_HEADING.format(title=s.title)}{_scope_mark(s)}：{s.content}"]
        for ln in lines_of(s.tags):
            lines.append(f"    {ln['id']}  {ln['part']}：{ln['tags']}")
        blocks.append("\n".join(lines))
    blocks.extend(_off_entries(selection))
    return "\n".join(blocks)


def _off_entries(selection: Selection) -> list[str]:
    """The rest of the project, one line each: what exists, not its lines."""
    if not (selection.off_characters or selection.off_settings):
        return []
    out = [wording.LIBRARY_OFF_HEADING]
    for c in selection.off_characters:
        line = wording.LIBRARY_OFF_CHARACTER.format(name=c.name, text=_brief(c.description))
        titles = [v.title for v in c.variants.all()]
        if titles:
            line += "；" + wording.LIBRARY_OFF_VARIANTS.format(titles="、".join(titles))
        out.append(line)
    for s in selection.off_settings:
        out.append(wording.LIBRARY_OFF_SETTING.format(title=s.title, text=_brief(s.content)))
    return out


def _brief(text: str, limit: int = 60) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def is_style_line(ln: dict) -> bool:
    """Whether a setting's line is a style line (its part is the style part):
    spliced into every picture's style slot by the app rather than picked."""
    return wording.is_part(ln.get("part") or "", "style")


def as_lines(selection: Selection) -> str:
    """
    The drawing model's rendering: lines with ids.

    An entry that has no lines yet — written before lines existed, or by hand
    in prose — still has to reach the drawing model somehow, so its prose
    stands in, under a label that says it is prose and not lines to pick.
    A setting's lines are listed with ids like a character's, because the
    model picks them the same way; its style lines are marked as the app's to
    splice, so the model neither writes nor picks them.
    """
    out: list[str] = []
    for character, variants in selection.by_character.items():
        out.append(f"◆ {wording.LIBRARY_CHARACTER_HEADING.format(name=character.name)}")
        lines = lines_of(character.tags)
        for ln in lines:
            out.append(f"    {ln['id']}  {ln['part']}：{ln['tags']}")
        if not lines and character.description:
            out.append(f"    {wording.LIBRARY_PROSE_LABEL}：{character.description}")
        for v in variants:
            out.append("  " + wording.LIBRARY_VARIANT_LABEL.format(title=v.title))
            vlines = lines_of(v.tags)
            for ln in vlines:
                out.append(f"    {ln['id']}  {ln['part']}：{ln['tags']}")
            if not vlines and v.content:
                out.append(f"    {wording.LIBRARY_PROSE_LABEL}：{v.content}")
    for s in selection.settings:
        slines = lines_of(s.tags)
        out.append(f"◆ {wording.LIBRARY_SETTING_HEADING.format(title=s.title)}")
        for ln in slines:
            mark = wording.LIBRARY_STYLE_LINE_MARK if is_style_line(ln) else ""
            out.append(f"    {ln['id']}  {ln['part']}：{ln['tags']}{mark}")
        if not slines and s.content:
            out.append(f"    {wording.LIBRARY_PROSE_LABEL}：{s.content}")
    return "\n".join(out) if out else wording.LIBRARY_NO_LINES


def lines_map(selection: Selection) -> dict[str, dict[str, str]]:
    """{character name: {line id: tags}} — what render() splices by."""
    out: dict[str, dict[str, str]] = {}
    for character, variants in selection.by_character.items():
        book = {ln["id"]: ln["tags"] for ln in lines_of(character.tags)}
        for v in variants:
            book.update({ln["id"]: ln["tags"] for ln in lines_of(v.tags)})
        out[character.name] = book
    return out


def parts_map(selection: Selection) -> dict[str, dict[str, str]]:
    """{character name: {line id: what the line is}} — for check()."""
    out: dict[str, dict[str, str]] = {}
    for character, variants in selection.by_character.items():
        book = {ln["id"]: ln["part"] for ln in lines_of(character.tags)}
        for v in variants:
            book.update({ln["id"]: ln["part"] for ln in lines_of(v.tags)})
        out[character.name] = book
    return out


def truth_table(selection: Selection, name: str) -> list[tuple[str, str, str, str]]:
    """One character's truth table: [(line id, outfit or "", part, tags)]."""
    for character, variants in selection.by_character.items():
        if character.name != name:
            continue
        rows = [(ln["id"], "", ln["part"], ln["tags"]) for ln in lines_of(character.tags)]
        for v in variants:
            rows += [(ln["id"], v.title, ln["part"], ln["tags"]) for ln in lines_of(v.tags)]
        return rows
    return []


def setting_lines_map(selection: Selection) -> dict[str, dict[str, str]]:
    """{setting title: {line id: tags}} — what render() splices a picture's
    setting picks by."""
    return {s.title: {ln["id"]: ln["tags"] for ln in lines_of(s.tags)} for s in selection.settings}


def style_lines_map(selection: Selection) -> dict[str, dict[str, str]]:
    """{setting title: {line id: tags}} of the style lines alone — what
    prompt.splice_style fills the style slot from."""
    out: dict[str, dict[str, str]] = {}
    for s in selection.settings:
        book = {ln["id"]: ln["tags"] for ln in lines_of(s.tags) if is_style_line(ln)}
        if book:
            out[s.title] = book
    return out


def setting_truth(selection: Selection) -> dict[str, list[tuple[str, str, str, bool]]]:
    """Every selected setting's lines: {title: [(line id, part, tags, is style line)]}."""
    return {
        s.title: [(ln["id"], ln["part"], ln["tags"], is_style_line(ln)) for ln in lines_of(s.tags)]
        for s in selection.settings
    }


def own_layer(conversation: Conversation) -> dict:
    """
    This conversation's own entries, every one of them, as they read now.

    What a branch made at this point needs: enough to rebuild the entries
    (ids included, so the pictures it copies still point at their lines) and
    to switch on the same ones. Unselected entries are in it too -- switched
    off is a state of an entry, not its absence.
    """
    on_settings = set(conversation.selected_settings.values_list("id", flat=True))
    on_variants = set(conversation.selected_variants.values_list("id", flat=True))

    def stamps(obj) -> dict:
        # when the text was last edited and when the lines were last written:
        # an entry whose lines are behind its text stays behind in a copy
        return {k: (getattr(obj, k).isoformat() if getattr(obj, k, None) else None)
                for k in ("text_edited_at", "lines_ai_at", "lines_tried_at")}

    return {
        "settings": [
            {"id": str(s.id), "title": s.title, "content": s.content,
             "tags": lines_of(s.tags), "selected": s.id in on_settings, **stamps(s)}
            for s in conversation.own_settings.order_by("position")
        ],
        "characters": [
            {
                "id": str(c.id), "name": c.name, "description": c.description,
                "tags": lines_of(c.tags), **stamps(c),
                "variants": [
                    {"id": str(v.id), "title": v.title, "content": v.content,
                     "tags": lines_of(v.tags), "selected": v.id in on_variants, **stamps(v)}
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
    # A record that carries the stamps keeps them: an entry whose lines were
    # behind its text is still behind, and the line writer gets to it. A
    # record from before the stamps were kept is dated as a model's, or the
    # writer would redo every entry of a fresh branch.
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

    for i, s in enumerate(own.get("settings") or [], 1):
        made = Setting.objects.create(
            folder=None, conversation=into, title=s.get("title") or "",
            content=s.get("content") or "", tags=lines_of(s.get("tags")), position=i, **dated(s),
        )
        if s.get("selected"):
            on_settings.append(made)
    for i, c in enumerate(own.get("characters") or [], 1):
        made = Character.objects.create(
            folder=None, conversation=into, name=c.get("name") or "",
            description=c.get("description") or "", tags=lines_of(c.get("tags")), position=i, **dated(c),
        )
        for j, v in enumerate(c.get("variants") or [], 1):
            variant = CharacterVariant.objects.create(
                character=made, title=v.get("title") or "", content=v.get("content") or "",
                tags=lines_of(v.get("tags")), position=j, **dated(v),
            )
            if v.get("selected"):
                on_variants.append(variant)
    return on_settings, on_variants


def snapshot(selection: Selection) -> dict | None:
    """
    A record of what was selected when a turn ran — titles *and* the text and
    lines as they read at that moment.

    The text is copied, not referenced, on purpose: editing a character next
    month must not silently rewrite what an old picture says it was drawn
    from. This is a receipt, so it has to keep the old numbers.
    """
    if not selection:
        return None
    return {
        "settings": [
            {"id": str(s.id), "title": s.title, "content": s.content,
             "tags": lines_of(s.tags), "layer": "conversation" if s.conversation_id else "project"}
            for s in selection.settings
        ],
        "characters": [
            {
                "id": str(c.id),
                "name": c.name,
                "description": c.description,
                "tags": lines_of(c.tags),
                "layer": "conversation" if c.conversation_id else "project",
                "variants": [
                    {"id": str(v.id), "title": v.title, "content": v.content,
                     "tags": lines_of(v.tags)}
                    for v in vs
                ],
            }
            for c, vs in selection.by_character.items()
        ],
    }
