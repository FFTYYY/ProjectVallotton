"""
One picture's prompt, kept in slots and assembled here.

The drawing model used to hand back one `base_prompt` string and the app sent
it on. That made every later question unanswerable: which part of this string
is the lighting, and may I change it without touching the person? A string has
no parts. So the model fills in named slots and this module is the only place
that turns them into what NovelAI receives.

Three things live here and nowhere else: the slot table, which decides who may
write what and how it may later be changed; `apply`, which merges a change
list into a picture so that "what was not mentioned stays byte for byte" is a
property of code rather than a virtue asked of a model; and `render`, which
decides the order it all goes out in. Quality words are deliberately not here —
they are appended by `novelai._build_payload`, which is already the app's doing
rather than the model's, and adding them in two places would be one place too
many.

Every slot has two halves, `tags` and `prose`. NovelAI reads tags as an
unordered list and sentences as ordered text that belongs after the list, so
the two are kept apart by structure rather than guessed apart: a slot's tags
go out with the other slots' tags, its sentences go out with the other slots'
sentences, after all of them. Nothing is moved by heuristics any more -- a
sentence is a sentence because the model put it in `prose`.

The shape a picture takes:

    {
      "size": "portrait",
      "slots": {
        "count":  {"tags": "1girl, solo", "prose": ""},
        "style":  {"tags": "...", "prose": ""},
        "camera": {...}, "scene": {...}, "light": {...}, "other": {...},
        "text": ""
      },
      "settings": [{"from": "图书馆", "use": ["s1", "s2"]}],
      "characters": [
        {"key": "林", "from": "林", "use": ["k1", "k2", "v1"],
         "patches": [{"line": "v1", "add": "hood up"}],
         "look": {"tags": "", "prose": ""},
         "moment": {"tags": "smoking", "prose": ""},
         "center_x": None, "center_y": None},
      ],
      "negative": "...",
    }

A character's `use`, `patches`, `look` and `moment` are its 表征表 for this
picture — what of its entry's 真实表 (the tag lines) is in it, in what state.
`key` is the handle a change list addresses it by, and it is the app's: the
entry's name when there is one, otherwise 主体1, 主体2 — so a subject that is
not in the library can still be told "take the umbrella away".

`settings` is the same idea for setting entries: which of a selected setting's
lines this picture takes, by id. A setting's 画风 lines are not picked -- the
app splices them into the style slot of every picture (pipeline) -- the rest
are picked like a character's lines.

Pictures written before slots had two halves (a string per slot, a `note`
slot for sentences) are read through `upgrade`, which gives them the shape
above without inventing anything: the string becomes the tags, the note
becomes the other slot's prose.

What `render` promises about the prompt NovelAI receives -- the invariants.
`lint` checks a rendered prompt against them, in the test suite and in the
audit of every real run, so a change to the assembly is held to the same
list rather than to whoever last looked at a prompt:

  1. Shape. base = tags, then sentences, then the text slot; each character
     = tags, then sentences. Tags come from the tag halves, sentences from
     the prose halves; nothing is moved from one to the other by the app.
  2. Order. base tags in slot order (count, style, camera, scene, light,
     other); inside the style slot: the settings' style lines (patched for
     this picture), the picture's own style tags, then the picked setting
     lines. Character tags: the picked lines in `use` order (patched), then
     look.tags, then moment.tags. Prose in the same order as its slots.
  3. Separators. Tags are set apart by ", ". Sentences end in a stop and are
     set apart by a space. A stop is never followed by a comma, a comma never
     by a stop; no empty item, no doubled separator, no separator at either
     end, no doubled space.
  4. No repeats. A tag appears once, the first time it is written (case
     blind): two entries sharing a tag must not weight it.
  5. Verbatim lines. A picked line goes out as the entry has it, changed
     only by this picture's patches; a line not in `use` does not go out.
  6. Script. Everything is English except the text slot: a sign may read in
     Chinese, a sentence about the picture may not -- NovelAI's tags are
     English, and a Chinese sentence is the model answering the brief's
     language instead of writing a prompt. (Checked on the picture, by
     `foreign`, before rendering; a hit is a bad answer and is retried.)
  7. Nothing empty. A character with nothing to say is left out; a base
     prompt with no tags is a bad answer and is retried.
  8. NovelAI's own words are in the stored prompt, where they belong: the
     quality words at the end of the base tag list (or earlier, where a
     style line already wrote them -- a tag is written once, the first
     time), before the sentences and the text, and `no text` only when
     there is no text; the default negative behind the picture's own, each
     word once. The prompt a row
     stores IS the prompt NovelAI receives -- what the inspector and the
     reports show is what was sent, not most of it. (They were once appended
     by the payload builder after the text, and a sign came out reading
     「Midnight Pages, no text」.)
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass

from core.prompts import wording
from core import ui_text
from core.services import novelai


class PatchRejected(Exception):
    """A change list the slot table does not allow."""


@dataclass(frozen=True)
class Slot:
    name: str
    #: Which edits the TAGS half allows. A slot that only takes `set` has to be
    #: rewritten whole; one that also takes `add`/`remove` can be nudged a word
    #: at a time. The prose half of every slot only takes `set`: a sentence
    #: cannot be nudged.
    #:
    #: The dividing line is whether the contents can contradict each other. A
    #: pose can: last picture said both hands on the wheel, this one says she is
    #: smoking, and a change that may only add leaves the painter to reconcile
    #: two sets of hands. So poses get rewritten whole, and no slot is given
    #: `add` as a convenience.
    ops: tuple[str, ...]
    #: Assembly order, lowest first.
    order: int


#: The slots shared by the whole picture, each with a tags half and a prose
#: half. Characters have their own, below. `text` is apart: a string NovelAI
#: renders as text, always last.
SLOTS: tuple[Slot, ...] = (
    Slot("count", ("set",), 1),
    Slot("style", ("set", "add", "remove"), 2),
    Slot("camera", ("set",), 3),
    Slot("scene", ("set",), 4),
    Slot("light", ("set",), 5),
    Slot("other", ("set",), 6),
)
BY_NAME = {s.name: s for s in SLOTS}

#: The two-halved slots, in assembly order.
FIELD_SLOTS: tuple[str, ...] = tuple(s.name for s in sorted(SLOTS, key=lambda s: s.order))

#: The rendered-text slot: a string, sent last.
TEXT_SLOT = "text"

#: Every slot the model fills in, by name.
MODEL_SLOTS: tuple[str, ...] = FIELD_SLOTS + (TEXT_SLOT,)

#: Only `set`: the prose half of a slot, the text slot, use, patches, moment.
SET_ONLY: tuple[str, ...] = ("set",)

#: A character's own slots.
#:
#: `use` is picked afresh for every picture, edits included: the composition
#: changed, so what can be seen changed, so the lines are chosen again from
#: the entry rather than inherited and nudged — a hand taken out of a pocket
#: brings the ring line back without anyone having to remember it was dropped.
#: `patches` is a set of structured patches and is rewritten as a set. `look`
#: can be nudged; `moment` contradicts itself between pictures and cannot.
CHAR_OPS: dict[str, tuple[str, ...]] = {
    "use": SET_ONLY,
    "patches": SET_ONLY,
    "look": ("set", "add", "remove"),
    "moment": SET_ONLY,
}
#: The character slots that have a tags half and a prose half.
CHAR_FIELDS: tuple[str, ...] = ("look", "moment")

#: Negatives are a list of things to avoid, and two of them cannot contradict
#: each other, so adding one more need not mean rewriting the rest.
NEGATIVE_OPS: tuple[str, ...] = ("set", "add", "remove")


def tags(text: str) -> list[str]:
    return [t.strip() for t in (text or "").split(",") if t.strip()]


def sentences(pieces) -> str:
    """
    Prose pieces as one run of sentences (invariant 3): each ends with a
    stop and they are set apart by a space. Joined with a comma like tags
    they read 「...passersby., Seen from...」, a stop and a comma in a row.
    """
    out = []
    for piece in pieces:
        piece = " ".join((piece or "").split()).strip(" ,;")
        if not piece:
            continue
        if piece[-1] not in ".!?":
            piece += "."
        out.append(piece)
    return " ".join(out)


def glue(parts) -> str:
    """Tags and prose in one string: a comma between tags, a space after a
    sentence -- never 「., 」 (invariant 3)."""
    out = ""
    for part in parts:
        part = (part or "").strip()
        if not part:
            continue
        if not out:
            out = part
        elif out[-1] in ".!?":
            out += " " + part
        else:
            out += ", " + part
    return out


_FOREIGN = re.compile("[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")


def foreign(picture: dict) -> list[str]:
    """
    Pieces of a picture written in a script NovelAI's tags are not
    (invariant 6): the drawing model now and then answers a Chinese brief
    with Chinese prose. The text slot is left alone.
    """
    found: list[str] = []
    slots = picture.get("slots") or {}
    for name, half in slots.items():
        if name == TEXT_SLOT or not isinstance(half, dict):
            continue
        for key in ("tags", "prose"):
            if _FOREIGN.search(half.get(key) or ""):
                found.append(f"{name}.{key}")
    for i, ch in enumerate(picture.get("characters") or [], 1):
        for part in ("look", "moment"):
            for key in ("tags", "prose"):
                if _FOREIGN.search(((ch.get(part) or {}).get(key)) or ""):
                    found.append(f"{ch.get('key') or ch.get('from') or i}.{part}.{key}")
    return found


_SEPARATOR_FAULTS = (
    (re.compile(r"[.!?]\s*,"), "a comma after a stop"),
    (re.compile(r",\s*[.!?]"), "a stop after a comma"),
    (re.compile(r",\s*,"), "an empty item between commas"),
    (re.compile(r"^\s*[,.]|[,]\s*$"), "a separator at an end"),
    (re.compile(r"\s{2,}"), "a doubled space"),
    (re.compile(r"\s,"), "a space before a comma"),
)


def used_entries(picture: dict | None, snapshot: dict | None) -> list[dict]:
    """
    What this picture took from the library, entry by entry: the lines each
    subject used and each setting pick, named by part. The transcript used to
    list the whole switched-on selection as 「用到的资料」 -- an entry that
    was on but not in the picture read as used. This is the picture's own
    account, from its slot table and the turn's receipt.
    """
    if not isinstance(picture, dict):
        return []
    picture = upgrade(picture)
    parts: dict[str, dict[str, str]] = {}
    style_of: dict[str, list[str]] = {}
    for s in (snapshot or {}).get("settings") or []:
        name = str(s.get("title") or "")
        parts[name] = {ln["id"]: ln["part"] for ln in s.get("tags") or [] if ln.get("id")}
        style_of[name] = [ln["id"] for ln in s.get("tags") or [] if ln.get("part") == wording.LINE_PART_STYLE]
    for c in (snapshot or {}).get("characters") or []:
        book = {ln["id"]: ln["part"] for ln in c.get("tags") or [] if ln.get("id")}
        for v in c.get("variants") or []:
            book.update({ln["id"]: f"{v.get('title')}·{ln['part']}" for ln in v.get("tags") or [] if ln.get("id")})
        parts[str(c.get("name") or "")] = book
    out: list[dict] = []
    for ch in picture["characters"]:
        name = ch.get("from") or ""
        if not name:
            continue
        book = parts.get(name, {})
        out.append({"kind": "character", "name": name,
                    "lines": [book.get(k, k) for k in ch.get("use") or []],
                    "patches": len(ch.get("patches") or [])})
    picked = {p.get("from"): p for p in picture["settings"]}
    for name, book in parts.items():
        if name in {c.get("name") for c in (snapshot or {}).get("characters") or []}:
            continue
        pick = picked.get(name) or {}
        lines = [book.get(k, k) for k in style_of.get(name, [])]      # style lines go in every picture
        lines += [book.get(k, k) for k in pick.get("use") or [] if k not in style_of.get(name, [])]
        if lines:
            out.append({"kind": "setting", "name": name, "lines": lines,
                        "patches": len(pick.get("patches") or [])})
    return out


def said_in(picture: dict | None) -> dict[str, str]:
    """
    The run of sentences each prompt carries, by prompt name, built the way
    `render` builds it. lint takes the sentences out before it looks for
    repeated tags: a sentence has commas of its own, and the first piece of
    「Bread, wine and plates lie on the table.」 is not the tag 「bread」 twice.
    """
    slots = (picture or {}).get("slots") or {}
    runs = {"base": sentences(field(slots.get(name))["prose"] for name in FIELD_SLOTS)}
    for i, ch in enumerate((picture or {}).get("characters") or [], 1):
        key = str(ch.get("key") or ch.get("from") or f"主体{i}")
        runs[f"character {key}"] = sentences([field(ch.get("look"))["prose"], field(ch.get("moment"))["prose"]])
    return runs


def lint(nai_prompts: dict, picture: dict | None = None) -> list[str]:
    """
    What a rendered prompt breaks of the invariants above: separators (3),
    repeats (4), script (6) and emptiness (7). Returns one line per fault,
    empty when the prompt is as promised. The picture is given so that its
    sentences are not read as tags and the text slot's script is not held
    against the prompt; without it, both are judged from the string alone.
    """
    faults: list[str] = []
    text = ((picture or {}).get("slots") or {}).get(TEXT_SLOT) or ""
    said = said_in(picture)
    pieces = [("base", nai_prompts.get("base_prompt") or "")]
    # a character prompt carries the key of the subject it was rendered
    # from; render leaves subjects with nothing to say out, so a place in
    # the list does not say which subject it is
    pieces += [(f"character {c.get('key') or i}", c.get("prompt") or "")
               for i, c in enumerate(nai_prompts.get("character_prompts") or [], 1)]
    pieces.append(("negative", nai_prompts.get("negative_prompt") or ""))
    for where, prompt in pieces:
        if where == "base" and not prompt:
            faults.append("base: empty")
        if where.startswith("character") and not prompt:
            faults.append(f"{where}: empty")
        for pattern, what in _SEPARATOR_FAULTS:
            if pattern.search(prompt):
                faults.append(f"{where}: {what}")
        tagged = prompt
        for spoken in (said.get(where, ""), text):
            if spoken:
                tagged = tagged.replace(spoken, "")
        seen: set[str] = set()
        for t in tags(tagged):
            key = t.rstrip(".!?").strip().lower()
            if key in seen:
                faults.append(f"{where}: repeated tag 「{t}」")
            seen.add(key)
        body = prompt.replace(text, "") if text else prompt
        if _FOREIGN.search(body):
            faults.append(f"{where}: not English")
        if where == "base" and text:
            # NovelAI's own words beside a text are read as the text (8)
            if re.search(r"(^|,\s*)" + re.escape(novelai.NO_TEXT_WORD) + r"(\s*,|\s*$)", prompt, re.I):
                faults.append("base: 「no text」 beside a text")
            after = prompt[prompt.rfind(text) + len(text):] if text in prompt else ""
            if after.strip(" ,"):
                faults.append("base: words after the text")
    return faults


def join(items) -> str:
    """Join back up, dropping repeats and keeping the first of each.

    Load-bearing, not tidiness: two library entries can carry the same style
    tags, and sending a tag twice is how NovelAI is told to weight it heavily.
    """
    out, seen = [], set()
    for t in items:
        # a line may carry a sentence (「a rope is knotted at her waist.」);
        # as an item in a tag list it goes without its stop, or the next
        # comma would follow a stop (invariant 3)
        t = (t or "").strip()
        bare = t.rstrip(".!?").strip()
        # 「!」「!?」「...」 are tags on Danbooru; only a stop BEHIND words goes
        if bare:
            t = bare
        key = t.lower()
        if t and key not in seen:
            seen.add(key)
            out.append(t)
    return ", ".join(out)


def field(value) -> dict:
    """
    A slot's two halves, whatever shape they arrived in.

    A dict is read as {tags, prose}; a string is tags (the shape pictures had
    before prose was its own half, and what a model that forgot the split
    sends); anything else is empty.
    """
    if isinstance(value, dict):
        return {
            "tags": str(value.get("tags") or "").strip(),
            "prose": str(value.get("prose") or "").strip(),
        }
    if isinstance(value, str):
        return {"tags": value.strip(), "prose": ""}
    return {"tags": "", "prose": ""}


def empty() -> dict:
    """A picture with every slot present and blank."""
    return {
        "size": "portrait",
        "slots": {**{name: field(None) for name in FIELD_SLOTS}, TEXT_SLOT: ""},
        "settings": [],
        "characters": [],
        "negative": "",
    }


def clean_settings(raw) -> list[dict]:
    """
    The picture's setting picks, shape-checked: an entry name, the ids of its
    lines this picture takes, and the patches this picture puts on its lines
    (a 画风 line is never picked, but it can be patched: that is how one
    picture changes the style without touching the entry). Anything else is
    dropped.
    """
    out: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("from") or "").strip()
        use = item.get("use")
        ids = tags(use) if isinstance(use, str) else [str(u).strip() for u in (use or []) if str(u).strip()]
        patches = clean_patches(item.get("patches"))
        if source and (ids or patches):
            out.append({"from": source, "use": ids, "patches": patches})
    return out


def upgrade(picture: dict | None) -> dict:
    """
    A picture in today's shape, whatever shape it was stored in.

    Idempotent on a current picture. For one written before slots had a prose
    half: each slot's string is its tags, the old `note` slot (the one slot
    that took sentences) becomes the other slot's prose, a character's look
    and moment strings are their tags. Nothing is split or moved by guessing:
    a sentence that was written into a tag slot stays where it was written.
    """
    out: dict = copy.deepcopy(picture) if isinstance(picture, dict) else {}
    raw_slots = out.get("slots")
    slots: dict = raw_slots if isinstance(raw_slots, dict) else {}
    fresh: dict = {name: field(slots.get(name)) for name in FIELD_SLOTS}
    note = slots.get("note")
    if isinstance(note, str) and note.strip():
        other = fresh["other"]
        other["prose"] = sentences([note, other["prose"]])
    text = slots.get(TEXT_SLOT)
    fresh[TEXT_SLOT] = field(text)["tags"] if isinstance(text, dict) else str(text or "").strip()
    out["slots"] = fresh
    out["settings"] = clean_settings(out.get("settings"))
    chars: list[dict] = []
    for ch in out.get("characters") or []:
        if not isinstance(ch, dict):
            continue
        ch = dict(ch)
        for name in CHAR_FIELDS:
            ch[name] = field(ch.get(name))
        ch["use"] = [str(u).strip() for u in (ch.get("use") or []) if str(u).strip()]
        ch["patches"] = clean_patches(ch.get("patches"))
        chars.append(ch)
    out["characters"] = chars
    out["negative"] = str(out.get("negative") or "").strip()
    if out.get("size") not in ("portrait", "landscape", "square"):
        out["size"] = "portrait"
    return out


def new_subject(name: str, index: int) -> dict:
    """An empty subject: `name` is its entry when it has one (and then its
    key too), otherwise it is 主体{index}, the key a nameless subject gets."""
    return {
        "key": name or f"主体{index}", "from": name, "use": [], "patches": [],
        "look": field(None), "moment": field(None), "center_x": None, "center_y": None,
    }


# --------------------------------------------------------------- patches


def clean_patches(raw) -> list[dict]:
    """The model's patches, shape-checked: a line, and at least one of set /
    add / remove. Anything else is dropped."""
    out: list[dict] = []
    for p in raw or []:
        if not isinstance(p, dict):
            continue
        line = str(p.get("line") or "").strip()
        ops = {k: str(p.get(k) or "").strip() for k in ("set", "add", "remove")}
        ops = {k: v for k, v in ops.items() if v}
        if line and ops:
            out.append({"line": line, **ops})
    return out


def patched(text: str, patches: list[dict], line: str) -> str:
    """One line as this picture has it: the entry's words, with the patches
    aimed at this line applied. `set` replaces the line; otherwise remove,
    then add."""
    out = tags(text)
    for p in patches or []:
        if p.get("line") != line:
            continue
        if p.get("set"):
            out = tags(p["set"])
            continue
        drop = {t.lower() for t in tags(p.get("remove") or "")}
        out = [t for t in out if t.lower() not in drop] + tags(p.get("add") or "")
    return join(out)


# --------------------------------------------------------------- change lists


def _merge(old: str, op, allowed: tuple[str, ...], where: str) -> str:
    if not isinstance(op, dict):
        raise PatchRejected(ui_text.PATCH_NOT_AN_OBJECT.format(where=where))
    for name in op:
        if name not in allowed:
            raise PatchRejected(
                ui_text.PATCH_OP_NOT_ALLOWED.format(where=where, op=name, allowed="/".join(allowed))
            )
    if "set" in op:
        return str(op["set"] or "")
    out = tags(old)
    for t in tags(str(op.get("remove") or "")):
        out = [x for x in out if x.lower() != t.lower()]
    out += tags(str(op.get("add") or ""))
    return join(out)


PROSE_OPS: tuple[str, ...] = ("set", "add", "remove")


def split_sentences(text: str | None) -> list[str]:
    """A run of prose as its sentences, each kept whole."""
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if s.strip()]


def _same_sentence(a: str, b: str) -> bool:
    norm = lambda s: " ".join(s.lower().rstrip(".!?").split())  # noqa: E731
    return norm(a) == norm(b)


def _merge_prose(old: str, op, where: str) -> str:
    """
    The prose half after a change. `set` rewrites it; `add` appends whole
    sentences; `remove` drops the sentences named (matched whole, case and
    final stop aside). A sentence is still never nudged a word at a time --
    but an edit that adds one cloud no longer has to copy out every sentence
    of the last picture to keep them, which is how a long chain of edits
    grew a base prompt from 34 items to 86.
    """
    if not isinstance(op, dict):
        raise PatchRejected(ui_text.PATCH_NOT_AN_OBJECT.format(where=where))
    for name in op:
        if name not in PROSE_OPS:
            raise PatchRejected(
                ui_text.PATCH_OP_NOT_ALLOWED.format(where=where, op=name, allowed="/".join(PROSE_OPS)))
    if "set" in op:
        return str(op["set"] or "")
    out = split_sentences(old)
    for gone in split_sentences(str(op.get("remove") or "")):
        out = [s for s in out if not _same_sentence(s, gone)]
    out += split_sentences(str(op.get("add") or ""))
    return sentences(out)


def _merge_field(old: dict, op, tag_ops: tuple[str, ...], where: str) -> dict:
    """
    A two-halved slot after a change: `{"tags": {...}, "prose": {...}}`, either
    half optional. The tags half takes the slot's own operations; the prose
    half is changed by whole sentences (see _merge_prose).
    """
    if not isinstance(op, dict):
        raise PatchRejected(ui_text.PATCH_NOT_AN_OBJECT.format(where=where))
    for name in op:
        if name not in ("tags", "prose"):
            raise PatchRejected(ui_text.PATCH_FIELD_HALVES.format(where=where, key=name))
    out = field(old)
    if "tags" in op:
        out["tags"] = _merge(out["tags"], op["tags"], tag_ops, f"{where}.tags").strip()
    if "prose" in op:
        out["prose"] = _merge_prose(out["prose"], op["prose"], f"{where}.prose").strip()
    return out


def apply(picture: dict, patch: dict) -> dict:
    """
    Merge a change list into a picture. Whatever the list does not mention
    stays byte for byte — the load-bearing promise of the whole design, and a
    property of this function rather than of the model's care.

    Rejects, rather than guesses at, a list whose shape the table forbids: a
    fast-changing slot given `add`, a slot that does not exist, a subject that
    is not in the picture. The caller retries the same request.
    """
    out = upgrade(picture)
    for key, op in (patch or {}).items():
        if key == "characters":
            # a list, each item naming its subject by `from`: a dict keyed by
            # name cannot be described in a JSON schema, and the model had to
            # guess at it
            for sub in (op or []):
                if not isinstance(sub, dict):
                    raise PatchRejected(ui_text.PATCH_SUBJECT_NOT_AN_OBJECT)
                who = str(sub.get("from") or "")
                ch = next((c for c in out["characters"] if c.get("key") == who), None)
                if sub.get("drop"):
                    # a subject this picture no longer has (the cat left)
                    if ch is None:
                        raise PatchRejected(ui_text.PATCH_NO_SUCH_SUBJECT.format(who=who))
                    out["characters"] = [c for c in out["characters"] if c is not ch]
                    continue
                if ch is None:
                    # a subject this picture did not have (a cat joins): named
                    # after its entry when it has one, 主体N when it has not,
                    # and filled in by the same operations as an old one
                    if not any(k in sub for k in CHAR_OPS) and "entry" not in sub:
                        raise PatchRejected(ui_text.PATCH_NO_SUCH_SUBJECT.format(who=who))
                    ch = new_subject(who, len(out["characters"]) + 1)
                    out["characters"].append(ch)
                if "entry" in sub:
                    # a subject drawn before its entry existed (the lead
                    # recorded her this turn) is bound to the entry from now
                    # on: without this its `use` named lines in no book and
                    # the costume never reached NovelAI
                    ch["from"] = str(sub.get("entry") or "").strip()
                for field_name, field_op in sub.items():
                    if field_name in ("from", "drop", "entry"):
                        continue
                    if field_name not in CHAR_OPS:
                        raise PatchRejected(
                            ui_text.PATCH_NO_SUCH_SUBJECT_SLOT.format(who=who, slot=field_name)
                        )
                    if field_name == "patches":
                        if not isinstance(field_op, dict) or set(field_op) - {"set"}:
                            raise PatchRejected(ui_text.PATCH_PATCHES_SET_ONLY.format(who=who))
                        ch["patches"] = clean_patches(field_op.get("set"))
                    elif field_name == "use":
                        ch["use"] = tags(_merge(", ".join(ch.get("use") or []), field_op,
                                               CHAR_OPS["use"], f"{who}.use"))
                    else:
                        ch[field_name] = _merge_field(ch.get(field_name) or {}, field_op,
                                                      CHAR_OPS[field_name], f"{who}.{field_name}")
            continue
        if key == "settings":
            if not isinstance(op, dict) or set(op) - {"set"}:
                raise PatchRejected(ui_text.PATCH_SETTINGS_SET_ONLY)
            out["settings"] = clean_settings(op.get("set"))
            continue
        if key == "size":
            value = op.get("set") if isinstance(op, dict) else op
            if value in ("portrait", "landscape", "square"):
                out["size"] = value
            continue
        if key == "negative":
            out["negative"] = _merge(out.get("negative") or "", op, NEGATIVE_OPS, "negative")
            continue
        if key == TEXT_SLOT:
            out["slots"][TEXT_SLOT] = _merge(out["slots"].get(TEXT_SLOT, ""), op, SET_ONLY, key).strip()
            continue
        slot = BY_NAME.get(key)
        if slot is None:
            raise PatchRejected(ui_text.PATCH_NO_SUCH_SLOT.format(slot=key))
        out["slots"][key] = _merge_field(out["slots"].get(key), op, slot.ops, key)
    return out


# --------------------------------------------------------------- render / check


def render(
    picture: dict,
    lines: dict[str, dict[str, str]] | None = None,
    setting_lines: dict[str, dict[str, str]] | None = None,
    style_lines: dict[str, dict[str, str]] | None = None,
    complete: bool = False,
) -> dict:
    """
    Slots in, the three things NovelAI actually receives out.

    `lines` is {entry name: {line id: tags}}; a character's `use` is looked up
    in it and each line arrives with this picture's patches applied. A line
    is a comma-separated list: it is cut at its commas, each item goes out
    without a trailing stop, and an item already written is not written
    again (invariant 4). A sentence inside a line therefore must not contain
    a comma -- tag_lines.txt says so.
    `setting_lines` is the same for settings; the picture's `settings` picks
    are spliced in right after the style slot. `style_lines` is the settings'
    style lines alone: those go into the style slot for every picture whether
    picked or not -- the library's words, taken from the entry every time and
    never from the model's memory of them -- patched as far as this picture's
    setting patches say. What the model wrote in the style slot follows them;
    a word of the entry's it repeated there is the entry's, not its own, so a
    picture stored before the lines were spliced here reads the same.

    Order: every slot's tags, in slot order, then every slot's prose in the
    same order, then the text to render. A character: its lines as the entry
    wrote them (sentences and all -- a line is not re-cut), its look and
    moment tags, then its look and moment prose.

    The returned shape is `llm.PROMPT_FIELDS` — the same dict that is stored on
    the row and replayed verbatim by 重绘.
    """
    picture = upgrade(picture)
    lines = lines or {}
    setting_lines = setting_lines or {}
    style_lines = style_lines or {}
    slots = picture["slots"]
    picks = {p["from"]: p for p in picture["settings"]}
    style_ids = {(title, k) for title, book in style_lines.items() for k in book}
    entry_words = {t.lower() for book in style_lines.values() for line in book.values() for t in tags(line)}

    ordered: list[str] = []
    prose: list[str] = []
    for name in FIELD_SLOTS:
        half = slots[name]
        if name == "style":
            for title, book in style_lines.items():
                patches = (picks.get(title) or {}).get("patches") or []
                for k, line in book.items():
                    ordered += tags(patched(line, patches, k))
            ordered += [t for t in tags(half["tags"]) if t.lower() not in entry_words]
            # a setting's picked lines go out beside the style lines: they
            # are the library's words, like a character's, not this picture's
            for pick in picture["settings"]:
                book = setting_lines.get(pick["from"], {})
                for k in pick["use"]:
                    if k in book and (pick["from"], k) not in style_ids:
                        ordered += tags(patched(book[k], pick.get("patches") or [], k))
        else:
            ordered += tags(half["tags"])
        if half["prose"]:
            prose.append(half["prose"])

    text = (slots.get(TEXT_SLOT) or "").strip()
    if complete and (ordered or prose or text):
        # NovelAI's own words, in their place (invariant 8) -- and only behind
        # words of the model's own: quality words alone are not a picture (7)
        ordered += tags(novelai.quality_words(has_text=bool(text)))
    parts = [join(ordered)]
    if prose:
        parts.append(sentences(prose))
    if text:
        parts.append(text)              # NovelAI wants rendered text last
    base = glue(parts)

    chars = []
    for ch in picture["characters"]:
        book = lines.get(ch.get("from") or "", {})
        patches = ch.get("patches") or []
        words: list[str] = []
        for k in ch.get("use") or []:
            if k in book:
                words += tags(patched(book[k], patches, k))
        words += tags(ch["look"]["tags"]) + tags(ch["moment"]["tags"])
        written = glue([join(words), sentences([ch["look"]["prose"], ch["moment"]["prose"]])])
        if written:
            chars.append({
                "key": ch.get("key") or ch.get("from") or "",
                "prompt": written,
                "center_x": ch.get("center_x"),
                "center_y": ch.get("center_y"),
            })

    return {
        "n_images": 1,
        "size": picture.get("size") or "portrait",
        "base_prompt": base,
        "character_prompts": chars,
        "negative_prompt": (novelai.merge_negative(picture.get("negative") or "") if complete
                            else (picture.get("negative") or "").strip()),
    }


#: Words that do not say WHAT a thing is: colours, sides, body parts, filler.
#: Used to tell whether a `look` tag re-describes a line that was picked.
_NOT_THINGS = {
    "black", "white", "grey", "gray", "red", "blue", "green", "yellow", "brown", "beige",
    "pink", "purple", "orange", "silver", "gold", "golden", "dark", "light", "pale", "deep",
    "left", "right", "both", "on", "over", "with", "and", "the", "a", "of", "in", "under",
    "small", "large", "long", "short", "thin", "thick", "single", "two",
    "hair", "eye", "eyes", "ear", "ears", "hand", "hands", "head", "face", "neck", "arm",
    "arms", "finger", "fingers", "wrist", "leg", "legs", "foot", "feet", "lip", "lips",
}


def _things(tag: str) -> set[str]:
    words = {w.strip("()").lower() for w in tag.replace("-", " ").split()}
    return {w for w in words if len(w) >= 3 and w not in _NOT_THINGS}


def check(
    picture: dict,
    lines: dict[str, dict[str, str]] | None = None,
    parts: dict[str, dict[str, str]] | None = None,
    setting_lines: dict[str, dict[str, str]] | None = None,
) -> list[str]:
    """
    What looks wrong with this picture. Reported, never enforced.

    Only slot-level problems: whether a tag exists on Danbooru is asked and
    answered by `llm.unknown_tags`, which already runs over the stored prompts
    and is shown in the same dialog.
    """
    picture = upgrade(picture)
    lines = lines or {}
    parts = parts or {}
    setting_lines = setting_lines or {}
    notes: list[str] = []

    for ch in picture["characters"]:
        who = ch.get("from") or ""
        key = ch.get("key") or who or ui_text.PROMPT_NAMELESS
        book = lines.get(who, {})
        use = ch.get("use") or []
        missing = [k for k in use if k not in book]
        if missing:
            notes.append(ui_text.PROMPT_UNKNOWN_LINES.format(
                who=who or ui_text.PROMPT_NAMELESS, lines="、".join(missing)))
        if who and parts:
            idents = [k for k, part in (parts.get(who) or {}).items()
                      if part == wording.LINE_PART_IDENTITY]
            if idents and not any(k in use for k in idents):
                notes.append(ui_text.PROMPT_IDENTITY_LINE_MISSING.format(
                    who=who, lines="、".join(idents)))
        patches = ch.get("patches") or []
        stray = [p["line"] for p in patches if p.get("line") not in use]
        if stray:
            notes.append(ui_text.PROMPT_PATCH_ON_UNPICKED_LINE.format(
                who=key, lines="、".join(stray)))
        # A library character may carry a `look` of its own: it is where what
        # this picture adds and no line has (a borrowed coat, the top under a
        # sweater that came off) is written. What it must not do is say again
        # what a picked line already says, which the note below is for.
        picked = {w for k in use if k in book
                  for t in tags(patched(book[k], patches, k)) for w in _things(t)}
        echoed = [t for t in tags(ch["look"]["tags"]) if _things(t) & picked]
        if echoed:
            notes.append(ui_text.PROMPT_LOOK_ECHOES_LINES.format(who=key, tags="、".join(echoed)))

    # A library line is English tags (tag_lines.txt); the lead now and then
    # writes a Chinese description into a 画风 line, and style lines go into
    # every picture without being picked -- so the fault is named here, by
    # line, before lint names it in the prompt.
    for who, book in list(lines.items()) + list(setting_lines.items()):
        for lid, text in book.items():
            if _FOREIGN.search(text or ""):
                notes.append(ui_text.PROMPT_LINE_NOT_ENGLISH.format(who=who, line=lid))

    for pick in picture["settings"]:
        book = setting_lines.get(pick["from"], {})
        missing = [k for k in pick["use"] if k not in book]
        missing += [p["line"] for p in pick.get("patches") or [] if p.get("line") not in book]
        if missing:
            notes.append(ui_text.PROMPT_UNKNOWN_SETTING_LINES.format(
                title=pick["from"], lines="、".join(dict.fromkeys(missing))))

    count = picture["slots"]["count"]["tags"].lower()
    if "solo" in count and len(picture["characters"]) > 1:
        notes.append(ui_text.PROMPT_SOLO_WITH_OTHERS)
    # Two people in a picture are two places in it. Without coordinates
    # NovelAI arranges them itself and the order the brief gave is a wish:
    # ten girls at a long table came out as eight with two swapped.
    if len(picture["characters"]) >= 2:
        unplaced = [ch.get("key") or ch.get("from") or ui_text.PROMPT_NAMELESS
                    for ch in picture["characters"] if ch.get("center_x") is None or ch.get("center_y") is None]
        if unplaced:
            notes.append(ui_text.PROMPT_MULTI_WITHOUT_CENTER.format(who="、".join(str(w) for w in unplaced)))

    # A sentence in a tags half is the model breaking the tags/prose split.
    # join sends it without its stop, so NovelAI no longer sees 「., 」 -- but
    # the split is the design, and a report that cannot see it broken cannot
    # say whether the model keeps it.
    for name in FIELD_SLOTS:
        for item in tags(picture["slots"][name]["tags"]):
            if item[-1:] in ".!?":
                notes.append(ui_text.PROMPT_SENTENCE_IN_TAGS.format(where=name, text=item[:60]))
    for ch in picture["characters"]:
        key = ch.get("key") or ch.get("from") or ui_text.PROMPT_NAMELESS
        for part in CHAR_FIELDS:
            for item in tags(ch[part]["tags"]):
                if item[-1:] in ".!?":
                    notes.append(ui_text.PROMPT_SENTENCE_IN_TAGS.format(where=f"{key}的{part}", text=item[:60]))

    return notes


def from_model(parsed: dict, brief: dict) -> dict:
    """The drawing model's JSON, as a picture. Shapes only; no judgement."""
    picture = empty()

    size = parsed.get("size")
    picture["size"] = (
        size if size in ("portrait", "landscape", "square")
        else brief.get("size") or "portrait"
    )

    for name in FIELD_SLOTS:
        picture["slots"][name] = field(parsed.get(name))
    picture["slots"][TEXT_SLOT] = field(parsed.get(TEXT_SLOT))["tags"]
    picture["settings"] = clean_settings(parsed.get("settings"))

    # No cap. `_build_payload` sends as many char_captions as it is given and
    # NovelAI's own client takes more than a dozen; the six that used to be
    # here was the app's invention, and it dropped the seventh figure of a
    # group scene without a word. If there is a ceiling it is NovelAI's to
    # state, and a refusal from it is a real error worth seeing.
    for i, item in enumerate(parsed.get("characters") or [], 1):
        if not isinstance(item, dict):
            continue
        source = str(item.get("from") or "").strip()
        look = field(item.get("look"))
        moment = field(item.get("moment"))
        use = [u for u in tags(str(item.get("use") or ""))]
        if not (look["tags"] or look["prose"] or moment["tags"] or moment["prose"] or use or source):
            continue
        cx, cy = item.get("center_x"), item.get("center_y")
        if isinstance(cx, (int, float)) and isinstance(cy, (int, float)):
            cx = round(min(max(float(cx), 0.0), 1.0), 3)
            cy = round(min(max(float(cy), 0.0), 1.0), 3)
        else:
            cx = cy = None
        picture["characters"].append({
            # the handle is the app's: the entry's name, or a number
            "key": source or wording.SUBJECT_KEY.format(n=i),
            "from": source,
            "use": use,
            "patches": clean_patches(item.get("patches")),
            "look": look,
            "moment": moment,
            "center_x": cx,
            "center_y": cy,
        })

    picture["negative"] = str(parsed.get("negative_prompt") or "").strip()
    return picture
