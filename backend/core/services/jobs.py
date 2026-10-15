"""
Background job runner.

Generation runs in a daemon thread inside the web process (single-user app —
no need for celery). Job state lives entirely in the Message row, so any
worker process can observe/cancel it. The reaper marks jobs whose heartbeat
went silent (e.g. backend restarted mid-generation) as failed.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from core.models import Message
from core import ui_text

from . import continuous, library_lines, pipeline, providers

log = logging.getLogger(__name__)


def _wrap(fn, message_id) -> None:
    try:
        close_old_connections()
        fn(message_id)
    finally:
        close_old_connections()


def start_generation(message_id) -> None:
    threading.Thread(
        target=_wrap, args=(pipeline.run_generation, message_id), daemon=True
    ).start()


def start_redraw(message_id) -> None:
    threading.Thread(
        target=_wrap, args=(pipeline.run_redraw, message_id), daemon=True
    ).start()


def _conclude(ids: list, error: str) -> int:
    """
    Write the outcome of turns whose worker is gone, and tell their runs.

    A turn that already produced pictures is not a failure, however it ended.
    Concluding it as one threw the pictures away — the run counted a failure,
    spent a round and queued a replacement, which then archived the very row
    the pictures were hanging on.
    """
    if not ids:
        return 0
    kept = set(
        Message.objects.filter(id__in=ids, attachments__kind="generated")
        .values_list("id", flat=True)
    )
    Message.objects.filter(id__in=list(kept)).update(status="done", error="", phase="")
    n = Message.objects.filter(id__in=[i for i in ids if i not in kept]).update(
        status="error", error=error, phase=""
    )
    # Nudge the runs these belonged to rather than leave them to the next tick.
    # Only a nudge: continuous.tick() reaches the same conclusion from the same
    # rows, so nothing here is load-bearing.
    for conv_id in set(
        Message.objects.filter(id__in=ids).values_list("conversation_id", flat=True)
    ):
        try:
            continuous.reconcile(conv_id)
        except Exception:  # noqa: BLE001 — one bad run must not block the sweep
            log.exception("could not reconcile the run in %s", conv_id)
    return n


def adopt_orphans() -> int:
    """
    Every turn still pending when this process starts belonged to one that is
    gone.

    Not a guess. This process has just come up and is working on nothing, and
    it is the only one that serves generation — see the single gunicorn worker
    in entrypoint.sh — so a row that says "being made" is a row whose maker no
    longer exists. That is a fact about the world, not an inference from a
    timestamp, and it is the common case by far: the backend was restarted.

    It is the reason a restart does not cost three minutes of waiting for the
    heartbeat to go stale. What is left to the clock below is only the case a
    clock can actually answer — a worker that is still running and stuck.
    """
    ids = list(
        Message.objects.filter(status="pending").values_list("id", flat=True)
    )
    if not ids:
        return 0
    n = _conclude(ids, ui_text.JOB_INTERRUPTED)
    log.warning("adopted %d turn(s) left pending by a previous process", len(ids))
    return n


def reap_stale_jobs(conversation_id=None) -> int:
    """
    Fail pending turns whose heartbeat stopped.

    The one judgement here that a database cannot make for itself: is the
    thread that claimed this row still working? Nothing in the database can
    answer that, so it is answered with a lease — the worker refreshes
    heartbeat_at on every stage and every 20s besides, and a row that has gone
    quiet for JOB_STALE_SECONDS is taken to be abandoned.

    A restart does not come through here; adopt_orphans() has already dealt
    with that, structurally. What is left is the rarer case of a worker that is
    alive and wedged, which is genuinely a question about time.
    """
    cutoff = timezone.now() - timedelta(seconds=settings.JOB_STALE_SECONDS)
    qs = Message.objects.filter(status="pending", heartbeat_at__lt=cutoff)
    if conversation_id:
        qs = qs.filter(conversation_id=conversation_id)
    stale = [m.id for m in qs.only("id")]
    if not stale:
        return 0
    n = _conclude(stale, ui_text.JOB_INTERRUPTED_MAYBE)
    log.warning("reaped %d stale pending turn(s) of %d", n, len(stale))
    return n


def _sweep() -> None:
    """
    The reaper on a clock, for when nobody is looking.

    It used to run only when a conversation was loaded, which is to say only
    when a browser was open. That is fine for showing the user a failure they
    are waiting for, and useless for a run of ten pictures they started and
    walked away from: the worker dies, the row stays pending, and the run waits
    for it forever.
    """
    # before the first sleep: a restart should not cost a whole stale window
    try:
        close_old_connections()
        adopt_orphans()
    except Exception:  # noqa: BLE001 — a bad start must not kill the sweeper
        log.exception("could not adopt orphaned turns at start-up")
    finally:
        close_old_connections()

    while True:
        time.sleep(settings.JOB_SWEEP_SECONDS)
        try:
            close_old_connections()
            reap_stale_jobs()
            # THE mechanism for continuous mode, not a backstop: every live run
            # is looked at, and whatever should happen next happens. The nudge
            # at the end of a turn only makes it sooner.
            continuous.tick()
        except Exception:  # noqa: BLE001 — a sweeper that dies is worse than a slow one
            log.exception("job sweep failed")
        finally:
            close_old_connections()


def _patrol() -> None:
    """
    The line writer on its own clock (services/library_lines.py).

    Not a few lines inside _sweep: a sweep that writes lines makes model
    calls, and a model call can hang for as long as the vendor lets it. It
    once held the reaper up for an hour, so a turn whose worker was stuck sat
    pending the whole time. The reaper must never wait on a model.
    """
    while True:
        time.sleep(settings.JOB_SWEEP_SECONDS)
        try:
            close_old_connections()
            library_lines.sweep()
        except Exception:  # noqa: BLE001 — the next sweep is a minute away
            log.exception("line patrol failed")
        finally:
            close_old_connections()


def _probe_roads() -> None:
    """
    Ask a road that is being stepped over whether it is back yet.

    Its own thread rather than a few lines inside _sweep, for one reason: a
    probe to somewhere that is down blocks until it times out, and the sweep
    loop is where continuous mode actually advances. Continuous runs must not
    tick late because an unrelated vendor is slow to refuse a connection.

    Nothing here knows what a road is. It asks the provider, and a provider
    with a single way to reach its model answers with an empty list and this
    thread goes back to sleep.
    """
    while True:
        time.sleep(settings.LLM_ROAD_PROBE_SECONDS)
        try:
            providers.active().recheck_roads()
        except Exception:  # noqa: BLE001 — a probe that fails is just "not yet"
            log.exception("road probe failed")


_sweeper_started = False


def start_sweeper() -> None:
    """Called once from AppConfig.ready(). Idempotent."""
    global _sweeper_started
    if _sweeper_started or not settings.JOB_SWEEP_SECONDS:
        return
    _sweeper_started = True
    threading.Thread(target=_sweep, daemon=True, name="job-sweeper").start()
    threading.Thread(target=_patrol, daemon=True, name="line-patrol").start()
    log.info("job sweeper started (every %ss)", settings.JOB_SWEEP_SECONDS)
    if settings.LLM_ROAD_PROBE_SECONDS:
        threading.Thread(target=_probe_roads, daemon=True, name="road-probe").start()
