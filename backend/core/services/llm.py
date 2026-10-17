"""
The model jobs this app has, and what each one must return.

`lead_turn`    the model the user talks to. Keeps the persona, writes the
               reply, writes library entries (this conversation's own, and
               proposed edits to the project's), and when a picture is to be
               made, describes it and says which entries the drawing model
               gets to see.
`reflect_turn` the same model shown what came back, answering in the same
               shape: entries it wants to change, and a picture to draw
               again or null.
`draw`         a second, context-free call: handed the description and the
               exposed entries, it says which entries are in the picture,
               what this picture changes about each, and what this picture
               adds of its own. The app assembles the prompt (assemble.py).
`compress`     the same model handed a finished prompt that is over
               NovelAI's limit, asked to make it fit.
`fill_entry`   the patrol: an entry's tags and prose written from its note.

Both the lead and the drawing model may consult the Danbooru tag table. Not
through the vendor's tool-call mechanism — that road lost whole turns to
calls the server could not parse — but in the answer itself: a `lookup`
field that, when it is not empty, makes the answer a query. The app runs it,
hands the result back as the next message, and asks again.

System prompts live in core/prompts/*.txt. Which vendor answers is not this
module's business — see core/services/providers/.
"""
from __future__ import annotations

import copy
import json
import logging
from collections.abc import Callable

from django.conf import settings

from core import prompts
from core.prompts import wording
from core import i18n, ui_text
from . import assemble, danbooru, library, novelai, providers
from .providers import LLMError
from .spend import record_llm_spend

log = logging.getLogger(__name__)

#: kept so `except grok.ModelError` sites and old imports still read
ModelError = LLMError

# re-exported: the questions callers ask of a prompt dict live with the assembler
can_paint = assemble.can_paint
same_words = assemble.same_words
unknown_tags = assemble.unknown_tags

# --------------------------------------------------------------------------
# schemas
# --------------------------------------------------------------------------

_STR = {"type": "string"}
_BOOL = {"type": "boolean"}
_NUM_OR_NULL = {"anyOf": [{"type": "number"}, {"type": "null"}]}
_STR_OR_NULL = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_STR_LIST = {"type": "array", "items": _STR}

#: Words to look up in the tag table. Present on every schema but the last
#: round's: an answer that fills it is a question, not an answer.
_LOOKUP = _STR_LIST

#: An entry as the lead writes one: its key, the note, the tags (comma
#: separated) and the prose. A setting also names its category. A costume is
#: an entry of its own among the characters, keyed `character/costume` --
#: the key it has everywhere else -- and the app files it under its character.
ENTRY_FIELDS = ("key", "note", "positive", "negative", "prose")
_ENTRY_PROPS = {f: _STR for f in ENTRY_FIELDS}
_SETTING_ENTRY = {
    "type": "object", "additionalProperties": False,
    "required": [*ENTRY_FIELDS, "category"],
    "properties": {**_ENTRY_PROPS, "category": _STR},
}
_CHARACTER_ENTRY = {
    "type": "object", "additionalProperties": False,
    "required": list(ENTRY_FIELDS),
    "properties": dict(_ENTRY_PROPS),
}
_ENTRIES = {
    "type": "object", "additionalProperties": False,
    "required": ["settings", "characters"],
    "properties": {
        "settings": {"type": "array", "items": _SETTING_ENTRY},
        "characters": {"type": "array", "items": _CHARACTER_ENTRY},
    },
}
#: The picture: the description, which entries the drawing model gets, the
#: picture it continues, the text to render, the size.
DRAW_FIELDS = ("description", "expose", "inherits", "text", "size")
_DRAW_OBJECT = {
    "type": "object", "additionalProperties": False,
    "required": list(DRAW_FIELDS),
    "properties": {
        "description": _STR,
        "expose": _STR_LIST,
        "inherits": _STR,
        "text": _STR,
        "size": {"type": "string", "enum": ["portrait", "landscape", "square", ""]},
    },
}
LEAD_SCHEMA = {
    "name": "vallotton_turn",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["reply", "settings", "characters", "edits", "draw"],
        "properties": {
            "lookup": _LOOKUP,
            "reply": _STR,
            # this conversation's own entries, written or overridden this turn
            "settings": {"type": "array", "items": _SETTING_ENTRY},
            "characters": {"type": "array", "items": _CHARACTER_ENTRY},
            # null = propose no change to the project's library (the common case)
            "edits": {"anyOf": [{"type": "null"}, _ENTRIES]},
            # null = this turn draws nothing
            "draw": {"anyOf": [{"type": "null"}, _DRAW_OBJECT]},
        },
    },
}
#: One look at what came back: the same shape, so the lead cannot drift
#: between its two jobs. A null `draw` IS "I am satisfied".
REFLECT_SCHEMA = {**LEAD_SCHEMA, "name": "vallotton_reflection"}

#: What this picture does with one exposed entry.
_CHANGE = {
    "type": "object", "additionalProperties": False,
    "required": ["key", "use", "add_positive", "remove_positive", "add_negative", "remove_negative",
                 "add_prose", "remove_prose"],
    "properties": {
        "key": _STR, "use": _BOOL,
        "add_positive": _STR, "remove_positive": _STR,
        "add_negative": _STR, "remove_negative": _STR,
        # sentences this picture adds to the entry's own, and sentences of
        # the entry's own it takes out, copied as they are
        "add_prose": _STR, "remove_prose": _STR,
    },
}
_SUBJECT = {
    "type": "object", "additionalProperties": False,
    "required": ["key", "center_x", "center_y"],
    "properties": {"key": _STR, "center_x": _NUM_OR_NULL, "center_y": _NUM_OR_NULL},
}
DRAW_SCHEMA = {
    "name": "novelai_prompts",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["entries", "positive", "negative", "prose", "subjects", "size"],
        "properties": {
            "lookup": _LOOKUP,
            "entries": {"type": "array", "items": _CHANGE},
            "positive": _STR,
            "negative": _STR,
            "prose": _STR,
            "subjects": {"type": "array", "items": _SUBJECT},
            "size": {"type": "string", "enum": ["portrait", "landscape", "square"]},
        },
    },
}

#: The compressor answers in the shape NovelAI is sent: the words, nothing
#: about where they came from. Characters are matched back by position.
COMPRESS_SCHEMA = {
    "name": "compressed_prompts",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["base_prompt", "character_prompts", "negative_prompt"],
        "properties": {
            "base_prompt": _STR,
            "character_prompts": {
                "type": "array",
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["name", "prompt", "negative"],
                    "properties": {"name": _STR, "prompt": _STR, "negative": _STR},
                },
            },
            "negative_prompt": _STR,
        },
    },
}

#: the patrol's answer: an entry's tags and prose, whole
FILL_SCHEMA = {
    "name": "library_entry",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["positive", "negative", "prose"],
        "properties": {"positive": _STR, "negative": _STR, "prose": _STR, "lookup": _LOOKUP},
    },
}


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------

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
    check: Callable[[dict], str] | None = None,
) -> tuple[dict, dict]:
    """
    Ask until answered, looking tags up in between.

    `rounds` is how many lookups may happen. Every request is the same shape
    and goes through the same parser; the last allowed round's schema simply
    has no `lookup` field, so the model can only answer. What was looked up
    rides back in usage under `_lookups`, for 「检查」.

    `check` reads a finished answer and says what is wrong with it, or "".
    A note goes back once, as the next message, and the model answers again
    in full; that second answer stands whatever it says. What it was told
    rides back under `_checks`.
    """
    history = list(messages)
    lookups: list[dict] = []
    checks: list[dict] = []
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
            note = check(parsed) if check is not None and isinstance(parsed, dict) else ""
            if note:
                checks.append({"note": note, "answer": parsed})
                history.append({"role": "assistant", "content": json.dumps(parsed, ensure_ascii=False)})
                history.append({"role": "user", "content": note})
                parsed, usage = _run(
                    system, history, _without_lookup(schema),
                    effort=effort, on_retry=on_retry, temperature=temperature,
                )
                retries += int(usage.get("retries") or 0)
                if isinstance(parsed, dict):
                    parsed = {k: v for k, v in parsed.items() if k != "lookup"}
            if retries:
                usage["retries"] = retries
            if lookups:
                usage["_lookups"] = lookups
            if checks:
                usage["_checks"] = checks
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


# --------------------------------------------------------------------------
# the lead
# --------------------------------------------------------------------------

def _str(v) -> str:
    return str(v or "").strip()


def _clean_entry(raw, *, with_category: bool = False, with_variants: bool = False) -> dict | None:
    """One entry as the lead wrote it, shape-checked; None without a key."""
    if not isinstance(raw, dict):
        return None
    key = _str(raw.get("key"))
    if not key:
        return None
    out: dict = {
        "key": key, "note": _str(raw.get("note")),
        "positive": library.split_tags(raw.get("positive")),
        "negative": library.split_tags(raw.get("negative")),
        "prose": _str(raw.get("prose")),
    }
    if with_category:
        out["category"] = _str(raw.get("category"))[:40]
    if with_variants:
        out["variants"] = [v for v in (_clean_entry(x) for x in raw.get("variants") or []) if v]
    return out


def nest_costumes(items: list[dict]) -> list[dict]:
    """
    The characters as the library holds them: a costume written as an entry
    of its own (`林晚/日常装`) goes under its character, keyed by its title.
    A costume whose character the answer does not write rides on a carrier
    (`carrier: True`) that names the character and leaves it as it is.
    Costumes already nested (the shape answers had before) stay where they are.
    """
    out: list[dict] = []
    owner: dict[str, dict] = {}
    for item in items:
        is_project, name, title = library.parse_key(item["key"])
        if not name:
            continue
        if not title:
            entry = {**item, "variants": list(item.get("variants") or [])}
            held = owner.get(name)
            if held is not None and held.get("carrier"):
                # the costume came first, its character after: one entry
                held.update({k: v for k, v in entry.items() if k != "variants"})
                held.pop("carrier", None)
                held["variants"] += entry["variants"]
                continue
            owner[name] = entry
            out.append(entry)
            continue
        held = owner.get(name)
        if held is None:
            held = {"key": (library.PROJECT_PREFIX if is_project else "") + name, "carrier": True, "variants": []}
            owner[name] = held
            out.append(held)
        costume = {k: v for k, v in item.items() if k != "variants"}
        held["variants"].append({**costume, "key": title})
    return out


def _clean_entries(raw) -> dict | None:
    """{settings, characters} out of whatever was sent, or None when empty."""
    if not isinstance(raw, dict):
        return None
    out = {
        "settings": [e for e in (_clean_entry(x, with_category=True) for x in raw.get("settings") or []) if e],
        "characters": nest_costumes(
            [e for e in (_clean_entry(x, with_variants=True) for x in raw.get("characters") or []) if e]),
    }
    return out if out["settings"] or out["characters"] else None


def as_said(entries: dict | None) -> dict | None:
    """
    {settings, characters} the way the lead writes them, for showing it its
    own earlier answers: the tags as one string, each costume an entry of its
    own keyed `character/costume`, and a character the answer did not write
    (a carrier) not there at all.
    """
    if not isinstance(entries, dict):
        return None

    def said(entry: dict) -> dict:
        out = {k: v for k, v in entry.items() if k not in ("variants", "carrier")}
        for k in ("positive", "negative"):
            if isinstance(out.get(k), list):
                out[k] = ", ".join(out[k])
        return out

    characters: list[dict] = []
    for c in entries.get("characters") or []:
        if not c.get("carrier"):
            characters.append(said(c))
        for v in c.get("variants") or []:
            title = library.parse_key(str(v.get("key") or ""))[2] or str(v.get("key") or "").strip()
            characters.append(said({**v, "key": f"{c.get('key')}{library.VARIANT_SEP}{title}"}))
    return {"settings": [said(e) for e in entries.get("settings") or []], "characters": characters}


def _draw_from(raw) -> dict | None:
    """The picture out of whatever the model sent, or None. A picture is
    something when it has a description or exposes an entry."""
    if not isinstance(raw, dict):
        return None
    expose = [_str(k) for k in (raw.get("expose") or []) if _str(k)] if isinstance(raw.get("expose"), list) else []
    size = raw.get("size")
    out = {
        "description": _str(raw.get("description")),
        "expose": expose,
        "inherits": _str(raw.get("inherits")),
        "text": _str(raw.get("text")),
        "size": size if size in ("portrait", "landscape", "square") else "",
    }
    if not (out["description"] or out["expose"]):
        return None
    return out


def _normalize_lead(parsed) -> dict:
    if not isinstance(parsed, dict):
        raise ModelError("lead JSON is not an object")
    own = _clean_entries({"settings": parsed.get("settings"), "characters": parsed.get("characters")}) \
        or {"settings": [], "characters": []}
    out: dict = {
        "reply": _str(parsed.get("reply")),
        "settings": own["settings"],
        "characters": own["characters"],
        "edits": _clean_entries(parsed.get("edits")),
        "draw": _draw_from(parsed.get("draw")),
    }
    if not out["reply"] and not out["draw"]:
        out["reply"] = ui_text.EMPTY_REPLY_FALLBACK
    return out


def _lead_check(check: Callable[[dict], str] | None) -> Callable[[dict], str] | None:
    """A check of the lead's answer as the app reads it, not as it arrived."""
    if check is None:
        return None
    return lambda parsed: check(_normalize_lead(parsed))


def lead_turn(
    messages: list[dict],
    on_retry: Callable[[str], None] | None = None,
    on_lookup: Callable[[int], None] | None = None,
    check: Callable[[dict], str] | None = None,
) -> dict:
    """
    Run one conversation turn. `messages` is the history WITHOUT the system
    prompt. `check` reads the tidied answer and returns a note when it does
    not hold together (see _ask). Returns {reply, settings, characters,
    edits|None, draw|None, raw_answer, reasoning, lookups, checks, usage}.
    """
    parsed, usage = _ask(
        prompts.lead(), messages, LEAD_SCHEMA, rounds=settings.LEAD_TOOL_ROUNDS,
        effort=None, on_retry=on_retry, on_lookup=on_lookup, check=_lead_check(check),
    )
    out = _normalize_lead(parsed)
    out["raw_answer"] = parsed if isinstance(parsed, dict) else None
    out["reasoning"] = usage.pop("_reasoning", "")
    out["lookups"] = usage.pop("_lookups", [])
    out["checks"] = usage.pop("_checks", [])
    out["usage"] = usage
    return out


def reflect_turn(
    messages: list[dict],
    on_retry: Callable[[str], None] | None = None,
    check: Callable[[dict], str] | None = None,
) -> dict:
    """
    Show the lead model what came back and ask whether it will do. Same
    model, same system prompt, same shape, same check; a null draw means
    satisfied.
    """
    parsed, usage = _ask(
        prompts.lead(), messages, REFLECT_SCHEMA, rounds=settings.LEAD_TOOL_ROUNDS,
        effort=None, on_retry=on_retry, check=_lead_check(check),
    )
    out = _normalize_lead(parsed)
    out["raw_answer"] = parsed if isinstance(parsed, dict) else None
    out["reasoning"] = usage.pop("_reasoning", "")
    out["lookups"] = usage.pop("_lookups", [])
    out["checks"] = usage.pop("_checks", [])
    out["usage"] = usage
    return out


# --------------------------------------------------------------------------
# the drawing model
# --------------------------------------------------------------------------

def format_draw(description: str, exposed: list[dict], *, text: str = "", size: str = "") -> str:
    """The drawing model's one message: the description, the text to render
    and the size when the lead gave them, then the exposed entries in full."""
    blocks = [wording.DRAW_DESCRIPTION_HEADING + "\n" + (description.strip() or wording.DRAW_NO_DESCRIPTION)]
    if text:
        blocks.append(wording.DRAW_TEXT_HEADING + "\n" + text.strip())
    if size:
        blocks.append(wording.DRAW_SIZE_HEADING + "\n" + size)
    blocks.append(wording.DRAW_ENTRIES_HEADING + "\n" + library.exposure_text(exposed))
    return "\n\n".join(blocks)


def draw(
    description: str,
    exposed: list[dict],
    *,
    text: str = "",
    size: str = "",
    on_retry: Callable[[str], None] | None = None,
    on_lookup: Callable[[int], None] | None = None,
) -> dict:
    """
    One picture's prompt. The drawing model reads the description and the
    exposed entries and answers with what this picture does with each entry
    and what it adds; the app assembles the prompt.

    Returns {nai_prompts, assembly, answer, asked, reasoning, lookups, usage}.
    An answer that assembles to nothing, or to prose NovelAI cannot read as
    tags, is a ModelError: the turn's retry policy decides what to do.
    """
    asked = format_draw(description, exposed, text=text, size=size)
    parsed, usage = _ask(
        prompts.drawing(),
        [{"role": "user", "content": asked}],
        DRAW_SCHEMA,
        rounds=settings.DRAW_TOOL_ROUNDS,
        effort=providers.active().draw_effort,
        on_retry=on_retry,
        on_lookup=on_lookup,
        # a translator, not an author — the same description should come back
        # as the same tags
        temperature=settings.DRAW_TEMPERATURE,
    )
    answer = assemble.clean_answer(parsed)
    made = assemble.assemble(exposed, answer, text=text, size_hint=size)
    nai_prompts = made["nai_prompts"]
    # the quality words are the app's and are always there, so the base
    # prompt is never empty on its own: what has to be there is something of
    # the picture's own, or of an entry the picture uses
    said = bool(answer["positive"] or answer["prose"] or any(
        r["used"] and (r["positive"] or r["prose"]) for r in made["assembly"]))
    if not said or not can_paint(nai_prompts):
        raise ModelError("drawing model returned an empty prompt")
    where = assemble.foreign(nai_prompts)
    if where:
        raise ModelError("drawing model wrote prose NovelAI cannot read as tags: " + ", ".join(where))
    return {
        "nai_prompts": nai_prompts,
        "assembly": made["assembly"],
        "answer": answer,
        "asked": asked,
        "reasoning": usage.pop("_reasoning", ""),
        "lookups": usage.pop("_lookups", []),
        "usage": usage,
    }


# --------------------------------------------------------------------------
# the compressor
# --------------------------------------------------------------------------

def format_compress(asked: str, nai_prompts: dict, counts: dict) -> str:
    """The compressor's one message: what the drawing model read, the limit
    and the counts, and the prompt as it stands."""
    rule = settings.NAI_PROMPT_TOKEN_RULE if settings.NAI_PROMPT_TOKEN_RULE in wording.REVIEW_RULE else "total"
    said = [wording.REVIEW_COUNT_BASE.format(n=counts["base"])]
    said += [wording.REVIEW_COUNT_CHAR.format(key=c["key"], n=c["tokens"]) for c in counts["characters"]]
    said.append(wording.REVIEW_COUNT_NEGATIVE.format(n=counts["negative"]))
    over = novelai.excess(counts)
    head = wording.REVIEW_LIMIT_HEADING + "\n" + wording.REVIEW_LIMIT_LINE.format(
        limit=novelai.limit(), rule=wording.REVIEW_RULE[rule], counts=wording.REVIEW_COUNT_SEP.join(said))
    head += wording.REVIEW_LIMIT_OVER.format(over=over) if over else wording.REVIEW_LIMIT_FITS
    sent = [wording.REVIEW_PROMPTS_HEADING,
            wording.REVIEW_PROMPT_BASE.format(text=nai_prompts.get("base_prompt") or "")]
    for c in nai_prompts.get("character_prompts") or []:
        sent.append(wording.REVIEW_PROMPT_CHAR.format(key=c.get("key") or "", text=c.get("prompt") or ""))
        if c.get("negative"):
            sent.append(wording.REVIEW_PROMPT_CHAR_NEGATIVE.format(key=c.get("key") or "", text=c.get("negative") or ""))
    sent.append(wording.REVIEW_PROMPT_NEGATIVE.format(text=nai_prompts.get("negative_prompt") or ""))
    return "\n\n".join([asked.strip(), head, "\n".join(sent)])


def compress(
    asked: str,
    nai_prompts: dict,
    *,
    on_retry: Callable[[str], None] | None = None,
) -> dict:
    """
    Make a prompt fit NovelAI's limit: the drawing model, on its own
    settings, handed what it read and the prompt as assembled, answers with
    the prompt cut down. Returns {nai_prompts, before, counts, changed, fits,
    tokens, reasoning, usage}. A wrong number of characters or no base
    prompt is a ModelError.
    """
    before = novelai.prompt_tokens(nai_prompts)
    parsed, usage = _ask(
        prompts.compressor(),
        [{"role": "user", "content": format_compress(asked, nai_prompts, before)}],
        COMPRESS_SCHEMA,
        rounds=0,
        effort=providers.active().draw_effort,
        on_retry=on_retry,
        temperature=settings.DRAW_TEMPERATURE,
    )
    if not isinstance(parsed, dict):
        raise ModelError("compressor JSON is not an object")
    chars_in = nai_prompts.get("character_prompts") or []
    chars_out = parsed.get("character_prompts") or []
    if len(chars_out) != len(chars_in):
        raise ModelError(f"compressor returned {len(chars_out)} character prompts for {len(chars_in)}")
    base = _str(parsed.get("base_prompt"))
    if not base:
        raise ModelError("compressor returned an empty base_prompt")
    shorter = {
        **nai_prompts,
        "base_prompt": base,
        "character_prompts": [{**c, "prompt": _str(o.get("prompt")) or (c.get("prompt") or ""),
                               "negative": _str(o.get("negative"))}
                              for c, o in zip(chars_in, chars_out)],
        "negative_prompt": _str(parsed.get("negative_prompt")),
    }
    after = novelai.prompt_tokens(shorter)
    over = novelai.over_limit(after)
    reasoning = usage.pop("_reasoning", "")
    usage.pop("_lookups", None)
    return {
        "nai_prompts": shorter,
        "before": {k: nai_prompts.get(k) for k in ("base_prompt", "character_prompts", "negative_prompt")},
        "counts": {"before": before, "after": after},
        "changed": not same_words(shorter, nai_prompts),
        "fits": not over,
        "tokens": max((n for _, n in over), default=after["positive"]),
        "reasoning": reasoning, "usage": usage,
    }


# --------------------------------------------------------------------------
# the patrol and titles
# --------------------------------------------------------------------------

def fill_entry(brief: str) -> tuple[dict, dict]:
    """
    An entry's tags and prose, written from its note. `brief` names the entry,
    its kind, its note and what it has now. The drawing model's settings
    apply: this is translation, not writing.
    """
    parsed, usage = _ask(
        prompts.patrol(),
        [{"role": "user", "content": brief}],
        FILL_SCHEMA,
        rounds=settings.DRAW_TOOL_ROUNDS,
        effort=providers.active().draw_effort,
        temperature=settings.DRAW_TEMPERATURE,
    )
    if not isinstance(parsed, dict):
        raise ModelError("patrol JSON is not an object")
    return {
        "positive": library.split_tags(parsed.get("positive")),
        "negative": library.split_tags(parsed.get("negative")),
        "prose": _str(parsed.get("prose")),
    }, usage


def make_title(user_text: str, reply_text: str) -> str:
    """Name a conversation. Best-effort — a failure here costs nothing."""
    try:
        title = providers.active().describe(
            system=wording.TITLE_SYSTEM + wording.TITLE_LANGUAGE[i18n.current()],
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
