"""
Assembling what NovelAI gets out of the exposed entries and the drawing
model's answer.

The drawing model does not write the prompt. It says, for each exposed
entry, whether it is in this picture and what this picture changes about it
(tags added or taken away, sentences added or dropped), and adds this picture's own
tags and prose -- the count, the camera, the scene, the light, what everyone
is doing. This module puts those together the one way NovelAI wants them:

  base      this picture's tags, then every used setting's tags (the style
            ones first), then the quality words; then the settings' prose
            and this picture's prose; then `Text: ...` last.
  character the character entry's tags, then its exposed variant's; then
            their prose. A negative of its own from the same entries.
  negative  the settings' negatives, this picture's, and the app's default.

A tag written twice in whatever dressing goes out once (novelai.tag_key).
Sentences are set apart from tags by a space, never 「., 」.

What was sent is the only record (`nai_prompts`); beside it the turn keeps
the exposed entries, the answer and what each entry came to, for the
inspector to show why a picture reads the way it does.
"""
from __future__ import annotations

import re

from core.services import danbooru, novelai
from core.services.library import split_tags

#: what a setting's category must say for its tags to lead the prompt
STYLE_CATEGORIES = {"画风", "style"}
TEXT_PREFIX = "Text: "

_FOREIGN = re.compile("[぀-ヿ㐀-䶿一-鿿가-힯]")
_SEPARATOR_FAULTS = (
    (re.compile(r"[.!?]\s*,"), "a comma after a stop"),
    (re.compile(r",\s*[.!?]"), "a stop after a comma"),
    (re.compile(r",\s*,"), "an empty item between commas"),
    (re.compile(r"^\s*[,.]|[,]\s*$"), "a separator at an end"),
    (re.compile(r"\s{2,}"), "a doubled space"),
    (re.compile(r"\s,"), "a space before a comma"),
)


# --------------------------------------------------------------- the answer
def _str(v) -> str:
    return str(v or "").strip()


def clean_answer(parsed: object) -> dict:
    """The drawing model's answer, shape-checked: {entries: {key: change},
    positive, negative, prose, subjects, size}. A change is {use,
    add_positive, remove_positive, add_negative, remove_negative, add_prose,
    remove_prose}."""
    if not isinstance(parsed, dict):
        parsed = {}
    entries: dict[str, dict] = {}
    for item in parsed.get("entries") or []:
        if not isinstance(item, dict):
            continue
        key = _str(item.get("key"))
        if not key:
            continue
        use = item.get("use")
        entries[key] = {
            "use": True if use is None else bool(use),
            "add_positive": split_tags(item.get("add_positive")),
            "remove_positive": split_tags(item.get("remove_positive")),
            "add_negative": split_tags(item.get("add_negative")),
            "remove_negative": split_tags(item.get("remove_negative")),
            "add_prose": _str(item.get("add_prose")),
            "remove_prose": _removed_sentences(item.get("remove_prose")),
        }
    subjects = []
    for item in parsed.get("subjects") or []:
        if not isinstance(item, dict) or not _str(item.get("key")):
            continue
        subjects.append({"key": _str(item.get("key")), "center_x": _coord(item.get("center_x")),
                         "center_y": _coord(item.get("center_y"))})
    size = parsed.get("size")
    return {
        "entries": entries,
        "positive": split_tags(parsed.get("positive")),
        "negative": split_tags(parsed.get("negative")),
        "prose": _str(parsed.get("prose")),
        "subjects": subjects,
        "size": size if size in ("portrait", "landscape", "square") else "",
    }


def _coord(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return min(max(f, 0.0), 1.0)


def _removed_sentences(value) -> list[str] | bool:
    """The sentences a picture takes out of an entry's description, one by
    one. An answer in the earlier shape says true or false: all of them, or
    none."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [s for v in value if isinstance(v, str) for s in split_sentences(v)]


# --------------------------------------------------------------- pieces
def _apply(base: list[str], change: dict | None, which: str) -> list[str]:
    """An entry's tag list with this picture's removals and additions."""
    if not change:
        return split_tags(base)
    gone = {novelai.tag_key(t) for t in change.get(f"remove_{which}") or []} - {""}
    kept: list[str] = []
    for t in base:
        if novelai.tag_key(t) in gone:
            # the tag goes, the weight it opens or closes stays
            rest = novelai.weight_markers(t)
            if rest:
                kept.append(rest)
        else:
            kept.append(t)
    return split_tags(kept + list(change.get(f"add_{which}") or []))


def _sentence_key(sentence: str) -> str:
    """A sentence as compared: letters, digits and single spaces."""
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", sentence.lower()).split())


def split_sentences(text: str) -> list[str]:
    """A run of prose as its sentences, each ending with a stop."""
    text = " ".join((text or "").split()).strip(" ,;")
    if not text:
        return []
    if text[-1] not in ".!?":
        text += "."
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if _sentence_key(s)]


def sentences(pieces) -> str:
    """Prose pieces as one run of sentences, each ending with a stop, a
    sentence said twice going out once."""
    out: list[str] = []
    seen: set[str] = set()
    for piece in pieces:
        for sentence in split_sentences(piece or ""):
            key = _sentence_key(sentence)
            if key in seen:
                continue
            seen.add(key)
            out.append(sentence)
    return " ".join(out)


def _prose_for(own: str, change: dict | None) -> str:
    """An entry's description in this picture: its own sentences less the
    ones this picture takes out, then the sentences it adds. A sentence is
    taken out when the one named matches it, or names a long enough piece
    of it -- the model copies, and copies are not always whole."""
    if not change:
        return sentences([own])
    remove = change.get("remove_prose")
    if remove is True:
        kept: list[str] = []
    else:
        named = [_sentence_key(s) for s in remove or []]
        named = [k for k in named if k]

        def gone(sentence: str) -> bool:
            key = _sentence_key(sentence)
            return any(k == key or (len(k) >= 15 and k in key) for k in named)

        kept = [s for s in split_sentences(own) if not gone(s)]
    return sentences(kept + [change.get("add_prose") or ""])


def glue(parts) -> str:
    """Tags and prose in one string: a comma between tags, a space after a
    sentence -- never 「., 」."""
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


def _is_style(e: dict) -> bool:
    return str(e.get("category") or "").strip().lower() in STYLE_CATEGORIES


# --------------------------------------------------------------- assembly
def assemble(exposed: list[dict], answer: dict, *, text: str = "", size_hint: str = "") -> dict:
    """
    What goes to NovelAI, and the record of how it was made.

    Returns {nai_prompts, assembly}: `nai_prompts` has size, base_prompt,
    character_prompts (key, prompt, negative, center_x, center_y) and
    negative_prompt; `assembly` lists every exposed entry with whether it
    was used and the tags and prose it came to.
    """
    changes = answer.get("entries") or {}
    record: list[dict] = []
    settings: list[dict] = []
    chars: dict[str, dict] = {}        # character key -> {entry, variants, final}
    for e in exposed:
        change = changes.get(e["key"]) or changes.get(e.get("shown_key") or "")
        used = change["use"] if change else True
        final = {
            "key": e["key"], "kind": e["kind"], "used": used,
            "positive": _apply(e.get("positive") or [], change, "positive"),
            "negative": _apply(e.get("negative") or [], change, "negative"),
            # the entry's own sentences less the ones this picture takes
            # out, then this picture's additions -- a sentence that still
            # holds is never lost to a pose sentence
            "prose": _prose_for(e.get("prose") or "", change),
        }
        record.append(final)
        if not used:
            continue
        if e["kind"] == "setting":
            settings.append({**final, "category": e.get("category") or ""})
        elif e["kind"] == "character":
            chars.setdefault(e["key"], {"main": None, "variants": []})["main"] = final
        else:
            chars.setdefault(e.get("character") or "", {"main": None, "variants": []})["variants"].append(final)
    settings.sort(key=lambda s: 0 if _is_style(s) else 1)

    base_tags = split_tags(list(answer.get("positive") or []) + [t for s in settings for t in s["positive"]])
    quality = [w.strip() for w in novelai.QUALITY_WORDS.split(",") if w.strip()]
    if not text:
        quality.append(novelai.NO_TEXT_WORD)
    base_tags = split_tags(base_tags + quality)
    base_prose = sentences([s["prose"] for s in settings] + [answer.get("prose") or ""])
    base = glue([", ".join(base_tags), base_prose])
    if text:
        base = glue([base, TEXT_PREFIX + text.strip()])

    negative_own = ", ".join(split_tags(
        [t for s in settings for t in s["negative"]] + list(answer.get("negative") or [])))

    order = [s["key"] for s in answer.get("subjects") or []]
    centers = {s["key"]: s for s in answer.get("subjects") or []}
    keys = [k for k in order if k in chars and chars[k]["main"]] + \
           [k for k in chars if k not in order and chars[k]["main"]]
    character_prompts = []
    for k in keys:
        group = chars[k]
        main = group["main"]
        tags = split_tags(main["positive"] + [t for v in group["variants"] for t in v["positive"]])
        prose = sentences([main["prose"]] + [v["prose"] for v in group["variants"]])
        neg = ", ".join(split_tags(main["negative"] + [t for v in group["variants"] for t in v["negative"]]))
        at = centers.get(k) or {}
        character_prompts.append({
            "key": k, "prompt": glue([", ".join(tags), prose]), "negative": neg,
            "center_x": at.get("center_x"), "center_y": at.get("center_y"),
        })
    negative = novelai.merge_negative(negative_own, ", ".join([base] + [c["prompt"] for c in character_prompts]))
    size = answer.get("size") or size_hint or "portrait"
    if size not in ("portrait", "landscape", "square"):
        size = "portrait"
    return {
        "nai_prompts": {"size": size, "base_prompt": base, "character_prompts": character_prompts,
                        "negative_prompt": negative},
        "assembly": record,
    }


# --------------------------------------------------------------- checks
def foreign(nai_prompts: dict) -> list[str]:
    """Which prompts carry a script NovelAI's tags are not: the drawing
    model now and then answers a Chinese brief in Chinese. The text after
    `Text:` is allowed to be anything."""
    out: list[str] = []
    base = str(nai_prompts.get("base_prompt") or "")
    base = base.split(TEXT_PREFIX, 1)[0]
    if _FOREIGN.search(base):
        out.append("base")
    for i, c in enumerate(nai_prompts.get("character_prompts") or [], 1):
        if _FOREIGN.search(str(c.get("prompt") or "")) or _FOREIGN.search(str(c.get("negative") or "")):
            out.append(f"character {c.get('key') or i}")
    if _FOREIGN.search(str(nai_prompts.get("negative_prompt") or "")):
        out.append("negative")
    return out


def lint(nai_prompts: dict) -> list[str]:
    """What a prompt breaks mechanically: separators, emptiness, script.
    One line per fault; empty when the prompt is as promised."""
    faults: list[str] = []
    pieces = [("base", str(nai_prompts.get("base_prompt") or "").split(TEXT_PREFIX, 1)[0])]
    pieces += [(f"character {c.get('key') or i}", str(c.get("prompt") or ""))
               for i, c in enumerate(nai_prompts.get("character_prompts") or [], 1)]
    pieces.append(("negative", str(nai_prompts.get("negative_prompt") or "")))
    for where, prompt in pieces:
        if where == "base" and not prompt.strip():
            faults.append("base: empty")
        if where.startswith("character") and not prompt.strip():
            faults.append(f"{where}: empty")
        for pattern, what in _SEPARATOR_FAULTS:
            if pattern.search(prompt):
                faults.append(f"{where}: {what}")
        if _FOREIGN.search(prompt):
            faults.append(f"{where}: not English")
    return faults


def unknown_tags(nai_prompts, assembly=None) -> list[str]:
    """
    Every tag in one stored prompt dict that the site does not have.
    `assembly` is the turn's record, when the row has it: a sentence is not
    a tag, and a sentence with a comma in it would otherwise be reported
    piece by piece. Reported, never enforced.
    """
    if not isinstance(nai_prompts, dict):
        return []
    prose = [" ".join(str(r.get("prose") or "").lower().split()) for r in (assembly or []) if isinstance(r, dict)]
    found: list[str] = [str(nai_prompts.get("base_prompt") or "").split(TEXT_PREFIX, 1)[0]]
    found += [str(c.get("prompt") or "") for c in nai_prompts.get("character_prompts") or [] if isinstance(c, dict)]
    found.append(str(nai_prompts.get("negative_prompt") or ""))
    tags: list[str] = []
    for t in found:
        for tag in novelai.tags_to_check(t):
            piece = tag.rstrip(".!?").strip().lower()
            if len(piece.split()) > 6 or any(piece in s for s in prose):
                continue
            tags.append(tag)
    return danbooru.unknown(tags)


def same_words(a: dict | None, b: dict | None) -> bool:
    """Whether two prompt dicts would send NovelAI the same words."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False

    def words(d: dict) -> tuple:
        return (
            str(d.get("base_prompt") or "").strip(),
            tuple((str(c.get("prompt") or "").strip(), str(c.get("negative") or "").strip())
                  for c in d.get("character_prompts") or [] if isinstance(c, dict)),
            str(d.get("negative_prompt") or "").strip(),
        )
    return words(a) == words(b)


def can_paint(nai_prompts) -> bool:
    return bool(isinstance(nai_prompts, dict) and str(nai_prompts.get("base_prompt") or "").strip())
