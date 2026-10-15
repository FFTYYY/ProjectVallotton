"""
The two model jobs this app has, and what each one must return.

`lead_turn`   the model the user talks to. Keeps the persona, decides whether
              a picture should be made and whether it is a new one or a change
              to one already drawn, and says what is particular to THIS
              picture — who is in it, doing what, where, under what light.
              It refers to library characters by name and never describes
              their looks: those are the entry's tag lines. It may propose
              library edits, which the user approves before anything is
              written.

`draw_turn`   a second, context-free call that turns that into NovelAI
              prompts: slots for the picture, and for each subject the lines
              it picks from the entry, the patches this picture puts on them,
              and what the subject is doing.

`draw_edit`   the same model handed a picture already drawn, returning only a
              change list; `prompt.apply` merges it so that nothing it did not
              mention changes.

Both models may consult the Danbooru tag table. Not through the vendor's
tool-call mechanism — that road lost whole turns to calls the server could
not parse — but in the answer itself: a `lookup` field that, when it is not
empty, makes the answer a query. The app runs it, hands the result back as the
next message, and asks again. One shape, one parser, one retry policy.

Splitting the jobs keeps two hard things apart: holding a conversation, and
writing dense tag soup. Neither prompt has to hedge for the other's job.
System prompts live in core/prompts/*.txt.

Which vendor answers is not this module's business — see
core/services/providers/. Everything here is the same whoever is behind it.
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Callable

from django.conf import settings

from core import prompts
from core.prompts import wording
from core import ui_text

from . import danbooru, novelai, prompt as prompt_slots, providers
from .providers import LLMError
from .spend import record_llm_spend

log = logging.getLogger(__name__)

#: kept so `except grok.ModelError` sites and old imports still read
ModelError = LLMError


# --------------------------------------------------------------------------
# schemas
# --------------------------------------------------------------------------

_STR = {"type": "string"}
_NUM_OR_NULL = {"anyOf": [{"type": "number"}, {"type": "null"}]}
#: Words to look up in the tag table. Present on every schema but the last
#: round's: an answer that fills it is a question, not an answer.
_LOOKUP = {"type": "array", "items": _STR}

#: A tag line as the lead proposes it: what the line is, and the tags.
_TAGS = {"type": "array", "items": {
    "type": "object", "additionalProperties": False,
    "required": ["part", "tags"],
    "properties": {"part": _STR, "tags": _STR},
}}
#: "Copy the words from that picture": the drawing's id and the subject's key.
#: With this, the lead only says how to split the lines; every word comes from
#: the picture the user approved rather than from the lead's memory of it.
_COPY_FROM = {"anyOf": [{"type": "null"}, {
    "type": "object", "additionalProperties": False,
    "required": ["picture"],
    "properties": {"picture": _STR, "subject": _STR},
}]}
#: Which layer an entry goes to: the project's folder, or this conversation.
_SCOPE = {"type": "string", "enum": ["project", "conversation"]}

_TITLED_ENTRY = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "content"],
    "properties": {"title": _STR, "content": _STR, "scope": _SCOPE,
                   "tags": _TAGS, "copy_from": _COPY_FROM},
}
#: An outfit. Its identity is its title, as it always was.
_VARIANT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "content"],
    "properties": {"title": _STR, "content": _STR, "tags": _TAGS, "copy_from": _COPY_FROM},
}

#: One subject of a new picture. A library character is named and not
#: described; a known character (one NovelAI has a tag for) is named; anyone
#: else gets a full `look`.
SUBJECT_FIELDS = ("who", "outfit", "look", "changes", "doing", "where")
_SUBJECT = {
    "type": "object",
    "additionalProperties": False,
    "required": list(SUBJECT_FIELDS),
    "properties": {f: _STR for f in SUBJECT_FIELDS},
}
#: The picture as the lead describes it. `change` alone on an edit turn; the
#: rest on a new picture. Defined once so reflection cannot drift from it.
PICTURE_FIELDS = ("composition", "camera", "scene", "light", "style", "notes")
_DRAW_OBJECT = {
    "type": "object",
    "additionalProperties": False,
    # no n_images: how many to paint is the app's call, taken from the 出图数量
    # control, and asking the model for a number it cannot see the setting for
    # only invites it to contradict the user
    "required": ["change", "subjects", *PICTURE_FIELDS, "size"],
    "properties": {
        "change": _STR,
        "subjects": {"type": "array", "items": _SUBJECT},
        **{f: _STR for f in PICTURE_FIELDS},
        "size": {"type": "string", "enum": ["portrait", "landscape", "square", ""]},
    },
}

_NULLABLE_DRAW = {"anyOf": [{"type": "null"}, _DRAW_OBJECT]}

LEAD_SCHEMA = {
    "name": "vallotton_turn",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["reply", "draw", "edits"],
        "properties": {
            "lookup": _LOOKUP,
            "reply": _STR,
            # the drawing this turn changes, by id; empty = a new picture
            "continues": _STR,
            # null = this turn draws nothing
            "draw": _NULLABLE_DRAW,
            # null = propose no library changes (the common case)
            "edits": {
                "anyOf": [
                    {"type": "null"},
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["settings", "characters"],
                        "properties": {
                            "settings": {"type": "array", "items": _TITLED_ENTRY},
                            "characters": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["name", "description", "variants"],
                                    "properties": {
                                        "name": _STR,
                                        "description": _STR,
                                        "scope": _SCOPE,
                                        "tags": _TAGS,
                                        "copy_from": _COPY_FROM,
                                        "variants": {"type": "array", "items": _VARIANT},
                                    },
                                },
                            },
                        },
                    },
                ]
            },
        },
    },
}

#: A patch on one picked line: which line, and set / add / remove.
_PATCH_ITEM = {
    "type": "object",
    "additionalProperties": False,
    "required": ["line"],
    "properties": {"line": _STR, "set": _STR, "add": _STR, "remove": _STR},
}
#: One slot: the tags, and the sentences. Kept apart by structure so the app
#: never has to guess which comma-separated piece was meant as a sentence.
_FIELD = {
    "type": "object", "additionalProperties": False,
    "required": ["tags", "prose"],
    "properties": {"tags": _STR, "prose": _STR},
}
#: Which of a selected setting's lines this picture takes, by id.
_SETTING_PICK = {
    "type": "object", "additionalProperties": False,
    "required": ["from", "use"],
    "properties": {"from": _STR, "use": _STR,
                   "patches": {"type": "array", "items": _PATCH_ITEM}},
}

#: The drawing model fills in slots rather than writing one string. Which slots
#: exist, and what each may hold, is `prompt_slots.SLOTS` — named there once and
#: read here, so the schema cannot drift from the table that assembles them.
DRAW_SCHEMA = {
    "name": "novelai_prompts",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        # n_images is likewise absent here — see the note in LEAD_SCHEMA
        "required": [
            "size", *prompt_slots.FIELD_SLOTS, prompt_slots.TEXT_SLOT, "settings",
            "characters", "negative_prompt",
        ],
        "properties": {
            "lookup": _LOOKUP,
            "size": {"type": "string", "enum": ["portrait", "landscape", "square"]},
            **{name: _FIELD for name in prompt_slots.FIELD_SLOTS},
            prompt_slots.TEXT_SLOT: _STR,
            "settings": {"type": "array", "items": _SETTING_PICK},
            "characters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["from", "use", "patches", "look", "moment",
                                 "center_x", "center_y"],
                    "properties": {
                        "from": _STR,
                        "use": _STR,
                        "patches": {"type": "array", "items": _PATCH_ITEM},
                        "look": _FIELD,
                        "moment": _FIELD,
                        "center_x": _NUM_OR_NULL,
                        "center_y": _NUM_OR_NULL,
                    },
                },
            },
            "negative_prompt": _STR,
        },
    },
}

_OP = {
    "type": "object",
    "additionalProperties": False,
    "required": [],
    "properties": {"set": _STR, "add": _STR, "remove": _STR},
}
_SET_ONLY = {
    "type": "object",
    "additionalProperties": False,
    "required": ["set"],
    "properties": {"set": _STR},
}
#: A change to a two-halved slot: the tags half nudged or rewritten, the
#: prose half rewritten or changed by whole sentences. Either half may be
#: left out.
_FIELD_OP = {
    "type": "object", "additionalProperties": False, "required": [],
    "properties": {"tags": _OP, "prose": _OP},
}
#: A change list. Which slots take which operations is `prompt_slots`'s table;
#: the schema only says what the shape is, and `prompt.apply` refuses what the
#: table forbids.
PATCH_SCHEMA = {
    "name": "novelai_patch",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [],
        "properties": {
            "lookup": _LOOKUP,
            **{name: _FIELD_OP for name in prompt_slots.FIELD_SLOTS},
            prompt_slots.TEXT_SLOT: _SET_ONLY,
            "negative": _OP,
            "settings": {
                "type": "object", "additionalProperties": False,
                "required": ["set"],
                "properties": {"set": {"type": "array", "items": _SETTING_PICK}},
            },
            "characters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["from"],
                    "properties": {
                        "from": _STR,
                        "entry": _STR,
                        "drop": {"type": "boolean"},
                        "use": _SET_ONLY,
                        "look": _FIELD_OP,
                        "moment": _FIELD_OP,
                        "patches": {
                            "type": "object", "additionalProperties": False,
                            "required": ["set"],
                            "properties": {"set": {"type": "array", "items": _PATCH_ITEM}},
                        },
                    },
                },
            },
        },
    },
}

#: One look at what came back. Reflection is the same model, under the same
#: system prompt, being shown what came back — so it answers in the SAME
#: shape. A null `draw` IS "I am satisfied"; a `draw` with a `change` is what
#: to alter, and the redraw is an edit of the picture it is looking at.
REFLECT_SCHEMA = {**LEAD_SCHEMA, "name": "vallotton_reflection"}

#: the line writer's answer: an entry's full list of lines (services/library_lines.py)
LINES_SCHEMA = {
    "name": "library_lines",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["lines"],
        "properties": {
            "lines": {
                "type": "array",
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["part", "tags"],
                    "properties": {"part": _STR, "tags": _STR},
                },
            },
            "lookup": _LOOKUP,
        },
    },
}


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------

#: Shown to the USER, never to a model, so it does not belong in wording.py —
#: that file is "every word the models read", and this is the one thing in the
#: transcript the models never see. It is narrow: an empty HTTP response is
#: retried and then raised, so it never gets this far. This is for the model
#: answering with well-formed JSON that says nothing at all —
#: {"reply": "", "draw": null} — which leaves a row with no text and no picture.
EMPTY_REPLY_FALLBACK = ui_text.EMPTY_REPLY_FALLBACK


def _run(
    system: str,
    messages: list[dict],
    schema: dict,
    effort: str | None,
    on_retry: Callable[[str], None] | None = None,
    temperature: float | None = None,
) -> tuple[dict, dict]:
    """One structured call against the configured provider, billed."""
    result = providers.active().complete(
        system=system,
        messages=messages,
        schema=schema,
        timeout=settings.LLM_TIMEOUT_SECONDS,
        effort=effort,
        temperature=temperature,
        on_retry=on_retry,
    )
    # a provider that does not price its responses still counts the request;
    # the quota panel shows its balance rather than a sum of zeroes
    record_llm_spend(result.cost_ticks or 0)
    usage = dict(result.usage)
    if result.reasoning:
        # not in usage — that block is counters. The working goes where the
        # turn is stored, so 「检查」 can show it long after the call.
        usage["_reasoning"] = result.reasoning
    if result.retries:
        # rides along in usage so it lands in model_raw and the 检查 dialog can
        # still say so after the live notice is gone
        usage["retries"] = result.retries
    return result.parsed, usage


def _without_lookup(schema: dict) -> dict:
    out = copy.deepcopy(schema)
    out["schema"]["properties"].pop("lookup", None)
    return out


def _queries(parsed) -> list[str]:
    raw = parsed.get("lookup") if isinstance(parsed, dict) else None
    if isinstance(raw, str):
        raw = [raw]
    return [str(q).strip() for q in (raw or []) if str(q).strip()]


def _ask(
    system: str,
    messages: list[dict],
    schema: dict,
    *,
    rounds: int,
    effort: str | None,
    on_retry: Callable[[str], None] | None = None,
    on_lookup: Callable[[int], None] | None = None,
    temperature: float | None = None,
) -> tuple[dict, dict]:
    """
    Ask until answered, looking tags up in between.

    `rounds` is how many lookups may happen. Every request is the same shape
    and goes through the same parser; the last allowed round's schema simply
    has no `lookup` field, so the model can only answer. What was looked up
    rides back in usage under `_lookups`, for 「检查」.
    """
    history = list(messages)
    lookups: list[dict] = []
    usage: dict = {}
    retries = 0
    for i in range(rounds + 1):
        last = i == rounds
        parsed, usage = _run(
            system, history, _without_lookup(schema) if last else schema,
            effort=effort, on_retry=on_retry, temperature=temperature,
        )
        retries += int(usage.get("retries") or 0)
        queries = [] if last else _queries(parsed)
        if not queries:
            if isinstance(parsed, dict):
                parsed = {k: v for k, v in parsed.items() if k != "lookup"}
            if retries:
                usage["retries"] = retries
            if lookups:
                usage["_lookups"] = lookups
            return parsed, usage
        if on_lookup:
            on_lookup(len(queries))
        result = danbooru.answer({"queries": queries})
        lookups.append({"queries": queries, "result": result})
        history.append({"role": "assistant",
                        "content": json.dumps({"lookup": queries}, ensure_ascii=False)})
        note = wording.LOOKUP_HEADING + "\n" + result
        if i + 1 == rounds:
            note += "\n" + wording.LOOKUP_LAST
        history.append({"role": "user", "content": note})
    raise AssertionError("unreachable")


def lead_turn(
    messages: list[dict],
    on_retry: Callable[[str], None] | None = None,
    on_lookup: Callable[[int], None] | None = None,
) -> dict:
    """
    Run one conversation turn. `messages` is the history WITHOUT the system
    prompt. Returns {reply, continues, draw|None, edits|None, lookups, usage}.

    May look tags up, on a smaller budget than the drawing call: this is the
    turn that writes library entries, and an entry's tag lines are frozen once
    written, so the call that chooses those words is the one that needs to be
    able to check them. Smaller budget because a reply is waiting on this, not
    a picture.
    """
    parsed, usage = _ask(
        prompts.lead(), messages, LEAD_SCHEMA, rounds=settings.LEAD_TOOL_ROUNDS,
        effort=None, on_retry=on_retry, on_lookup=on_lookup,
    )
    out = _normalize_lead(parsed)
    # the answer as the model gave it, before the app tidied it (dropped a
    # nameless subject, forced a scope): what the inspector shows as 原话
    out["raw_answer"] = parsed if isinstance(parsed, dict) else None
    out["reasoning"] = usage.pop("_reasoning", "")
    out["lookups"] = usage.pop("_lookups", [])
    out["usage"] = usage
    return out


def reflect_turn(
    messages: list[dict], on_retry: Callable[[str], None] | None = None
) -> dict:
    """
    Show the lead model what came back and ask whether it will do.

    Same model, same system prompt, same job — it is still the author looking
    at the picture — so the only thing that changes is the schema and the one
    message the caller appends. Returns {reply, draw|None, usage}: a null draw
    means satisfied; a draw is a change to the picture being looked at.
    """
    parsed, usage = _ask(
        prompts.lead(), messages, REFLECT_SCHEMA, rounds=settings.LEAD_TOOL_ROUNDS,
        effort=None, on_retry=on_retry,
    )
    if not isinstance(parsed, dict):
        raise ModelError("reflection JSON is not an object")
    out = {
        "reply": str(parsed.get("reply") or "").strip(),
        "draw": _brief_from(parsed.get("draw")),
    }
    out["reasoning"] = usage.pop("_reasoning", "")
    out["usage"] = usage
    return out


def _clean_lines(raw) -> list[dict]:
    out = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        tags = str(item.get("tags") or "").strip()
        part = str(item.get("part") or "").strip()
        if tags:
            out.append({"part": part or wording.LINE_PART_OTHER, "tags": tags})
    return out


def _clean_copy_from(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    picture = str(raw.get("picture") or "").strip()
    if not picture:
        return None
    return {"picture": picture, "subject": str(raw.get("subject") or "").strip()}


def _clean_scope(raw) -> str:
    """Which layer an entry goes to. Left out, it goes to this conversation:
    the project is the layer the user asks for by name, so a model that forgot
    to say is not writing there unasked."""
    return "project" if raw == "project" else "conversation"


def _clean_entries(raw, key: str) -> list[dict]:
    """Titled entries with a title; the rest of the fields shape-checked."""
    out = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get(key) or "").strip()
        content = str(item.get("content") or "").strip()
        tags = _clean_lines(item.get("tags"))
        copy_from = _clean_copy_from(item.get("copy_from"))
        if title and (content or tags or copy_from):
            out.append({key: title, "content": content, "tags": tags,
                        "copy_from": copy_from, "scope": _clean_scope(item.get("scope"))})
    return out


def _brief_from(raw_draw) -> dict | None:
    """
    A picture description out of whatever the model sent, or None.

    Shared by the ordinary turn and by reflection so a brief means the same
    thing however it arrived. A brief is something when it changes a picture
    (`change`) or describes one (subjects, or any picture field).
    """
    if not isinstance(raw_draw, dict):
        return None
    brief: dict = {"change": str(raw_draw.get("change") or "").strip()}
    subjects = []
    for item in raw_draw.get("subjects") or []:
        if not isinstance(item, dict):
            continue
        sj = {f: str(item.get(f) or "").strip() for f in SUBJECT_FIELDS}
        if sj["who"] or sj["look"] or sj["doing"]:
            subjects.append(sj)
    brief["subjects"] = subjects
    for f in PICTURE_FIELDS:
        value = str(raw_draw.get(f) or "").strip()
        if value:
            brief[f] = value
    size = raw_draw.get("size")
    if size in ("portrait", "landscape", "square"):
        brief["size"] = size
    # A brief is a picture when it changes one or describes one: subjects, or
    # the picture's own composition, scene or camera. Notes or a style alone
    # describe nothing to draw.
    if not (brief["change"] or subjects or any(brief.get(f) for f in ("composition", "scene", "camera"))):
        return None
    return brief


def _normalize_lead(parsed: dict) -> dict:
    if not isinstance(parsed, dict):
        raise ModelError("lead JSON is not an object")

    out: dict = {
        "reply": str(parsed.get("reply") or "").strip(),
        "continues": str(parsed.get("continues") or "").strip(),
        "draw": None,
        "edits": None,
    }

    out["draw"] = _brief_from(parsed.get("draw"))

    raw_edits = parsed.get("edits")
    if isinstance(raw_edits, dict):
        settings_edits = _clean_entries(raw_edits.get("settings"), "title")
        characters = []
        for item in raw_edits.get("characters") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            variants = _clean_entries(item.get("variants"), "title")
            for v in variants:
                v.pop("scope", None)      # an outfit lives where its character does
            description = str(item.get("description") or "").strip()
            tags = _clean_lines(item.get("tags"))
            copy_from = _clean_copy_from(item.get("copy_from"))
            # a character edit that changes nothing is noise — drop it rather
            # than asking the user to approve nothing
            if description or variants or tags or copy_from:
                characters.append({
                    "name": name, "description": description, "tags": tags,
                    "copy_from": copy_from, "scope": _clean_scope(item.get("scope")),
                    "variants": variants,
                })
        if settings_edits or characters:
            out["edits"] = {"settings": settings_edits, "characters": characters}

    if not out["reply"] and not out["draw"]:
        out["reply"] = EMPTY_REPLY_FALLBACK
    return out


# --------------------------------------------------------------------------
# the drawing model
# --------------------------------------------------------------------------


def format_brief(brief: dict, lines_text: str = "") -> str:
    """The lead model's brief, as the drawing model reads it, then the lines."""
    out = [wording.BRIEF_HEADING]
    subjects = brief.get("subjects") or []
    if subjects:
        out.append(wording.BRIEF_SUBJECTS_LABEL)
    for i, sj in enumerate(subjects, 1):
        bits = [f"  {i}. 「{sj.get('who') or '?'}」"]
        for key, label in wording.BRIEF_SUBJECT_LABELS.items():
            value = str(sj.get(key) or "").strip()
            if value:
                bits.append(f"{label}：{value}")
        out.append("；".join(bits))
    for key, label in wording.BRIEF_PICTURE_LABELS.items():
        value = str(brief.get(key) or "").strip()
        if value:
            out.append(f"{label}：{value}")
    if brief.get("size"):
        out.append(f"{wording.BRIEF_SIZE_LABEL}：{brief['size']}")
    out.append("")
    out.append(wording.BRIEF_LINES_HEADING)
    out.append(lines_text or wording.LIBRARY_NO_LINES)
    return "\n".join(out)


def format_current(
    picture: dict,
    truth: dict[str, list],
    lines_text: str = "",
    setting_truth: dict[str, list] | None = None,
) -> str:
    """
    A picture already drawn, slot by slot, for an edit turn.

    `truth` is {entry name: [(line id, outfit, part, tags)]}: each library
    subject's whole truth table, marked with what the previous picture used,
    so the lines are picked again for this one rather than inherited.
    `setting_truth` is {title: [(line id, part, tags, is 画风)]} for every
    selected setting, marked the same way.
    """
    picture = prompt_slots.upgrade(picture)
    out = [wording.CURRENT_PICTURE_HEADING]
    slots = picture["slots"]
    for name in prompt_slots.FIELD_SLOTS:
        out.append(f"  {name}.tags: {slots[name]['tags']}")
        out.append(f"  {name}.prose: {slots[name]['prose']}")
    out.append(f"  {prompt_slots.TEXT_SLOT}: {slots[prompt_slots.TEXT_SLOT]}")
    if setting_truth:
        picks = {p["from"]: p for p in picture["settings"]}
        out.append("  " + wording.CURRENT_SETTINGS_HEADING)
        for title, rows in setting_truth.items():
            pick = picks.get(title) or {}
            use = pick.get("use") or []
            out.append("    " + wording.CURRENT_SETTING.format(title=title) + f" use={', '.join(use)}")
            for lid, part, tg, style in rows:
                if style:
                    out.append(f"        　 {lid}  {part}：{tg}{wording.LIBRARY_STYLE_LINE_MARK}")
                    continue
                mark = wording.CURRENT_TRUTH_USED if lid in use else "　"
                out.append(f"        {mark} {lid}  {part}：{tg}")
            if pick.get("patches"):
                out.append(f"        patches: {json.dumps(pick['patches'], ensure_ascii=False)}")
    for ch in picture["characters"]:
        key = ch.get("key") or ""
        source = ch.get("from") or ""
        use = ch.get("use") or []
        out.append("  " + wording.CURRENT_SUBJECT.format(key=key)
                   + (wording.CURRENT_SUBJECT_FROM.format(name=source) if source else "")
                   + f" use={', '.join(use)}")
        if source:
            out.append("      " + wording.CURRENT_TRUTH_HEADING)
            for lid, outfit, part, tg in truth.get(source) or []:
                mark = wording.CURRENT_TRUTH_USED if lid in use else "　"
                where = f"[{outfit}] " if outfit else ""
                out.append(f"        {mark} {lid}  {where}{part}：{tg}")
        if ch.get("patches"):
            out.append(f"      patches: {json.dumps(ch['patches'], ensure_ascii=False)}")
        for name in prompt_slots.CHAR_FIELDS:
            out.append(f"      {name}.tags: {ch[name]['tags']}")
            out.append(f"      {name}.prose: {ch[name]['prose']}")
    out.append(f"  negative: {picture.get('negative') or ''}")
    out.append("")
    out.append(wording.BRIEF_LINES_HEADING)
    out.append(lines_text or wording.LIBRARY_NO_LINES)
    return "\n".join(out)


def format_change(brief: dict) -> str:
    change = str(brief.get("change") or "").strip()
    if not change:
        # said it was an edit but wrote the picture out instead: that text is
        # the change description
        change = format_brief(brief).split("\n\n" + wording.BRIEF_LINES_HEADING)[0]
    return wording.CHANGE_HEADING + "\n" + change


def draw_turn(
    brief: dict,
    lines_text: str = "",
    lines: dict[str, dict[str, str]] | None = None,
    on_retry: Callable[[str], None] | None = None,
    on_lookup: Callable[[int], None] | None = None,
    setting_lines: dict[str, dict[str, str]] | None = None,
    style_lines: dict[str, dict[str, str]] | None = None,
) -> dict:
    """
    Turn a brief into NovelAI prompts. This call carries no conversation
    history — only the brief and the selected entries' lines.
    """
    parsed, usage = _ask(
        prompts.drawing(),
        [{"role": "user", "content": format_brief(brief, lines_text)}],
        DRAW_SCHEMA,
        rounds=settings.DRAW_TOOL_ROUNDS,
        effort=providers.active().draw_effort,
        on_retry=on_retry,
        on_lookup=on_lookup,
        # a translator, not an author — the same description should come back
        # as the same tags
        temperature=settings.DRAW_TEMPERATURE,
    )
    out = _normalize_draw(parsed, brief, lines, setting_lines, style_lines)
    out["reasoning"] = usage.pop("_reasoning", "")
    out["lookups"] = usage.pop("_lookups", [])
    out["usage"] = usage
    return out


def draw_edit(
    picture: dict,
    brief: dict,
    truth: dict[str, list],
    lines_text: str = "",
    lines: dict[str, dict[str, str]] | None = None,
    on_retry: Callable[[str], None] | None = None,
    on_lookup: Callable[[int], None] | None = None,
    setting_lines: dict[str, dict[str, str]] | None = None,
    setting_truth: dict[str, list] | None = None,
    style_lines: dict[str, dict[str, str]] | None = None,
) -> dict:
    """
    Change a picture already drawn. The model returns a change list and
    `prompt.apply` merges it; a list the slot table refuses is a model error,
    retried as the same request like any other unusable answer.

    The picture may be in an older shape (a row from before slots had a prose
    half): it is read through `prompt.upgrade` on the way in, so the model is
    shown, and patches, today's shape.
    """
    picture = prompt_slots.upgrade(picture)
    user = (format_current(picture, truth, lines_text, setting_truth)
            + "\n\n" + format_change(brief))
    parsed, usage = _ask(
        prompts.drawing() + "\n\n" + prompts.drawing_edit(),
        [{"role": "user", "content": user}],
        PATCH_SCHEMA,
        rounds=settings.DRAW_TOOL_ROUNDS,
        effort=providers.active().draw_effort,
        on_retry=on_retry,
        on_lookup=on_lookup,
        temperature=settings.DRAW_TEMPERATURE,
    )
    if not isinstance(parsed, dict):
        raise ModelError("drawing model JSON is not an object")
    try:
        merged = prompt_slots.apply(picture, parsed)
    except prompt_slots.PatchRejected as exc:
        raise ModelError(str(exc)) from exc
    if brief.get("size") in ("portrait", "landscape", "square"):
        merged["size"] = brief["size"]
    rendered = prompt_slots.render(merged, lines, setting_lines, style_lines, complete=True)
    if not rendered["base_prompt"]:
        raise ModelError("drawing model returned an empty base_prompt")
    _reject_foreign(merged)
    out = {**rendered, "picture": merged, "patch": parsed}
    out["reasoning"] = usage.pop("_reasoning", "")
    out["lookups"] = usage.pop("_lookups", [])
    out["usage"] = usage
    return out


#: Everything in a draw_turn result that IS the prompts, and nothing else.
#:
#: The result also carries the working, the lookups and the usage, and what
#: gets stored on the row as `nai_prompts` is replayed verbatim by 重绘 — so the
#: two have to be told apart somewhere. Here, as a list of what belongs, rather
#: than at the call site as a list of what does not: this module decides the
#: shape, and a field added here should not have to be excluded over there.
PROMPT_FIELDS = ("n_images", "size", "base_prompt", "character_prompts", "negative_prompt")


def prompts_from(draw: dict) -> dict:
    """The prompts out of a draw_turn result, with nothing else riding along."""
    return {k: draw[k] for k in PROMPT_FIELDS if k in draw}


def same_words(a: dict | None, b: dict | None) -> bool:
    """Whether two prompt dicts would send NovelAI the same words: the base,
    each character's prompt and the negative, nothing else (count, size and
    the drawing id ride along and are not what a picture is made of)."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False

    def words(d: dict) -> tuple:
        return (
            (d.get("base_prompt") or "").strip(),
            tuple((c.get("prompt") or "").strip() for c in d.get("character_prompts") or [] if isinstance(c, dict)),
            (d.get("negative_prompt") or "").strip(),
        )
    return words(a) == words(b)


def _prose_of(picture) -> list[str]:
    """Every sentence a picture carries, lower-cased: the prose half of each
    slot and of each subject's look and moment."""
    if not isinstance(picture, dict):
        return []
    picture = prompt_slots.upgrade(picture)
    out = [picture["slots"][name]["prose"] for name in prompt_slots.FIELD_SLOTS]
    for ch in picture["characters"]:
        out += [ch[name]["prose"] for name in prompt_slots.CHAR_FIELDS]
    return [" ".join(p.lower().split()) for p in out if p.strip()]


def unknown_tags(nai_prompts, picture=None) -> list[str]:
    """
    Every tag in one stored prompt dict that the site does not have.

    The shape is this module's (see PROMPT_FIELDS); what counts as a tag inside
    one of those strings is NovelAI's, because it is NovelAI's syntax; and
    whether a tag exists is the index's. Each question is asked of whoever
    knows the answer, and this is the one place that puts the three together.

    `picture` is the same prompt in slots, when the row has it: a sentence is
    not a tag, and a sentence with a comma in it would otherwise be reported
    piece by piece as several tags the site has never heard of.

    Reported, never enforced. The index is trimmed to tags with enough pictures
    behind them and is a few weeks old besides, so a word missing from it is a
    reason to look, not a reason to refuse to paint.
    """
    if not isinstance(nai_prompts, dict):
        return []
    prose = _prose_of(picture)
    # base, then the characters, then the negative: the order they are written
    # in and the order the dialog shows them, so a reader can find each one
    found: list[str] = [nai_prompts.get("base_prompt") or ""]
    found += [
        c.get("prompt") or ""
        for c in nai_prompts.get("character_prompts") or []
        if isinstance(c, dict)
    ]
    found.append(nai_prompts.get("negative_prompt") or "")
    tags: list[str] = []
    for text in found:
        for tag in novelai.tags_to_check(text):
            # a sentence goes out with its stop; the piece of it that a comma
            # cut off is matched against the sentence without that stop
            piece = tag.rstrip(".!?").strip()
            if any(piece in sentence for sentence in prose):
                continue
            tags.append(tag)
    return danbooru.unknown(tags)


def _reject_foreign(picture: dict) -> None:
    """A prompt NovelAI cannot read as tags is a bad answer, retried like an
    empty one: the drawing model has answered a Chinese brief in Chinese."""
    where = prompt_slots.foreign(picture)
    if where:
        raise ModelError("drawing model wrote prose NovelAI cannot read as tags: " + ", ".join(where))


def can_paint(nai_prompts) -> bool:
    """
    Whether this dict can still make a picture.

    The same question asked at both ends of a prompt's life: when the drawing
    model hands one over, and much later when 重绘 replays one off a row. A row
    can be older than the shape the app now expects — written by an earlier
    version, by MCP, or copied into a branch — and replaying one that cannot
    paint used to send NovelAI an empty prompt and bill for whatever came back.
    """
    return bool(
        isinstance(nai_prompts, dict)
        and str(nai_prompts.get("base_prompt") or "").strip()
    )


def _normalize_draw(parsed: dict, brief: dict, lines: dict | None,
                    setting_lines: dict | None = None, style_lines: dict | None = None) -> dict:
    """
    The drawing model's slots, as both the slots and what NovelAI receives.

    Both are kept: the rendered prompts because that is what is sent, stored and
    replayed by 重绘, and the slots because an edit next turn needs to know which
    part of the picture was the lighting.
    """
    if not isinstance(parsed, dict):
        raise ModelError("drawing model JSON is not an object")

    picture = prompt_slots.from_model(parsed, brief)
    rendered = prompt_slots.render(picture, lines, setting_lines, style_lines, complete=True)
    if not rendered["base_prompt"]:
        raise ModelError("drawing model returned an empty base_prompt")
    _reject_foreign(picture)

    # n_images is not the model's to decide — _paint stamps the app's count in.
    # Kept in the dict because the UI counts placeholders off it and 重绘
    # replays it. `picture` rides alongside and is filtered out of what gets
    # replayed by PROMPT_FIELDS; it is stored in its own column.
    return {**rendered, "picture": picture}


# --------------------------------------------------------------------------
# titles
# --------------------------------------------------------------------------


def write_lines(brief: str) -> tuple[list[dict], dict]:
    """
    The tag lines an entry's text calls for, as a full list of {part, tags}.

    `brief` names the entry, gives its text and the lines it has now; the
    rules are the same tag-line rules the lead works to (prompts.patrol). The
    drawing model's settings apply: this is translation, not writing.
    """
    parsed, usage = _ask(
        prompts.patrol(),
        [{"role": "user", "content": brief}],
        LINES_SCHEMA,
        rounds=settings.DRAW_TOOL_ROUNDS,
        effort=providers.active().draw_effort,
        temperature=settings.DRAW_TEMPERATURE,
    )
    lines = [ln for ln in (parsed.get("lines") or []) if isinstance(ln, dict)]
    return lines, usage


def make_title(user_text: str, reply_text: str) -> str:
    """Name a conversation. Best-effort — a failure here costs nothing."""
    try:
        title = providers.active().describe(
            system=wording.TITLE_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": wording.TITLE_USER.format(
                        user=user_text[:500], reply=reply_text[:300]
                    ),
                }
            ],
            timeout=60,
        )
        return title.strip().strip('"“”「」').strip()[:40]
    except Exception as exc:  # noqa: BLE001 — titles are best-effort
        log.warning("title generation failed: %s", exc)
        return ""
