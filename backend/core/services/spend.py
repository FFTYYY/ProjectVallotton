"""Local usage accounting (LLM cost ticks, novelai image count)."""

from __future__ import annotations

import logging

from django.db import close_old_connections
from django.db.models import F
from django.utils import timezone

log = logging.getLogger(__name__)


def _bump(**deltas) -> None:
    from core.models import DailySpend

    try:
        close_old_connections()
        today = timezone.localdate()
        obj, _created = DailySpend.objects.get_or_create(day=today)
        DailySpend.objects.filter(pk=obj.pk).update(
            **{k: F(k) + v for k, v in deltas.items()}
        )
    except Exception as exc:  # noqa: BLE001 — accounting must never break the pipeline
        log.warning("spend accounting failed: %s", exc)


def record_llm_spend(ticks: int) -> None:
    """
    One model call happened, and it cost this much.

    Providers that do not price their responses pass 0 — the request still
    counts, and the quota panel shows their balance instead of a sum.
    """
    _bump(llm_ticks=max(ticks, 0), llm_requests=1)


def record_nai_images(count: int) -> None:
    _bump(nai_images=max(count, 0))
