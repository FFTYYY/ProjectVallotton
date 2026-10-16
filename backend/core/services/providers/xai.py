"""xAI (Grok). Native structured output, and it bills per response."""

from __future__ import annotations

from collections.abc import Callable

from django.conf import settings
from core import ui_text

from .base import (
    Capabilities,
    Completion,
    LLMError,
    Provider,
    clip_reasoning,
    parse_json_lenient,
    post_with_retry,
    system_for,
)

API_URL = "https://api.x.ai/v1/chat/completions"


class GrokProvider(Provider):
    name = "grok"
    label = ui_text.PROVIDER_GROK
    capabilities = Capabilities(
        json_schema=True,
        vision=True,
        reasoning_effort=True,
        # every response carries cost_in_usd_ticks, so spend is summed locally
        native_cost=True,
        # xAI exposes no balance endpoint for API keys
        balance=False,
    )

    @property
    def context_limit(self) -> int:
        return settings.GROK_CONTEXT_LIMIT

    @property
    def long_context_threshold(self) -> int:
        return settings.GROK_LONG_CONTEXT_THRESHOLD

    @property
    def draw_effort(self) -> str | None:
        return settings.GROK_DRAW_REASONING_EFFORT

    # xAI prices double past this mark, which is worth saying plainly
    long_context_key = "LONG_CONTEXT_PRICED"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {settings.GROK_API_KEY}",
            "Content-Type": "application/json",
        }

    def complete(
        self,
        *,
        system: str,
        messages: list[dict],
        schema: dict,
        timeout: float,
        effort: str | None = None,
        temperature: float | None = None,
        # structured output is native here, so there is nothing to retry for
        on_retry: Callable[[str], None] | None = None,
    ) -> Completion:
        def build(enforced: bool, history: list[dict]) -> dict:
            """One request, built the same way whichever mode it ends up in."""
            payload = {
                "model": settings.GROK_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": system_for(system, schema, enforced=enforced),
                    }
                ]
                + history,
                "response_format": (
                    {"type": "json_schema", "json_schema": schema}
                    if enforced
                    else {"type": "json_object"}
                ),
                "max_tokens": settings.GROK_MAX_TOKENS,
            }
            # Set here rather than bolted on afterwards: the json_schema
            # fallback below builds a second request, and the pair that has to
            # carry the same settings is exactly the pair that drifts.
            asked_effort = settings.GROK_REASONING_EFFORT if effort is None else effort
            if asked_effort:
                payload["reasoning_effort"] = asked_effort
            if temperature is not None:
                payload["temperature"] = temperature
            return payload

        #: Once a deployment has refused json_schema it will refuse it again, so
        #: the fallback sticks for the rest of this call.
        enforced = True

        def ask(history: list[dict]) -> dict:
            nonlocal enforced
            try:
                return post_with_retry(
                    API_URL, self._headers(),
                    build(enforced, history), timeout, self.name,
                    answer_seconds=settings.LLM_ANSWER_SECONDS,
                )
            except LLMError as exc:
                # some deployments reject json_schema — fall back to plain json
                # mode. Not enforced any more, so the shape has to be said in
                # words — and it is the same builder that says it, so this
                # cannot be the half-built request it used to be.
                if not enforced:
                    raise
                if "response_format" not in str(exc) and "json_schema" not in str(exc):
                    raise
                enforced = False
                return post_with_retry(
                    API_URL, self._headers(),
                    build(False, history), timeout, self.name,
                    answer_seconds=settings.LLM_ANSWER_SECONDS,
                )

        data = ask(messages)

        usage = data.get("usage") or {}
        message = (data.get("choices") or [{}])[0].get("message", {})
        content = message.get("content", "")
        return Completion(
            parsed=parse_json_lenient(content, self.name),
            usage={
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
            },
            cost_ticks=int(usage.get("cost_in_usd_ticks") or 0),
            # xAI hands this back only on some deployments; absent is normal
            reasoning=clip_reasoning(message.get("reasoning_content") or ""),
        )

    def describe(self, *, system: str, messages: list[dict], timeout: float) -> str:
        payload = {
            "model": settings.GROK_TITLE_MODEL or settings.GROK_MODEL,
            "messages": [{"role": "system", "content": system}] + messages,
            "max_tokens": 2000,
        }
        data = post_with_retry(API_URL, self._headers(), payload, timeout, self.name)
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
