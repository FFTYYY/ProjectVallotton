"""
Quota / usage reporting for the frontend.

- NovelAI: live subscription info from image.novelai.net (cached ~5 min).
- The LLM: whatever the active provider can actually tell us. Some price
  every response, so their spend is summed locally (xAI has no usage endpoint
  for API keys, so that is all there is). Some expose the account balance
  instead, which is better information and needs no accounting. The panel
  reports whichever exists rather than pretending every vendor is xAI.
"""

from __future__ import annotations

import logging
import threading
import time

import httpx
from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from . import providers

log = logging.getLogger(__name__)

_CACHE_TTL = 300.0
_cache_lock = threading.Lock()
_nai_cache: tuple[float, dict] | None = None
_balance_cache: tuple[str, float, dict | None] | None = None

TIER_NAMES = {0: "Paper", 1: "Tablet", 2: "Scroll", 3: "Opus"}


def _fetch_novelai() -> dict:
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(
                "https://image.novelai.net/user/subscription",
                headers={"Authorization": f"Bearer {settings.NOVELAI_API_KEY}"},
            )
        if resp.status_code != 200:
            return {"ok": False, "error": f"HTTP {resp.status_code}"}
        data = resp.json()
        usage = data.get("usage") or {}
        return {
            "ok": True,
            "tier": TIER_NAMES.get(data.get("tier"), str(data.get("tier"))),
            "active": bool(data.get("active")),
            "expires_at": data.get("expiresAt"),
            # NovelAI's rolling usage meter: 100 = full capacity available.
            "usage_percent": usage.get("percent"),
            "seconds_until_next_percent": usage.get("timeUntilNextPercent"),
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("novelai quota fetch failed: %s", exc)
        return {"ok": False, "error": str(exc)[:200]}


def get_status() -> dict:
    global _nai_cache
    with _cache_lock:
        cached = _nai_cache
    if cached and time.monotonic() - cached[0] < _CACHE_TTL:
        nai = cached[1]
    else:
        nai = _fetch_novelai()
        if nai.get("ok") or not cached:
            with _cache_lock:
                _nai_cache = (time.monotonic(), nai)
        else:  # keep last good data on transient failure
            nai = cached[1]

    from core.models import DailySpend

    today = timezone.localdate()
    row = DailySpend.objects.filter(day=today).first()
    totals = DailySpend.objects.aggregate(
        ticks=Sum("llm_ticks"), reqs=Sum("llm_requests"), imgs=Sum("nai_images")
    )
    provider = providers.active()
    llm = {
        "provider": provider.name,
        "label": provider.label,
        "model": _model_name(provider),
        # the frontend's context meter reads these rather than hard-coding a
        # window that is only right for one vendor
        "context_limit": provider.context_limit,
        "long_context_threshold": provider.long_context_threshold,
        "today_requests": row.llm_requests if row else 0,
        "total_requests": totals["reqs"] or 0,
        # only meaningful for a provider that prices its responses; the others
        # would report a confident $0.00, which is worse than saying nothing
        "priced": provider.capabilities.native_cost,
        "today_usd": round((row.llm_ticks if row else 0) * 1e-10, 4),
        "total_usd": round((totals["ticks"] or 0) * 1e-10, 4),
        "balance": _balance(provider) if provider.capabilities.balance else None,
    }
    return {
        "novelai": nai,
        "llm": llm,
        "images": {
            "today": row.nai_images if row else 0,
            "total": totals["imgs"] or 0,
        },
    }


def _model_name(provider) -> str:
    """Which model the provider is actually pointed at, for the panel's caption."""
    key = f"{provider.name.upper()}_MODEL"
    if provider.name == "grok":
        key = "GROK_MODEL"
    return getattr(settings, key, "") or ""


def _balance(provider) -> dict | None:
    """Cached account balance — a live HTTP call per page load would be rude."""
    global _balance_cache
    with _cache_lock:
        cached = _balance_cache
    if cached and cached[0] == provider.name and time.monotonic() - cached[1] < _CACHE_TTL:
        return cached[2]
    fresh = provider.balance()
    if (fresh or {}).get("ok") or not cached or cached[0] != provider.name:
        with _cache_lock:
            _balance_cache = (provider.name, time.monotonic(), fresh)
        return fresh
    return cached[2]  # keep the last good reading through a transient failure
