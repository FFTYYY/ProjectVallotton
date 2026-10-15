"""
Continuous mode: one instruction, a run of pictures.

The user picks a number and a quick phrase. After each turn finishes, the app
sends that phrase again on their behalf until the number is used up. Nothing
about it reaches a model — it reads the same words it would have read if they
had been typed.

**The run lives on the server**, because a run of ten pictures is the kind of
thing you start and walk away from. It has to survive the page, the tab, and
the machine you started it on.

---

**It is reconciled, not driven.**

`continuous_run` on the conversation says what the user *wants*: this many more
pictures, from this instruction, with these settings. Nothing in it describes
what is happening right now. `reconcile()` is the only thing that decides what
happens next, and it decides by looking:

    a turn of this conversation is still pending      → leave it alone
    a finished turn nobody has counted yet            → count it, then decide
    the run still wants more                          → start the next one
    spent, stopped, or out of retries                 → close it

That reads the world instead of remembering it, which is the whole point. The
previous shape advanced the run from a callback at the end of the pipeline, and
kept a sweeper around to catch the times that callback did not happen — two
mechanisms, one patching the gaps in the other. Every bug this feature ever had
lived in the seam between them: who counts a turn, who says the run ended, and
above all *is it still alive*, which was inferred from a heartbeat and a
timestamp. A system that infers liveness from time always has a window where it
infers wrong.

Here there is one mechanism. `tick()` runs it on a clock; `reconcile()` is also
called the moment a turn ends, but only as a nudge — if that call never
happens, the next tick does exactly the same thing a few seconds later. A
missed nudge costs latency, never correctness.

The ending that used to need its own machinery — the backend stopping between
one turn and the next — stopped existing. A run that wants more and has nothing
running is not stranded. It is a run whose next turn has not been started yet,
which is the ordinary case and takes the ordinary path.

**Every run is still a pair of brackets.** `arm` writes the opening note,
`_close` writes the summary, and `_close` is the only way the run is ever
cleared — state and note in one transaction, so they cannot come apart.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from core.models import Conversation, Message, QuickPhrase
from core import ui_text

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ brackets


def _instruction(conversation: Conversation) -> str:
    """
    The words this run will send again, resolved here and nowhere else.

    They used to be sent up by the browser, which knew them only if the right
    rail had been mounted — and on a phone that rail starts closed, so the
    quick phrases were often never loaded. The run was then armed with an empty
    instruction, which meant it was not armed at all: one picture, no note, no
    error, nothing to tell you the ten you asked for had turned into one.

    The server never had that problem. `continuous_phrase_id` is on the
    conversation; the phrase is a row. Asking a client to hand back a fact the
    database already holds is the shape of every bug this feature has had.

    Nothing chosen yet is not a failure either: the first phrase is what the
    rail would have picked, so it is picked here, where it cannot be skipped.
    """
    from core.services import gensettings

    chosen = gensettings.of(conversation)["continuous_phrase_id"]
    if chosen:
        phrase = QuickPhrase.objects.filter(id=chosen).first()
        if phrase and phrase.content.strip():
            return phrase.content.strip()

    first = QuickPhrase.objects.order_by("created_at").first()
    if first and first.content.strip():
        gensettings.merge(conversation, {"continuous_phrase_id": str(first.id)})
        return first.content.strip()
    return ""


def arm(
    conversation: Conversation,
    rounds: int,
    *,
    n_images: int | None,
    reflection_rounds: int | None,
    sticky_refs: bool,
) -> None:
    """
    Open a run. Called when the user sends by hand, or presses 重跑.

    The settings arrive as arguments rather than being read off a message row,
    because this runs *before* the turn's rows exist: the opening bracket has
    to land above the message that starts the run, not below it. They are also
    what every later turn in the run will be given — a run should not change
    character half way through because a slider moved.

    The instruction is not among them: it is resolved from the conversation —
    see _instruction — because the browser is not a reliable place to keep a
    fact the database already has.

    Whatever the old run was up to, a message the user typed ends it. That is
    one of the endings, so it gets a closing bracket like any other.
    """
    close_open_run(conversation, "replaced")

    rounds = min(max(int(rounds or 1), 1), settings.MAX_CONTINUOUS_ROUNDS)
    if rounds < 2:
        return
    text = _instruction(conversation)
    if not text:
        # Asked for a run and there is nothing to send again. Saying so is the
        # whole point: this used to return in silence, and a silent branch here
        # is indistinguishable from never having asked.
        with transaction.atomic():
            conv = Conversation.objects.select_for_update().filter(
                id=conversation.id
            ).first()
            if conv is not None:
                _note(conv, "continuous_start", ui_text.CONTINUOUS_NO_PHRASE)
        log.info("continuous run wanted in %s but there is no phrase to send",
                 conversation.id)
        return

    with transaction.atomic():
        conv = Conversation.objects.select_for_update().filter(id=conversation.id).first()
        if conv is None:
            return
        # A run can only ever be waiting on turns that did not exist when it
        # started, so everything already here is settled — said once, here,
        # rather than trusted to hold on every row that ever gets made.
        #
        # It has not held. A branch copies its source's turns, and a copy is
        # a new row with the column at its default; rows written before the
        # column existed are the same. reconcile counts "the newest turn
        # nobody has counted yet", so the first thing a run in a fresh branch
        # did was count an inherited turn as a round of its own — and paint
        # one picture fewer than was asked for.
        conv.messages.filter(role="assistant", run_settled=False).update(
            run_settled=True
        )
        _note(conv, "continuous_start", ui_text.CONTINUOUS_START.format(of=rounds))
        _save(
            conv,
            {
                "left": rounds - 1,
                "of": rounds,
                "text": text,
                "free": settings.FREE_RETRIES_IN_RUN,
                "done": 0,
                "failed": 0,
                "n_images": n_images,
                "reflection_rounds": reflection_rounds,
                "sticky_refs": bool(sticky_refs),
            },
        )
    log.info("continuous run armed in %s (%s rounds)", conversation.id, rounds)


def stop(conversation_id) -> None:
    """
    Called when the user stops a turn. Stopping stops the run — otherwise the
    next instruction goes out a second after they said no.
    """
    conv = Conversation.objects.filter(id=conversation_id).first()
    if conv is not None:
        close_open_run(conv, "stopped")


def close_open_run(conversation: Conversation, how: str) -> bool:
    """End whatever run this conversation has, if any. Says whether it did."""
    with transaction.atomic():
        conv = Conversation.objects.select_for_update().filter(id=conversation.id).first()
        run = conv.continuous_run if conv else None
        if not conv or not run:
            return False
        _close(conv, run, how)
        return True


# ---------------------------------------------------------------- reconcile


def reconcile(conversation_id) -> None:
    """
    Make what is happening match what the run asks for. The only decider.

    Under the conversation's own lock, so two callers — the clock, and the turn
    that just ended — cannot act on the same state at once. Everything it needs
    is read here; nothing is carried between calls except the run itself.

    Safe to call at any time, from anywhere, as often as you like. That is the
    property the previous design did not have, and the reason it kept breaking.
    """
    with transaction.atomic():
        conv = Conversation.objects.select_for_update().filter(id=conversation_id).first()
        run = conv.continuous_run if conv else None
        if not conv or not run:
            return

        # Something is being made for this conversation right now.
        #
        # Deliberately regardless of `archived`: being archived is about what
        # the reader sees, and has nothing to do with whether a worker is
        # painting. Asking for non-archived rows only would mean that anything
        # which archived a running turn — today nothing does, but that is a
        # convention, not a rule — would leave this believing the coast was
        # clear and start a second one alongside it.
        if conv.messages.filter(role="assistant", status="pending").exists():
            return

        # Also regardless of `archived`, and for the same reason: a turn that
        # happened, happened. Whether it is on screen is a separate question
        # from whether the run has counted it, and skipping an archived one
        # would quietly drop a round from the tally.
        turn = (
            conv.messages.filter(role="assistant", run_settled=False)
            .exclude(status="pending")
            .order_by("-seq")
            .first()
        )

        if turn is not None:
            # Counted once. Claiming it here rather than trusting the caller is
            # what makes this function safe to call twice for the same turn.
            Message.objects.filter(id=turn.id).update(run_settled=True)
            # A turn carrying pictures is a turn that worked, whatever its
            # status ended up saying. Pictures are paid for the moment they
            # exist, and the retry path archives the turn it replaces — so
            # reading the status alone is how a run throws away work somebody
            # was billed for, on the word of whichever writer happened to get
            # there last.
            made_something = turn.attachments.filter(kind="generated").exists()
            if turn.status == "done" or made_something:
                run = {**run, "done": run["done"] + 1,
                       "free": settings.FREE_RETRIES_IN_RUN}
            elif turn.status == "error":
                spent = _spend_retry(conv, run, turn)
                if spent is None:
                    return  # out of retries; the run is closed
                run = spent
            else:
                # cancelled, or a status this app does not recognise — either
                # way the run does not carry on by itself
                _close(conv, run, "stopped")
                return

        if run["left"] <= 0:
            _close(conv, run, "done")
            return

        # Nothing running, and the run wants more. This is the ordinary "queue
        # the next one" path AND the recovery path: a backend that stopped
        # between two turns leaves exactly this state, and it needs no special
        # handling because there is nothing special about it.
        if turn is not None and turn.status == "error" and not made_something:
            # The user's words are still up there, so this replaces the answer
            # rather than asking a second time — a run that stuttered should
            # leave one picture behind, not one picture and a repeated request.
            _save(conv, run)
            Message.objects.filter(id=turn.id).update(archived=True)
            _new_assistant(conv, run)
            log.info("continuous: retrying a failed turn in %s", conv.id)
        else:
            run = {**run, "left": run["left"] - 1}
            _save(conv, run)
            _send_next(conv, run)


def tick() -> int:
    """
    Reconcile every live run. The mechanism, not a backstop.

    One conversation going wrong must not stop the others, so each gets its own
    transaction and its own try.
    """
    ids = list(
        Conversation.objects.filter(continuous_run__isnull=False).values_list(
            "id", flat=True
        )
    )
    for conv_id in ids:
        try:
            reconcile(conv_id)
        except Exception:  # noqa: BLE001
            log.exception("could not reconcile the continuous run in %s", conv_id)
    return len(ids)


def _spend_retry(conv: Conversation, run: dict, turn: Message) -> dict | None:
    """
    A turn came to nothing. Free attempts first, then one round out of the run
    per attempt — the same policy reflection uses, and the same reason: one bad
    minute must not end a run of ten, and a broken generator must not become a
    loop that never stops spending.

    Returns the run to carry on with, or None once it has been closed.
    """
    run = {**run, "failed": run["failed"] + 1}
    if run["free"] > 0:
        run["free"] -= 1
    elif run["left"] > 0:
        run["left"] -= 1
        run["free"] = settings.FREE_RETRIES_IN_RUN
    else:
        _close(conv, run, "failed", last=turn)
        return None
    return run


# --------------------------------------------------------------------- rows


def _send_next(conv: Conversation, run: dict) -> None:
    """The app types the next instruction, in the user's name."""
    seq = (conv.messages.aggregate(m=Max("seq"))["m"] or 0) + 1
    Message.objects.create(
        conversation=conv,
        seq=seq,
        role="user",
        status="done",
        content=run["text"],
        auto_sent={"round": run["of"] - run["left"], "of": run["of"]},
    )
    _new_assistant(conv, run)


def _new_assistant(conv: Conversation, run: dict) -> None:
    """
    The pending row for the next turn, carrying the settings of the run.

    Queued after the transaction commits: the worker starts at once and would
    otherwise race the row it is meant to work on. If that queueing never
    happens — the process dies right here — the row is still pending, the
    reaper will fail it, and the next reconcile retries it. Losing this call
    costs time, not work.
    """
    from core.services import jobs

    seq = (conv.messages.aggregate(m=Max("seq"))["m"] or 0) + 1
    msg = Message.objects.create(
        conversation=conv,
        seq=seq,
        role="assistant",
        status="pending",
        force_n_images=run.get("n_images"),
        force_reflection_rounds=run.get("reflection_rounds"),
        sticky_refs=bool(run.get("sticky_refs")),
        heartbeat_at=timezone.now(),
    )
    Conversation.objects.filter(id=conv.id).update(updated_at=timezone.now())
    transaction.on_commit(lambda: jobs.start_generation(msg.id))


# -------------------------------------------------------------------- state


def _save(conv: Conversation, run: dict) -> None:
    Conversation.objects.filter(id=conv.id).update(continuous_run=run)


def _close(conv: Conversation, run: dict, how: str, last: Message | None = None) -> None:
    """
    The run is over. Clear it, and say what it came to. The only way out.

    A row rather than a toast, because the browser may not have been watching —
    that is the whole point of this living on the server. system rows are
    excluded from build_history, so the note is for the reader only.

    It is written even when the run achieved nothing. That used to be skipped
    on the grounds that a failed turn speaks for itself, which is true of the
    failure and says nothing about the run: the opening bracket was still up
    there promising ten pictures.
    """
    Conversation.objects.filter(id=conv.id).update(continuous_run=None)
    if last is not None and run["done"] > 0:
        # the run made things, and then stumbled on the way out. The note below
        # says so; a 生成失败 sitting under the pictures would say something
        # else. Archived, not deleted — the row and its error are still there.
        Message.objects.filter(id=last.id).update(archived=True)
    _note(
        conv,
        "continuous",
        ui_text.CONTINUOUS_SUMMARY.format(
            head=ui_text.CONTINUOUS_HEADS.get(how, ui_text.CONTINUOUS_HEADS["stopped"]),
            of=run["of"],
            done=run["done"],
            failed=run["failed"],
        ),
    )
    log.info("continuous run in %s ended (%s): %s/%s", conv.id, how, run["done"], run["of"])


def _note(conv: Conversation, kind: str, text: str) -> None:
    seq = (conv.messages.aggregate(m=Max("seq"))["m"] or 0) + 1
    Message.objects.create(
        conversation=conv,
        seq=seq,
        role="system",
        status="done",
        notice_kind=kind,
        content=text,
    )
