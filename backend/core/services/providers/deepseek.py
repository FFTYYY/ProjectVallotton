"""
DeepSeek. Same wire format as the others, three differences that matter.

  - No json_schema: its JSON mode only promises *an* object, so the shape goes
    into the system prompt via schema_prose — which is also where the two
    things its docs require are met, the literal word "json" and a sample
    structure.
  - The usage block carries no price, so spend cannot be summed from
    responses. It does expose the account balance, which is better anyway.
  - Its docs warn that content comes back empty now and then. That is retried
    here rather than surfaced as a failed turn.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import httpx
from django.conf import settings

from core import ui_text

from .base import (
    Capabilities,
    Completion,
    LLMError,
    Provider,
    Road,
    Roads,
    ask_any,
    clip_reasoning,
    parse_json_lenient,
    post_with_retry,
    system_for,
)

log = logging.getLogger(__name__)


API_BASE = "https://api.deepseek.com"

#: Which roads are currently being stepped over. Module-level because it is a
#: fact about the outside world, not about any one request: what the last call
#: learned is the only thing that saves the next one from learning it again.
_known = Roads(blocked_for=settings.LLM_ROAD_BLOCKED_SECONDS)

#: What to call each road where a person will read it.
LABELS = ui_text.ROUTE_LABELS

#: What to call each road to someone who is not reading this file.
class DeepSeekProvider(Provider):
    name = "deepseek"
    label = ui_text.PROVIDER_DEEPSEEK
    capabilities = Capabilities(
        json_schema=False,
        vision=True,
        reasoning_effort=True,
        native_cost=False,
        balance=True,
    )

    @property
    def context_limit(self) -> int:
        return settings.DEEPSEEK_CONTEXT_LIMIT

    @property
    def long_context_threshold(self) -> int:
        return settings.DEEPSEEK_LONG_CONTEXT_THRESHOLD

    @property
    def draw_effort(self) -> str | None:
        return settings.DEEPSEEK_DRAW_REASONING_EFFORT

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        }

    def _roads(self) -> list[Road]:
        """
        Where this model can be reached, best first.

        Best first is not politeness — the official road is the cheaper one, so
        the app belongs on it whenever it is there. A road with no key is not a
        road, so with OPENROUTER_API_KEY unset this is the single road it has
        always been.

        Both take the same request. The only thing that differs is what each
        endpoint calls the model, which is why ask_any is handed the road and
        swaps that one field: if anything else differed, the answer would
        depend on the road, and then everything above here would have to know
        which road it got.
        """
        roads: list[Road] = []
        if settings.DEEPSEEK_API_KEY:
            roads.append(
                Road(
                    name="deepseek",
                    url=f"{API_BASE}/chat/completions",
                    model=settings.DEEPSEEK_MODEL,
                    headers=self._headers(),
                    probe=self._probe(settings.DEEPSEEK_MODEL),
                )
            )
        if settings.OPENROUTER_API_KEY:
            roads.append(
                Road(
                    name="openrouter",
                    url=f"{settings.OPENROUTER_BASE}/chat/completions",
                    model=settings.OPENROUTER_MODEL,
                    headers={
                        "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    # OpenRouter is not one host: it fans this model out over
                    # ~18 of them, and they do not all advertise the same
                    # parameters — two (Relace, Io Net) leave response_format
                    # out of supported_parameters, and one unconstrained call
                    # here did route to Relace. What such a host actually does
                    # with the field has not been measured, and this exists so
                    # that it never has to be: routing is restricted to hosts
                    # that accept every field sent, which is the only way a
                    # road can be a road rather than a second provider — the
                    # request is answerable to the same degree whichever one
                    # takes it.
                    #
                    # Measured 2026-09-15, of the hosts this leaves eligible:
                    # smallest context 1,000,000 and smallest max output
                    # 131,072 — above DEEPSEEK_CONTEXT_LIMIT and still above
                    # DEEPSEEK_MAX_TOKENS, so capacity survives the switch too.
                    # The margin is now 3,072 tokens rather than the 67,072 it
                    # was: DEEPSEEK_MAX_TOKENS was raised to the API's own
                    # default for max reasoning effort.
                    # That part is luck rather than guarantee: BaseTen caps
                    # output at 32,768 and is only excluded because it happens
                    # not to take response_format. If either of those settings
                    # is raised, or a small host adds the parameter, this is
                    # the line that stops being enough.
                    extra={"provider": {"require_parameters": True}},
                    probe=self._probe(settings.OPENROUTER_MODEL),
                )
            )
        return roads

    @staticmethod
    def _probe(model: str) -> dict:
        """
        The smallest completion this endpoint will answer.

        A completion, and not the balance endpoint next door, because those are
        two services behind one hostname and the outage this exists for took
        down only one of them. Asking the one that still works would reopen the
        road and put every turn back into the hole it just climbed out of.

        One token out, and nothing else set. Every extra field is one more way
        to earn a 400 from an endpoint that is actually fine, and a probe that
        can only ever fail would leave the road blocked forever — the failure
        it exists to prevent, arrived at from the other side.
        """
        return {
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
        }

    def recheck_roads(self) -> list[str]:
        return _known.recheck(self._roads())

    def roads_status(self) -> list[dict]:
        roads = self._roads()
        open_now = [r.name for r in _known.open(roads)]
        return [
            {
                "name": r.name,
                "label": LABELS.get(r.name, r.name),
                "model": r.model,
                "stepped_over": _known.blocked(r),
                "seconds_left": round(_known.until(r)),
                # the one a request would take right now, so the page can say
                # what is happening rather than what is configured
                "in_use": bool(open_now) and r.name == open_now[0],
            }
            for r in roads
        ]

    def step_over(self, name: str, *, over: bool) -> bool:
        for road in self._roads():
            if road.name != name:
                continue
            _known.block(road) if over else _known.clear(road)
            log.warning("%s marked %s by hand", name, "out" if over else "back")
            return True
        return False

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
        head = [
            {
                "role": "system",
                # never enforced here — see the class docstring
                "content": system_for(system, schema, enforced=False),
            }
        ]
        payload = {
            # each road has its own name for it; ask_any swaps this field and
            # nothing else
            "model": settings.DEEPSEEK_MODEL,
            "response_format": {"type": "json_object"},
            # reasoning tokens count against this, and a truncated answer is a
            # broken one — the JSON simply stops mid-string
            "max_tokens": settings.DEEPSEEK_MAX_TOKENS,
        }
        effort = settings.DEEPSEEK_REASONING_EFFORT if effort is None else effort
        if effort:
            payload["reasoning_effort"] = effort
        if temperature is not None:
            payload["temperature"] = temperature

        def send(road: Road, body: dict, last_road: bool) -> dict:
            return post_with_retry(
                road.url, road.headers, body, timeout, road.name,
                retries=3 if last_road else 1,
                answer_seconds=settings.LLM_ANSWER_SECONDS,
            )

        def ask(history: list[dict]) -> dict:
            return ask_any(self._roads(), _known, send, {**payload, "messages": head + history})

        # One retry budget covering every way an answer can be unusable: empty
        # content (which its docs warn about outright), a truncated reply, and
        # JSON that will not parse. They are the same problem from the caller's
        # side — nothing came back that can be used — so they share a budget
        # rather than each quietly getting their own.
        # One retry budget covering every way an answer can be unusable, and
        # every attempt is the SAME request: models sample, so asking again is
        # a fresh draw rather than a repeat. Nothing is added to the prompt
        # between attempts — an attempt that was told something the first one
        # was not is no longer evidence that the first one can work.
        attempts = 1 + max(settings.DEEPSEEK_RETRIES, 0)
        last: LLMError | None = None
        reasoning = ""
        for attempt in range(attempts):
            if attempt:
                reason = str(last)
                log.warning("%s retrying (%d/%d): %s", self.name, attempt, attempts - 1, reason)
                if on_retry:
                    on_retry(reason)
            data = ask(messages)
            choice = (data.get("choices") or [{}])[0]
            usage = data.get("usage") or {}
            message = choice.get("message") or {}
            content = message.get("content") or ""
            # The model's own working, kept so 「检查」 can answer "why these
            # tags" with what it actually thought rather than a guess.
            #
            # Two names for one thing: DeepSeek's own API calls it
            # reasoning_content, the OpenAI-compatible shape OpenRouter serves
            # calls it reasoning, and each leaves the other null. Reconciled
            # here because this is the layer that knows there is more than one
            # way in — anywhere higher and 「检查」 would quietly come back empty
            # for the hours the app spends on the second road, which is exactly
            # the kind of difference a road is not allowed to make.
            reasoning = (
                message.get("reasoning_content") or message.get("reasoning") or ""
            )

            if choice.get("finish_reason") == "length":
                # Thinking and answering share this budget. Which lever to pull
                # is named here because it is a setting, not a fluke: a request
                # that leaves no room for the answer will keep not leaving room
                # however many times it is run.
                # The lever to pull is a setting, so it goes to the log; the
                # bubble gets one line.
                log.warning(
                    "%s hit max_tokens=%s at reasoning_effort=%s — raise "
                    "DEEPSEEK_MAX_TOKENS or lower DEEPSEEK_REASONING_EFFORT",
                    self.name, payload["max_tokens"],
                    payload.get("reasoning_effort") or "default",
                )
                last = LLMError(
                    ui_text.MODEL_OUT_OF_TOKENS.format(name=self.label)
                )
                continue
            if not content.strip():
                # Empty content has several causes and they want different
                # answers, so say which one this was. Before this they all read
                # as "returned empty content", and a run where 5 of 27 calls
                # came back empty could not be told apart from a run that was
                # merely being filtered or throttled.
                #
                # The API's own finish_reason list is stop / length /
                # content_filter / tool_calls / insufficient_system_resource /
                # aborted. `length` is handled above; of the rest only `stop`
                # is the documented "API 有概率会返回空的 content", and the
                # others are the backend telling us something specific.
                # Which kind of empty it was decides what to do about it, and
                # the API has several: `stop` is the one its JSON mode is
                # documented to produce now and then, while content_filter,
                # insufficient_system_resource and aborted each mean something
                # the retry cannot fix. All of that is for the log.
                log.warning(
                    "%s returned no content: finish_reason=%s reasoning_tokens=%s",
                    self.name,
                    choice.get("finish_reason") or "stop",
                    (usage.get("completion_tokens_details") or {}).get(
                        "reasoning_tokens"
                    ) or 0,
                )
                last = LLMError(ui_text.MODEL_EMPTY.format(name=self.label))
                continue
            try:
                parsed = parse_json_lenient(content, self.name)
            except LLMError as exc:
                last = exc
                continue

            return Completion(
                parsed=parsed,
                usage={
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                },
                cost_ticks=None,
                retries=attempt,
                reasoning=clip_reasoning(reasoning),
            )

        raise last or LLMError(
            ui_text.MODEL_NOTHING_USABLE.format(name=self.label)
        )

    def describe(self, *, system: str, messages: list[dict], timeout: float) -> str:
        payload = {
            # each road has its own name for it; ask_any swaps this field and
            # nothing else
            "model": settings.DEEPSEEK_MODEL,
            "messages": [{"role": "system", "content": system}] + messages,
            "max_tokens": 4000,
            # plain prose, and something is waiting on it — no thinking budget
            "reasoning_effort": "none",
        }
        data = ask_any(
            self._roads(),
            _known,
            lambda road, body, last: post_with_retry(
                road.url, road.headers, body, timeout, road.name,
                retries=3 if last else 1,
            ),
            payload,
        )
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""

    def balance(self) -> dict | None:
        """USD credit left on the key — shown instead of a running local bill."""
        try:
            with httpx.Client(timeout=20.0) as client:
                resp = client.get(f"{API_BASE}/user/balance", headers=self._headers())
            if resp.status_code != 200:
                return {"ok": False, "error": f"HTTP {resp.status_code}"}
            data = resp.json()
            usd = next(
                (b for b in data.get("balance_infos") or [] if b.get("currency") == "USD"),
                None,
            )
            if not usd:
                return {"ok": False, "error": "no USD balance on this key"}
            return {
                "ok": True,
                "available": bool(data.get("is_available")),
                "currency": "USD",
                "balance": float(usd.get("total_balance") or 0),
            }
        except Exception as exc:  # noqa: BLE001 — a quota panel must never break a page
            log.warning("deepseek balance failed: %s", exc)
            return {"ok": False, "error": str(exc)[:200]}
