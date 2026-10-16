"""
What every model provider has to offer, and the parts they can share.

The app asks for one thing — a JSON object matching a schema, optionally with
images in the conversation — and each provider answers it however its API
allows. What differs between them is not the request but the *capabilities*:
whether structured output is native or has to be asked for in prose, whether
the bill comes back with the response or has to be read off an account
balance, whether images are accepted at all. Those differences live in
`Capabilities` so the rest of the app can branch on a fact instead of on a
provider name.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from core.prompts import wording
from core import ui_text

log = logging.getLogger(__name__)

#: A chain of thought runs to tens of thousands of characters. It is worth
#: keeping to debug a turn with, not worth storing in full forever.
MAX_REASONING_CHARS = 20_000


def clip_reasoning(text: str) -> str:
    text = text or ""
    if len(text) <= MAX_REASONING_CHARS:
        return text
    return text[:MAX_REASONING_CHARS] + ui_text.REASONING_CLIPPED.format(
        chars=len(text) - MAX_REASONING_CHARS)


class LLMError(Exception):
    """
    Any failure talking to a model provider.

    `raw` is the answer that could not be used, when there was one. A failure
    that throws away the thing which would explain it can only ever be
    diagnosed by guessing — and an excerpt is not the thing: a JSON object
    that will not parse breaks at one character, and which character it was
    decides whether this is a stray line break, a full-width colon, or a
    sentence the model wrote before the object.
    """

    def __init__(self, message: str, raw: str = "", status: int | None = None):
        super().__init__(message)
        self.raw = raw
        #: the HTTP status, when the endpoint answered with one. None means
        #: nothing came back at all — a timeout, a refused connection, DNS.
        #: The distinction is the whole basis of Roads: an endpoint that
        #: answers is reachable, whatever it decided to say.
        self.status = status


@dataclass(frozen=True)
class Capabilities:
    """What this provider can actually do. Facts, not preferences."""

    #: response_format={"type": "json_schema"} is honoured. When False the
    #: schema is spelled out in the system prompt instead — see schema_prose.
    json_schema: bool
    #: image_url parts in a user message are understood.
    vision: bool
    #: reasoning_effort is honoured.
    reasoning_effort: bool
    #: every response carries what it cost, so spend can be summed locally.
    native_cost: bool
    #: the account's remaining credit can be read from the API.
    balance: bool


@dataclass
class Completion:
    """One structured answer, plus what it cost to get it."""

    parsed: dict
    usage: dict
    #: in units of 1e-10 USD, or None when the provider does not say
    cost_ticks: int | None = None
    #: how many answers had to be thrown away before this one was usable
    retries: int = 0
    #: the model's own reasoning, when it hands any back. Kept so 「检查」 can
    #: answer "why these tags" with the working rather than a guess.
    reasoning: str = ""


class Provider(ABC):
    """One model vendor, behind the only interface the app needs."""

    #: settings key and env value — "grok", "deepseek"
    name: str
    #: what the UI calls it
    label: str
    capabilities: Capabilities

    @abstractmethod
    def complete(
        self,
        *,
        system: str,
        messages: list[dict],
        schema: dict,
        timeout: float,
        effort: str | None = None,
        temperature: float | None = None,
        on_retry: Callable[[str], None] | None = None,
    ) -> Completion:
        """
        Run one structured call and return the parsed object.

        `temperature` is left alone unless a caller asks: the vendors' defaults
        are tuned for writing, which is what the lead model does. The drawing
        model translates, and asks for 0.

        `on_retry` is called with a short reason before each further attempt,
        so a caller that has a user waiting can say so. The provider knows
        nothing about who is waiting; the caller knows nothing about HTTP.

        """

    @abstractmethod
    def describe(self, *, system: str, messages: list[dict], timeout: float) -> str:
        """Free-text completion — used to describe an image and to name a chat."""

    def recheck_roads(self) -> list[str]:
        """
        Knock on any way to this model that is currently being stepped over.

        Called from the sweeper. A provider with one road has nothing to do
        here, which is why this is not abstract.
        """
        return []

    def roads_status(self) -> list[dict]:
        """Which ways in there are, and which are currently stepped over."""
        return []

    def step_over(self, name: str, *, over: bool) -> bool:
        """
        Mark a road as out, or take that back. Says whether it found the road.

        The point of doing this by hand: an endpoint that has stopped answering
        takes a full request timeout to discover, and the person watching the
        spinner already knows. This sets exactly the state that discovery would
        have set — nothing downstream can tell the difference, and the probe
        that puts a road back runs the same either way.
        """
        return False

    def balance(self) -> dict | None:
        """
        Remaining credit, when the provider exposes it.

        Returns {"ok": bool, ...} or None when this provider has no such
        endpoint — in which case the UI falls back to local accounting.
        """
        return None

    @property
    def draw_effort(self) -> str | None:
        """
        Reasoning effort for the drawing call, which is translation rather
        than judgement and rarely wants the lead model's setting.
        """
        return None

    # ---- context budget, read by the "this chat is getting long" notice ----
    @property
    @abstractmethod
    def context_limit(self) -> int: ...

    @property
    @abstractmethod
    def long_context_threshold(self) -> int: ...

    #: What passing the long-context mark actually means here, as the key of
    #: the ui_text sentence (core.services.notices renders it). Vendors differ
    #: on whether it means anything at all — one doubles its price, another
    #: simply has a big window — so the sentence the user reads belongs to the
    #: provider, not to the pipeline.
    long_context_key = "LONG_CONTEXT"


# --------------------------------------------------------------------------
# shared plumbing
# --------------------------------------------------------------------------


class _AnswerTooLong(Exception):
    """The body was still arriving when the wall clock ran out."""


def _read_in_time(
    client: httpx.Client, url: str, headers: dict, payload: dict, answer_seconds: float | None
) -> tuple[int, bytes]:
    """
    One POST, read to the end -- or to the deadline, whatever the socket does.

    The read timeout bounds the silence between two bytes, and a long answer is
    never silent: DeepSeek keeps a request alive with empty lines while the
    model thinks, so a call that has lost its way (one reasoned for seventeen
    minutes into max_tokens) is never cut off by it. Nor is one whose peer has
    stopped sending anything a timeout can see: a drawing call once hung for
    an hour on a connection that was neither closed nor silent enough.

    So the read runs on its own thread and the caller waits for it on the
    wall clock. Past the deadline the client is closed under the reader,
    which ends it, and the caller is told the answer ran away.
    """
    if answer_seconds is None:
        resp = client.post(url, headers=headers, json=payload)
        return resp.status_code, resp.content
    box: dict = {}

    def read() -> None:
        try:
            with client.stream("POST", url, headers=headers, json=payload) as resp:
                box["status"] = resp.status_code
                box["body"] = b"".join(resp.iter_bytes())
        except BaseException as exc:  # noqa: BLE001 -- re-raised on the caller's thread
            box["exc"] = exc

    reader = threading.Thread(target=read, daemon=True, name="llm-read")
    reader.start()
    reader.join(answer_seconds)
    if reader.is_alive():
        try:
            client.close()
        except Exception:  # noqa: BLE001 -- the reader is what this ends
            pass
        raise _AnswerTooLong()
    if "exc" in box:
        raise box["exc"]
    return box["status"], box["body"]


def post_with_retry(
    url: str,
    headers: dict,
    payload: dict,
    timeout: float,
    who: str,
    retries: int = 3,
    answer_seconds: float | None = None,
) -> dict:
    """
    POST JSON, retrying the statuses that are worth retrying.

    `answer_seconds` caps one answer on the wall clock (see _read_in_time). A
    capped answer is asked for once more -- the next draw is a fresh one and
    almost always of ordinary length -- and a second runaway is reported rather
    than waited out a third time.
    """
    last_err: Exception | None = None
    cut = 0
    for attempt in range(retries):
        try:
            with httpx.Client(timeout=httpx.Timeout(timeout, connect=15.0)) as client:
                status, body = _read_in_time(client, url, headers, payload, answer_seconds)
            if status in (429, 500, 502, 503) and attempt < retries - 1:
                log.warning("%s %s, retrying (%d)", who, status, attempt + 1)
                time.sleep(4 * (attempt + 1))
                continue
            if status != 200:
                raise LLMError(
                    f"{who} HTTP {status}: {body[:500].decode('utf-8', errors='replace')}",
                    status=status,
                )
            return json.loads(body)
        except _AnswerTooLong:
            cut += 1
            if cut == 1 and attempt < retries - 1:
                log.warning("%s has been answering for %.0fs, cutting it off and asking once more",
                            who, answer_seconds or 0)
                continue
            # answered, in the sense that matters to the roads: the endpoint is
            # fine, the request is what ran away
            raise LLMError(
                ui_text.MODEL_ANSWER_TOO_LONG.format(
                    name=who, minutes=round((answer_seconds or 0) / 60)),
                status=408,
            )
        except httpx.HTTPError as exc:
            last_err = exc
            if attempt < retries - 1:
                log.warning("%s network error, retrying: %s", who, exc)
                time.sleep(4 * (attempt + 1))
                continue
            raise LLMError(f"{who} network error: {exc}") from exc
    raise LLMError(f"{who} failed: {last_err}")


def parse_json_lenient(content: str, who: str) -> dict:
    """
    The object a model meant to send, even when it wrapped it in prose.

    Three ways a well-meant answer arrives unreadable, and all three are the
    model being sloppy rather than wrong:

      * it introduces its JSON, or fences it in backticks — the outermost
        braces recover both without encouraging either;
      * it writes a real newline inside a string instead of 
. This is the
        one that hurt: these fields hold several lines of Chinese describing a
        picture, and one stray line break made the whole answer unparseable.
        json's own `strict` switch is exactly about control characters inside
        strings, and off is the right setting for reading someone else's text;
      * both at once.

    What is NOT recovered is a truncated or genuinely malformed object. That
    is a different failure and it gets a different answer — see the retry in
    the DeepSeek provider, which tells the model what was wrong with the last
    one rather than asking again in the same words.
    """
    content = (content or "").strip()
    candidates = [content]
    start, end = content.find("{"), content.rfind("}")
    if start >= 0 and end > start:
        candidates.append(content[start : end + 1])
    for text in candidates:
        for strict in (True, False):
            try:
                got = json.loads(text, strict=strict)
            except json.JSONDecodeError:
                continue
            if isinstance(got, dict):
                return got
    # The answer rides along in `raw`, which the caller stores on the row for
    # 「检查」 to show. Putting it in the message as well only meant the bubble
    # printed three hundred characters of someone else's JSON at the user.
    log.warning("%s returned unparseable JSON: %s", who, content[:300])
    raise LLMError(ui_text.MODEL_UNREADABLE.format(name=who), raw=content)


def _skeleton(schema: dict, path: str = "", nullable=None, lists=None) -> object:
    """
    A JSON Schema rendered as an example of the shape it describes.

    One example cannot show two things, and both were being lost:

      * an array is drawn with exactly ONE element, which next to "match this
        shape" reads as "give me exactly one";
      * a field that may be null is drawn as its non-null branch, so `draw`
        and `edits` — whose null case is the ordinary one, meaning "no picture
        this turn" and "no library changes" — came out looking mandatory. The
        old comment here claimed null was "explained in the field list"; there
        was no field list.

    So the walk collects those two facts as it goes and schema_prose says them
    in words underneath. `nullable` and `lists` are out-parameters: passing
    them is how a caller asks for the notes.
    """
    if nullable is None:
        nullable = []
    if lists is None:
        lists = []
    t = schema.get("type")
    if "anyOf" in schema:
        branches = schema["anyOf"]
        real = [b for b in branches if b.get("type") != "null"]
        if len(real) != len(branches):
            # a scalar can carry the alternative inline; a shape cannot, so it
            # gets named in the note instead
            inner = _skeleton(real[0], path, nullable, lists) if real else None
            if isinstance(inner, str):
                return inner + " | null"
            if path:
                nullable.append(path)
            return inner
        return _skeleton(real[0], path, nullable, lists) if real else None
    if t == "object":
        return {
            k: _skeleton(v, f"{path}.{k}" if path else k, nullable, lists)
            for k, v in (schema.get("properties") or {}).items()
        }
    if t == "array":
        if path:
            cap = schema.get("maxItems")
            lists.append(
                wording.SCHEMA_PROSE_LIST_CAP.format(field=path, cap=cap) if cap else path
            )
        return [_skeleton(schema.get("items") or {}, f"{path}[]", nullable, lists)]
    if t == "integer" or t == "number":
        lo, hi = schema.get("minimum"), schema.get("maximum")
        return f"<{t}{f' {lo}..{hi}' if lo is not None and hi is not None else ''}>"
    if t == "boolean":
        return "<true|false>"
    if schema.get("enum"):
        return " | ".join(str(x) for x in schema["enum"])
    return "<string>"


# --------------------------------------------------------------------------
# One model, more than one road to it
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Road:
    """
    A way to reach a model: where to send the request, and as whom.

    A road is not a provider. The model at the other end is the same model,
    answering the same way, to the same request — which is the point. It means
    an answer cannot be traced to the road it came down, and nothing above this
    layer has to know there was a choice.
    """

    #: what to call it in a log
    name: str
    #: the chat/completions endpoint
    url: str
    #: what THIS endpoint calls the model; the only part of a request that
    #: differs between roads
    model: str
    headers: dict = field(default_factory=dict)
    #: fields this endpoint needs in every body to behave like the others.
    #: Routing and transport only — never anything that changes the question,
    #: because the moment a road alters what is asked, the answer depends on
    #: which road it came down and the layer above has to start caring.
    extra: dict = field(default_factory=dict)
    #: the smallest real request this road will accept, used to ask whether it
    #: is back. It goes to `url` with `headers` — the same door, the same key —
    #: because an endpoint next to the broken one answering proves nothing
    #: about the broken one. None means only the clock can reopen this road.
    probe: dict | None = None


class Roads:
    """
    Which roads are worth trying, and which just failed.

    Two things have to be true at once, and they pull against each other:

      * the preferred road is preferred — one outage must not move the app onto
        the fallback for the rest of the day;
      * finding out that an outage is over must not cost a user anything. A
        road that is down costs a full retry ladder to discover, around a
        minute, and having every so-many-th request pay that is the outage all
        over again, just rarer.

    So a road that fails is stepped over, and what brings it back is a probe on
    the sweeper's own timer (see jobs._sweep) rather than a user's request.

    The probe is the same request, shrunk — not a cheaper question asked
    somewhere else. A provider is several services behind one hostname, and the
    one that serves completions can be face-down while the one that reports a
    balance answers in 200 ms; a probe aimed at the second would call the road
    open and walk every turn straight back into the hole. A request costs a
    fraction of a cent and is the only thing that actually answers the question
    being asked.

    The clock is only a backstop for roads with no probe: a process that never
    sweeps would otherwise keep one blocked forever.
    """

    def __init__(self, blocked_for: float = 600.0):
        self.blocked_for = blocked_for
        self._blocked: dict[str, float] = {}

    def open(self, roads: list[Road]) -> list[Road]:
        """Those worth trying, in the caller's order of preference."""
        now = time.monotonic()
        usable = [r for r in roads if self._blocked.get(r.name, 0.0) <= now]
        # Everything is blocked. The blocks are a guess and the caller has a
        # real request to make, so try them all rather than refuse without
        # asking anyone.
        return usable or list(roads)

    def blocked(self, road: Road) -> bool:
        return self._blocked.get(road.name, 0.0) > time.monotonic()

    def block(self, road: Road) -> None:
        self._blocked[road.name] = time.monotonic() + self.blocked_for

    def clear(self, road: Road) -> None:
        self._blocked.pop(road.name, None)

    def until(self, road: Road) -> float:
        """Seconds left on this road's block, 0 when it is open."""
        return max(self._blocked.get(road.name, 0.0) - time.monotonic(), 0.0)

    def recheck(self, roads: list[Road], timeout: float = 20.0) -> list[str]:
        """
        Knock on every blocked door. Returns the names of the roads reopened.

        Called from the sweeper, so the cost of discovering that an outage is
        over is paid by a background tick rather than by whoever asks next.

        One attempt each, no retry ladder: this is a question, and "not yet" is
        a perfectly good answer to get back from it. The next sweep asks again.
        """
        reopened = []
        for road in roads:
            if not self.blocked(road) or road.probe is None:
                continue
            try:
                with httpx.Client(timeout=timeout, trust_env=False) as client:
                    resp = client.post(
                        road.url, headers=road.headers, json={**road.extra, **road.probe}
                    )
                if resp.status_code < 300:
                    self.clear(road)
                    reopened.append(road.name)
                else:
                    log.debug("%s still refusing: %s", road.name, resp.status_code)
            except Exception as exc:  # noqa: BLE001 — a probe never fails a sweep
                log.debug("%s still not answering: %s", road.name, str(exc)[:120])
        if reopened:
            log.warning("back on %s", "、".join(reopened))
        return reopened


#: Statuses that mean this endpoint will not serve us, though it is up. Going
#: elsewhere is the right move: another endpoint bills a different account and
#: holds a different rate limit, so it may well take the same request.
_REFUSES_US = frozenset({401, 402, 403, 429})


def _impassable(exc: LLMError) -> bool:
    """
    Whether this failure is the road, rather than the request.

    Nothing came back at all — a timeout, a refused connection — and the road
    is out. An endpoint that answered is reachable, and most of what it can
    answer is about the request: a 400 says the body is wrong and the identical
    body is wrong everywhere, so carrying it to the next road buys a second
    400 and, worse, leaves a working road marked as broken behind us.

    5xx is the exception among answers: the endpoint is admitting the fault is
    its own, which is exactly what another one might not share.
    """
    if exc.status is None:
        return True
    return exc.status in _REFUSES_US or exc.status >= 500


def ask_any(
    roads: list[Road],
    known: Roads,
    send: Callable[[Road, dict, bool], dict],
    payload: dict,
) -> dict:
    """
    Put one request down whichever road answers.

    `send` is passed in rather than called from here: this knows which roads
    are worth trying, and nothing else. How a request is actually made — the
    retry ladder, the timeout, the error the caller expects back — already
    exists at the call site and does not want a second copy living in here.

    The road it went down is not returned. Nothing upstream may branch on it,
    because the moment something does, the answer depends on the route and the
    fallback stops being invisible.

    Only a road being out moves us along. An answer that arrives and cannot be
    used is the model's answer, and it would be the same model's answer down
    the other road — that is the caller's to retry, not a reason to go
    somewhere else.

    `send` is told whether it is on the last road, because how hard to push
    depends on whether there is anywhere else to go. On the last road, push:
    nothing else is coming. Before then, do not — retrying a road that just
    timed out means waiting out the same timeout again for the same reason,
    while the road next to it is sitting there answering. Trying the next one
    IS the retry, and a better one.

    See `_impassable` for what counts as the road being out, as opposed to the
    road answering with something we did not want.
    """
    usable = known.open(roads)
    last: LLMError | None = None
    for i, road in enumerate(usable):
        try:
            data = send(
                road,
                {**road.extra, **payload, "model": road.model},
                i == len(usable) - 1,
            )
        except LLMError as exc:
            if not _impassable(exc):
                raise
            last = exc
            known.block(road)
            log.warning("%s is not answering (%s)", road.name, str(exc)[:200])
            continue
        known.clear(road)
        return data
    raise last or LLMError("no road to the model is answering")



def system_for(system: str, schema: dict, *, enforced: bool) -> str:
    """
    The system prompt for one request, given how its shape will be held.

    Two ways a reply can be made to fit a schema: the vendor enforces it, or
    the prompt spells it out. They are the same decision seen from two sides,
    so they are made together, here — a request that is not enforced carries
    the shape in words, and nothing else has to remember that.

    It was not always one decision, and the gap had teeth: xAI asks for
    enforcement and falls back to plain JSON mode when a deployment refuses it,
    and that fallback rebuilt the request without adding the words. The model
    was then left to guess the shape from whatever the prompt file happened to
    say — which for a reflection turn (lead.txt's system prompt, a different
    schema) describes a different answer entirely.
    """
    return system if enforced else system + schema_prose(schema)


def schema_prose(schema: dict) -> str:
    """
    The system-prompt block that stands in for native structured output.

    Providers whose JSON mode is only "emit some object" need the shape spelled
    out, and DeepSeek's docs additionally require the literal word "json" and a
    sample structure to appear in the prompt — both are here on purpose. The
    skeleton is generated from the same schema the strict providers are given,
    so the two can never drift apart.
    """
    nullable: list[str] = []
    lists: list[str] = []
    body = json.dumps(
        _skeleton(schema.get("schema") or {}, "", nullable, lists),
        ensure_ascii=False,
        indent=2,
    )
    notes = [wording.SCHEMA_PROSE_OUTRO]
    if lists:
        notes.append(wording.SCHEMA_PROSE_LISTS.format(fields="、".join(lists)))
    if nullable:
        notes.append(wording.SCHEMA_PROSE_NULLABLE.format(fields="、".join(nullable)))
    return (
        "\n\n"
        + wording.SCHEMA_PROSE_HEADING
        + "\n"
        + wording.SCHEMA_PROSE_INTRO
        + "\n"
        + body
        + "\n"
        + "\n".join(notes)
    )
