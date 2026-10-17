"""
A provider that answers instantly and costs nothing. For the e2e stack only.

The browser suites exist to prove things the Django tests cannot: that the real
HTTP endpoints, the real polling, and the server-side run loop hold up with a
real browser in front of them — and, for continuous mode, with no browser at
all. None of that needs a real model. Driving two vendors for every run made
the suites cost minutes and money apiece, which in practice meant they were run
selectively, which is the opposite of what a test suite is for.

So this stands in for the vendor and nothing else does. Everything downstream —
the schemas, the normalisation in llm.py, the pipeline, reflection, continuous
— runs exactly as it does in production.

It is reachable only by setting LLM_PROVIDER=fake, which tmp/test-stack.sh does
and .env does not. It is registered alongside the real ones rather than hidden
behind a test-only import so that `active()` stays the single way a provider is
chosen; a second mechanism for picking one is how a stub ends up in front of a
user.

**What it decides, it decides from the message it was sent** — never from the
prompt files. Those are the user's to reword at any time, and a stub that reads
them is a test that fails on an edit to prose.
"""

from __future__ import annotations

import itertools
import os
import re
import time


from core.prompts import wording

from .base import (
    Capabilities,
    Completion,
    LLMError,
    Provider,
)
from core import ui_text

#: How many calls the "fail on purpose" marker has already been spent on.
#:
#: Keyed to a marker rather than counting every turn, because a plain counter
#: is spent by whichever suite happens to generate first — the failures a run
#: was meant to see were being eaten by its neighbours, and which neighbour
#: depended on the order they ran in.
FAIL_MARK = "故意失败"
#: A turn that fails the way the real ones do: an answer that arrived whole and
#: could not be read. The suites need the raw answer to be kept, and keeping it
#: is only observable when there is one.
UNREADABLE_MARK = "故意乱码"
#: A whole answer that is simply not JSON — prose that restates the picture,
#: which is what this looks like in life. Several lines long on purpose: the
#: transcript has to fold it down to one.
_UNREADABLE = (
    "好的，这是画面描述：\n"
    "少女站在城墙上，侧身回头，黄昏的光从她背后来。\n"
    "画幅：portrait\n"
    "（这一整段本该是一个 json 对象）"
)
FAIL_CALLS = 3  # the original plus the two free retries the pipeline allows
_failed = 0
#: A turn that looks a tag up before answering. The stub answers with a
#: `lookup` once, the app runs it against the real index and asks again, and
#: the second answer is the ordinary one — the whole loop, driven for real.
LOOKUP_MARK = "查标签"
#: A turn that changes the previous drawing rather than making a new one:
#: `inherits` names it, the description is written whole.
EDIT_MARK = "改一下"
#: A turn that writes a setting into this conversation's library: 「黄昏」 in
#: the text becomes the entry's key. With PROJECT_MARK as well, the same
#: entry is also proposed for the project (an edit with the @ prefix).
REMEMBER_WORDS = ("记下来", "记住", "记到")
PROJECT_MARK = "进项目"
#: A turn that writes a character: 角色「林晚」 becomes a character entry with
#: one outfit, 「林晚/日常装」.
CHARACTER_MARK = "角色「"
#: What the stand-in writes into an entry it makes: enough for the picture
#: to be assembled from it and for a suite to see which entry was used.
STUB_SETTING_TAGS = "sunset, warm lighting"
STUB_CHARACTER_TAGS = "1girl, silver hair, blue eyes"
STUB_VARIANT_TAGS = "white dress"
STUB_PICTURE_TAGS = "1girl, solo, looking at viewer"
#: What the stand-in writes when it is asked to fill an entry from its note
#: (the patrol): the hair follows one word of the note, which is how a suite
#: sees that the entry was written FROM the note.
STUB_FILL_BROWN = "1girl, brown hair"
STUB_FILL_BLACK = "1girl, black hair"


def _flag(name: str, default: str) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return int(default)


class FakeProvider(Provider):
    name = "fake"
    label = ui_text.PROVIDER_FAKE
    capabilities = Capabilities(
        json_schema=True,
        vision=True,
        reasoning_effort=True,
        native_cost=False,
        balance=False,
    )

    def __init__(self) -> None:
        # Reflection has to be able to do both things: ask for a redraw, and
        # then be satisfied. Which one it does is counted here rather than read
        # out of the prompt — see the note at the top of this module.
        self._reflections = itertools.count()
        self._redraw_rounds = _flag("FAKE_REFLECT_REDRAWS", "1")

    # ----------------------------------------------------------- the calls

    def complete(
        self,
        *,
        system: str,
        messages: list[dict],
        schema: dict,
        timeout: float,
        effort: str | None = None,
        temperature: float | None = None,
        on_retry=None,
    ) -> Completion:
        _pause()
        kind = (schema or {}).get("name", "")
        if kind == "vallotton_turn" and UNREADABLE_MARK in _last_user(messages):
            # not a refusal and not a timeout: a complete answer that is not
            # JSON, which is the failure this app actually sees
            raise LLMError(
                "fake returned unparseable JSON: " + _UNREADABLE[:60],
                raw=_UNREADABLE,
            )
        if kind == "vallotton_turn" and FAIL_MARK in _last_user(messages):
            # One turn that fails, on request. A run that stumbles and retries
            # is the shape several things only go wrong in — the replaced turn
            # being left on screen, above all — and it cannot be driven from
            # the browser. Enough calls to exhaust the free retries, then it
            # works, so the run carries on and can be watched to the end.
            global _failed
            if _failed < FAIL_CALLS:
                _failed += 1
                raise LLMError("假模型按要求失败了一次")
        if _wants_lookup(schema, messages):
            # once per turn: the app answers it and asks again without the
            # field, so this cannot loop
            parsed = {"lookup": ["1girl", "window", "indoors"]}
        elif kind == "vallotton_turn":
            # A new turn, so a new redraw budget. Counting across the process
            # instead made the stub stateful between suites: whichever suite
            # ran first spent the redraws, and the reflection suite passed
            # alone and failed in a sweep. A turn is the right scope because
            # every reflection sequence sits inside exactly one.
            self._reflections = itertools.count()
            parsed = _turn(_last_user(messages), messages)
        elif kind == "novelai_prompts":
            parsed = _picture(_last_user(messages))
        elif kind == "library_entry":
            parsed = _fill(_last_user(messages))
        elif kind == "vallotton_reflection":
            n = next(self._reflections)
            parsed = {
                "reply": "假模型的检查结论。",
                "settings": [], "characters": [], "edits": None,
                # the app fills `inherits` in with the drawing being looked at
                "draw": _draw("再亮一点", []) if n < self._redraw_rounds else None,
            }
        elif kind == "compressed_prompts":
            # the prompt back as it came: the stand-in has no opinion, and
            # what the suites expect to see painted is the drawing stub's words
            parsed = _echo_prompt(_last_user(messages))
        else:
            raise LLMError(
                f"the fake provider was handed a schema it does not know: {kind!r}"
            )
        return Completion(
            parsed=parsed, usage={"prompt_tokens": 1200}, cost_ticks=None,
        )

    def describe(self, *, system: str, messages: list[dict], timeout: float) -> str:
        _pause()
        return "假模型的描述。"

    # ------------------------------------------------------------- budgets

    @property
    def context_limit(self) -> int:
        return 500_000

    @property
    def long_context_threshold(self) -> int:
        return 200_000



# ------------------------------------------------------------------ helpers


def _pause() -> None:
    """
    Take a moment, on purpose.

    An instant answer is not a faster test, it is a blind one: the phase line,
    the stop button that changes shape mid-reflection, the chip that counts
    along with a run — all of them exist only while a turn is in flight, and a
    turn that finishes between two polls cannot be looked at. This is the
    smallest delay that leaves them on screen long enough to be read.
    """
    ms = _flag("FAKE_LATENCY_MS", "600")
    if ms > 0:
        time.sleep(ms / 1000.0)


def _wants_lookup(schema: dict, messages: list[dict]) -> bool:
    """
    Whether this request should come back as a lookup.

    Only when the schema still offers the field (the last allowed round does
    not), the turn asked for it, and it has not been answered yet — the answer
    arrives as a message under its own heading, which is the whole state.
    """
    if "lookup" not in ((schema or {}).get("schema") or {}).get("properties", {}):
        return False
    if LOOKUP_MARK not in _last_user(messages):
        return False
    return not any(
        isinstance(m.get("content"), str) and m["content"].startswith(wording.LOOKUP_HEADING)
        for m in messages
    )


def _last_draw_id(messages: list[dict]) -> str:
    """The id of the newest drawing in the history, read off the app's note."""
    for m in reversed(messages or []):
        content = m.get("content")
        texts = [content] if isinstance(content, str) else [
            p.get("text") or "" for p in (content or []) if isinstance(p, dict)
        ]
        for text in texts:
            found = re.search(r"本次绘图调用的id为 (\w+)", text or "")
            if found:
                return found.group(1)
    return ""


# ------------------------------------------------------------- the lead


def _turn(said: str, messages: list[dict]) -> dict:
    """
    One lead turn, decided from what the user typed.

    「X」 with a remember word writes setting X into this conversation; 角色「X」
    writes character X with one outfit; EDIT_MARK makes the picture an edit of
    the newest drawing; 不用画 draws nothing. Everything written this turn is
    exposed to the drawing stub, so a suite can watch an entry travel from the
    lead's answer into the prompt NovelAI is sent.
    """
    settings = _settings(said)
    characters = _characters(said)
    edits = None
    if settings and PROJECT_MARK in said:
        edits = {"settings": [{**s, "key": "@" + s["key"]} for s in settings], "characters": []}
    expose = [s["key"] for s in settings]
    for ch in characters:
        expose += [f"{ch['key']}/{v['key']}" for v in ch["variants"]] or [ch["key"]]
    previous = _last_draw_id(messages) if EDIT_MARK in said else ""
    return {
        "reply": f"假模型收到：{said[:40]}" if said else "假模型收到。",
        "settings": settings,
        "characters": characters,
        "edits": edits,
        "draw": None if "不用画" in said else _draw(said or "一个人物站在窗前", expose, inherits=previous),
    }


def _draw(description: str, expose: list[str], *, inherits: str = "") -> dict:
    return {"description": description, "expose": expose, "inherits": inherits,
            "text": "", "size": "portrait"}


def _settings(said: str) -> list[dict]:
    if not any(w in said for w in REMEMBER_WORDS):
        return []
    keys = [k for k in re.findall(r"「([^」]+)」", said.replace(CHARACTER_MARK, "角色『"))]
    return [{"key": k, "category": "其他", "note": said[-80:],
             "positive": STUB_SETTING_TAGS, "negative": "", "prose": ""} for k in keys[:1]]


def _characters(said: str) -> list[dict]:
    names = re.findall(re.escape(CHARACTER_MARK) + r"([^」]+)」", said)
    return [{"key": n, "note": said[-80:], "positive": STUB_CHARACTER_TAGS, "negative": "", "prose": "",
             "variants": [{"key": "日常装", "note": "", "positive": STUB_VARIANT_TAGS,
                           "negative": "", "prose": ""}]}
            for n in names[:1]]


# ---------------------------------------------------------- the drawing


def _exposed_keys(said: str) -> list[tuple[str, str]]:
    """(key, kind) of every entry in the drawing model's message, read off
    the lines library.exposure_text writes."""
    out = []
    for key, kind in re.findall(r"^◆ (.+?)（([^）·]+)", said, flags=re.M):
        out.append((key, kind))
    return out


def _picture(said: str) -> dict:
    """The drawing stub: every exposed entry used as it is, the picture's own
    tags fixed, one subject per exposed character."""
    exposed = _exposed_keys(said)
    return {
        "entries": [{"key": k, "use": True, "add_positive": "", "remove_positive": "",
                     "add_negative": "", "remove_negative": "", "add_prose": "", "remove_prose": ""}
                    for k, _ in exposed],
        "positive": STUB_PICTURE_TAGS,
        "negative": "",
        "prose": "",
        "subjects": [{"key": k, "center_x": None, "center_y": None}
                     for k, kind in exposed if kind == wording.ENTRY_KIND_CHARACTER],
        "size": "portrait",
    }


def _fill(note: str) -> dict:
    """The patrol stub: an entry written FROM its note, visibly."""
    return {"positive": STUB_FILL_BROWN if "棕" in note else STUB_FILL_BLACK,
            "negative": "", "prose": ""}


def _echo_prompt(said: str) -> dict:
    """The prompt in the compressor's message, read back off its own lines
    (llm.format_compress writes one per piece, under the prompts heading)."""
    base_head = wording.REVIEW_PROMPT_BASE.split("{text}")[0]
    negative_head = wording.REVIEW_PROMPT_NEGATIVE.split("{text}")[0]
    char_head, char_mid = wording.REVIEW_PROMPT_CHAR.split("{key}")[0], \
        wording.REVIEW_PROMPT_CHAR.split("{key}")[1].split("{text}")[0]
    cneg_head, cneg_mid = wording.REVIEW_PROMPT_CHAR_NEGATIVE.split("{key}")[0], \
        wording.REVIEW_PROMPT_CHAR_NEGATIVE.split("{key}")[1].split("{text}")[0]
    out: dict = {"base_prompt": "", "character_prompts": [], "negative_prompt": ""}
    for line in said.split(wording.REVIEW_PROMPTS_HEADING, 1)[-1].splitlines():
        if line.startswith(base_head):
            out["base_prompt"] = line[len(base_head):]
        elif line.startswith(negative_head):
            out["negative_prompt"] = line[len(negative_head):]
        elif line.startswith(cneg_head) and cneg_mid in line and out["character_prompts"]:
            out["character_prompts"][-1]["negative"] = line[len(cneg_head):].split(cneg_mid, 1)[1]
        elif line.startswith(char_head) and char_mid in line:
            name, prompt = line[len(char_head):].split(char_mid, 1)
            out["character_prompts"].append({"name": name, "prompt": prompt, "negative": ""})
    return out


def _last_user(messages: list[dict]) -> str:
    """The words this turn is answering, whether sent as text or as parts."""
    for m in reversed(messages or []):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = (part.get("text") or "").strip()
                    if text:
                        return text
    return ""
