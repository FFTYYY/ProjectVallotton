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
import json
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
#: A turn that changes the previous drawing rather than making a new one.
EDIT_MARK = "改一下"
#: The same, but the way the real lead sometimes says it: a `change` with no
#: `continues` at all. The app has to read that as a change to the latest
#: drawing, and a suite can only check that if the stub can say it.
NAMELESS_EDIT_MARK = "改最近"


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
            said = _last_user(messages)
            # "改" with a drawing in the history: an edit of that drawing, so
            # a browser run exercises the change-list path and the seed that
            # follows the main picture
            previous = _last_draw_id(messages) if EDIT_MARK in said else ""
            nameless = NAMELESS_EDIT_MARK in said and bool(_last_draw_id(messages))
            parsed = {
                "reply": f"假模型收到：{said[:40]}" if said else "假模型收到。",
                "continues": previous,
                "draw": None if "不用画" in said
                else ({"change": said} if previous or nameless else _brief()),
                "edits": _edits(said),
            }
        elif kind == "novelai_prompts":
            # Slots, like the real drawing model: the app assembles them (see
            # services/prompt.py) and a fake that still returned one finished
            # base_prompt would never exercise the assembly.
            blank = {"tags": "", "prose": ""}
            parsed = {
                "size": "portrait",
                "count": {"tags": "1girl, solo", "prose": ""},
                "style": dict(blank),
                "camera": dict(blank),
                "scene": dict(blank),
                "light": dict(blank),
                "other": dict(blank),
                "text": "",
                "settings": [],
                "characters": [
                    {"from": "", "use": "", "patches": [],
                     "look": {"tags": "looking at viewer", "prose": ""},
                     "moment": dict(blank),
                     "center_x": None, "center_y": None},
                ],
                "negative_prompt": "lowres, bad anatomy",
            }
        elif kind == "novelai_patch":
            # an edit: change one half of one slot and leave the rest to the merge
            parsed = {"other": {"prose": {"set": "redrawn by the stand-in"}}}
        elif kind == "library_lines":
            parsed = {"lines": _lines_for(_last_user(messages))}
        elif kind == "vallotton_reflection":
            n = next(self._reflections)
            parsed = {
                "reply": "假模型的检查结论。",
                "draw": {"change": "再亮一点"} if n < self._redraw_rounds else None,
            }
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


def _lines_for(text: str) -> list[dict]:
    """
    Lines for an entry's text, enough to show that a sweep wrote them FROM the
    text: the hair line follows one word of it.
    """
    hair = "brown hair" if "棕" in text else "black hair"
    return [{"part": "身份", "tags": "1girl"}, {"part": "头发", "tags": hair}]


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


def _edits(said: str) -> dict | None:
    """
    Propose a library entry when the turn asked for one to be remembered.

    The trigger is what the suite types, not anything in a prompt file. Titles
    come out of 「」 because that is how the suites name the thing they expect
    to see written back.
    """
    if not any(w in said for w in ("记下来", "记住", "记到")):
        return None
    titles = re.findall(r"「([^」]+)」", said)
    if not titles:
        return None
    # Scoped the way a model that read the prompt would: the project unless
    # the turn says this conversation. 「不说层」 leaves the field out, which
    # is the model forgetting, and the app has to have an answer for that.
    entry = {"title": titles[0], "content": said[-80:]}
    if "不说层" not in said:
        entry["scope"] = "conversation" if "本对话" in said else "project"
    return {"settings": [entry], "characters": []}


def _brief() -> dict:
    return {
        "change": "",
        "subjects": [{"who": "瓦洛东小姐", "outfit": "", "look": "银发少女",
                      "changes": "", "doing": "侧身站立", "where": ""}],
        "composition": "一个人物站在窗前",
        "scene": "室内，午后",
        "camera": "中景",
        "light": "",
        "style": "厚涂",
        "notes": "",
        "size": "portrait",
    }
