"""
Reflection: the lead model looks at what it just painted and may redraw it.

The loop is ordinary — paint, look, decide, paint again — but two things about
it are load-bearing and easy to get wrong, so they are stated here once:

  * **Inside the loop the model sees everything.** Its own reply, the picture
    description it wrote, the tags that came out of it, and the pictures
    themselves, for every round so far. Judging a fix without seeing what it
    was fixing is not judging.

  * **Outside the loop none of it happened.** When the loop ends, the
    conversation carries exactly what it would have carried without
    reflection: one assistant turn, one picture description, one set of tags,
    one set of pictures — the last round's. No reflection message is ever
    stored as a conversation row, so `build_history` cannot pick one up
    even by accident. What the user sees of it is a log on the turn, shown
    like a system note and read by nobody but them.

This module owns the loop and knows nothing about HTTP, threads or the DB
schema. Painting, cancelling and progress are passed in — that keeps the
import graph one-way (pipeline imports this, never the reverse) and lets the
tests drive the whole loop without a network.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field

from django.conf import settings

from core import prompts
from core.services import drawnote, llm

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReflectionShape:
    """
    How much of the loop the model is shown, and how hard it may try.

    Separate from HistoryShape because it describes a different window: this
    one is about a single turn's internal rounds, not about the conversation.
    Nothing below reads a literal, so changing the shape is changing an
    instance here.
    """

    #: Pictures from ONE round that go back to the model. The rest of that
    #: round's output is text and costs little; pictures are the expensive part.
    max_images_per_round: int = 2
    #: How many rounds back stay in the reflection context. None = all of them,
    #: which is the point — a model that cannot see round 1 cannot tell whether
    #: round 3 fixed it.
    rounds_shown: int | None = None
    #: Attempts after a failed redraw that cost nothing. Past these, each
    #: further attempt costs one round out of the run — see _redraw_with_retries.
    #: None means "ask the settings when you need it": a value baked in here
    #: would be read once at import, which is both a surprise and out of reach
    #: of override_settings.
    free_retries: int | None = None

    @property
    def free_retry_budget(self) -> int:
        return (
            settings.FREE_RETRIES_IN_RUN if self.free_retries is None else self.free_retries
        )
    #: Throw away the previous round's pictures once a newer round has some.
    #: They are intermediate by definition; the turn is supposed to end holding
    #: only the last ones.
    discard_superseded_images: bool = True


SHAPE = ReflectionShape()


@dataclass
class Hooks:
    """
    Everything the loop needs from the world, injected.

    Written as a record rather than taken from pipeline directly so this module
    imports nothing that imports it back, and so a test can run the whole loop
    against counters.
    """

    #: (verdict) -> (nai_prompts, images) — one full redraw from the lead's
    #: reflection answer (its entries and its draw), prompts and paint
    redraw: Callable[[dict], tuple[dict, list]]
    #: raises if the user cancelled
    check_cancel: Callable[[], None]
    #: (phase) -> None, for the spinner
    set_phase: Callable[[str], None]
    #: (attachment) -> str|None — the data URL the model is shown
    image_url: Callable[[object], str | None]
    #: (attachments) -> None — drop pictures a later round has replaced
    discard: Callable[[list], None] = lambda atts: None
    #: (prefix) -> context manager — everything reported inside it is reported
    #: as happening inside that round, so the spinner can say "反思中（正在落笔）"
    #: instead of the bare stage it would otherwise look identical to
    enter_round: Callable[[str], object] = lambda prefix: nullcontext()
    #: Exceptions that mean "stop everything" rather than "try again" — a
    #: cancel, above all. Everything else is treated as a hiccup worth another
    #: go, which is only safe because something has to name the exceptions that
    #: are not.
    fatal: tuple = ()
    #: (exception) -> bool — whether this particular failure would come back
    #: the same way. `fatal` names types; this asks about the one in hand,
    #: because "out of Anlas" and "NovelAI had a bad minute" arrive as the same
    #: class and only one of them is worth a retry budget. Answering yes ends
    #: the run here rather than after it has spent every round on the same wall.
    hopeless: Callable[[Exception], bool] = lambda exc: False
    #: (answer) -> str — what does not hold together in a round's answer
    #: (llm._ask's check); "" when it does. None checks nothing.
    check_answer: Callable[[dict], str] | None = None


@dataclass
class Outcome:
    """What the turn ends up carrying. Everything else is forgotten."""

    brief: dict
    nai_prompts: dict
    images: list
    #: one entry per round: {round, reply, redrew}
    log: list[dict] = field(default_factory=list)
    rounds_run: int = 0


def _images_block(
    images: list,
    image_url: Callable[[object], str | None],
    nai_prompts: dict | None = None,
) -> dict | None:
    """
    One round's pictures, in a turn of the app's own.

    The tags ride here rather than on the assistant's turn: the drawing model
    wrote them, not the assistant, and what the assistant's turns look like is
    what the model takes for an example.
    """
    # What gets said about a drawing is drawnote's to decide — the very same
    # answer build_history asks it for. This turn is the one actually looking
    # at the picture, and while it decided for itself it was the one that
    # forgot to name it.
    said = "\n".join(drawnote.lines(nai_prompts, with_images=True))
    parts: list[dict] = [{"type": "text", "text": said}]
    for att in images[: SHAPE.max_images_per_round]:
        url = image_url(att)
        if url:
            parts.append({"type": "image_url", "image_url": {"url": url}})
    return {"role": "user", "content": parts} if len(parts) > 1 else None


def run(
    *,
    base_history: list[dict],
    first_reply: str,
    brief: dict,
    nai_prompts: dict,
    images: list,
    rounds: int,
    hooks: Hooks,
    first_answer: dict | None = None,
) -> Outcome:
    """
    Run the reflection loop and return what the turn should keep.

    `base_history` is the same message array the ordinary lead call was given.
    The loop appends to a copy of it and throws the copy away at the end.
    `first_answer` is the lead's whole answer for this turn (reply, the
    entries it wrote, its project edits, its draw); without it only the
    reply is shown back.
    """
    outcome = Outcome(brief=brief, nai_prompts=nai_prompts, images=images)
    if rounds <= 0 or not images:
        return outcome

    try:
        _loop(outcome, base_history, first_answer or {"reply": first_reply}, rounds, hooks)
    except hooks.fatal:
        raise
    except Exception as exc:  # noqa: BLE001 — see the promise below
        # A picture already exists. Reflection is an attempt to improve it, and
        # an attempt that fails leaves what it started with — it does not take
        # the picture down with it. Anything that gets this far is a bug or an
        # outage; either way the user asked for a picture and there is one.
        log.exception("reflection failed; keeping the picture it already had")
        outcome.log.append({
            "round": outcome.rounds_run + 1,
            "failed": str(exc)[:200],
            "paid": False,
            "gave_up": True,
        })
    return outcome


def _loop(
    outcome: Outcome,
    base_history: list[dict],
    first_answer: dict,
    rounds: int,
    hooks: Hooks,
) -> None:
    """The rounds themselves. Mutates `outcome`; see run() for the promise."""
    brief, nai_prompts = outcome.brief, outcome.nai_prompts

    # The turn as the model would read it back: what it said, what it asked
    # for, and what it got. Built here rather than reused from the history
    # because none of this is in the database yet.
    base = list(base_history)
    opening = [{
        "role": "assistant",
        "content": _assistant_turn_text(first_answer, brief),
    }]
    # one list per round, so the window below can be measured in rounds rather
    # than in messages — the rounds are what vary in length
    groups: list[list[dict]] = []

    # Rounds are a budget rather than a count, because a failed redraw can buy
    # another attempt with one of them. Without that, a single bad minute ends
    # a run of ten pictures; with an unbounded retry, a bug turns it into a
    # loop that never stops spending.
    budget = rounds
    round_no = 0
    while budget > 0:
        budget -= 1
        round_no += 1
        hooks.check_cancel()
        block = _images_block(outcome.images, hooks.image_url, outcome.nai_prompts)
        if block is None:
            log.warning("reflection: no readable image for round %d, stopping", round_no)
            break
        group = [
            block,
            {"role": "user",
             "content": prompts.reflection(round=round_no, max_rounds=rounds)},
        ]
        groups.append(group)

        # Everything from here to the end of the round is reported as part of
        # it — the look, and the redraw's own prompting and painting. Without
        # this the redraw's phases are indistinguishable from an ordinary
        # turn's and the spinner stops saying reflection is happening at all.
        with hooks.enter_round(f"reflecting:{round_no}/{rounds}"):  # type: ignore[attr-defined]
            verdict, budget = _look_with_retries(
                _assemble(base, opening, groups), outcome, round_no, budget, hooks
            )
            if verdict is None:
                break
            hooks.check_cancel()
            outcome_of_round, budget = _finish_round(
                verdict, outcome, round_no, rounds, budget, hooks
            )
        if outcome_of_round == "stop":
            break
        if outcome_of_round == "satisfied":
            break

        if outcome_of_round == "redrew":
            # this round's verdict and what it produced, so the next round can
            # see what it is judging a fix against
            group.append({
                "role": "assistant",
                "content": _assistant_turn_text(verdict, outcome.brief),
            })


def _finish_round(
    verdict: dict,
    outcome: Outcome,
    round_no: int,
    rounds: int,
    budget: int,
    hooks: Hooks,
) -> tuple[str, int]:
    """
    Record the verdict and act on it. Returns (what next, remaining budget).

    Split out so the whole round — the look AND the redraw it may ask for —
    sits inside one phase scope; otherwise the redraw reports itself as an
    ordinary paint and the spinner stops mentioning reflection halfway through.
    """
    redraw_brief = verdict.get("draw")
    # redrew says a picture was redrawn -- true once one is, not when one was asked for
    entry = {"round": round_no, "reply": verdict.get("reply") or "", "redrew": False}
    if verdict.get("checks"):
        # what the app sent back about this round's answer before taking it
        entry["checks"] = [c.get("note") or "" for c in verdict["checks"]]
    outcome.log.append(entry)
    outcome.rounds_run = round_no

    if not redraw_brief:
        log.info("reflection: satisfied after %d round(s)", round_no)
        return "satisfied", budget

    new_prompts, new_images, budget = _redraw_with_retries(
        verdict, outcome, round_no, rounds, budget, hooks
    )
    if not new_images:
        # nothing came back even after paying for retries; the previous round's
        # pictures are still the best thing this turn has, so keep them
        log.warning("reflection: round %d could not redraw, keeping what there was",
                    round_no)
        return "stop", budget

    if SHAPE.discard_superseded_images:
        hooks.discard(outcome.images)
    outcome.brief = redraw_brief
    outcome.nai_prompts = new_prompts
    outcome.images = new_images
    entry["redrew"] = True
    return "redrew", budget


def _spend(outcome: Outcome, round_no: int, budget: int, free_left: int, reason: str):
    """
    A step failed. Decide who pays for another go, and write it down.

    Returns (keep_going, budget, free_left). Free attempts first, then one
    round out of the run per attempt, then nothing — which is what keeps a
    broken generator bounded by a number the user chose.

    Failures are logged either way: a run that quietly retried four times and
    one that went smoothly should not look the same afterwards.
    """
    paid = free_left <= 0
    if paid and budget <= 0:
        outcome.log.append({"round": round_no, "failed": reason, "paid": False,
                            "gave_up": True})
        return False, budget, free_left
    if paid:
        budget -= 1
    else:
        free_left -= 1
    outcome.log.append({"round": round_no, "failed": reason, "paid": paid})
    return True, budget, free_left


def _look_with_retries(
    convo: list[dict], outcome: Outcome, round_no: int, budget: int, hooks: Hooks
) -> tuple[dict | None, int]:
    """
    Ask the model what it thinks of the picture, retrying the same way a redraw
    does. A vendor having a bad minute must not be able to turn a turn that
    already produced a picture into a failed turn.
    """
    free_left = SHAPE.free_retry_budget
    while True:
        try:
            hooks.set_phase("thinking")
            return llm.reflect_turn(
                convo, on_retry=lambda _r: hooks.set_phase("retry:thinking"),
                check=hooks.check_answer,
            ), budget
        except hooks.fatal:
            raise
        except Exception as exc:  # noqa: BLE001 — a failure here is a retry
            if hooks.hopeless(exc):
                raise
            go_on, budget, free_left = _spend(
                outcome, round_no, budget, free_left, str(exc)[:200]
            )
            if not go_on:
                return None, budget
            hooks.check_cancel()


def _redraw_with_retries(
    brief: dict,
    outcome: Outcome,
    round_no: int,
    rounds: int,
    budget: int,
    hooks: Hooks,
) -> tuple[dict, list, int]:
    """
    One redraw, with the run's retry policy applied.

    A generation fails for reasons that have nothing to do with the picture —
    the vendor is busy, a model returns something unusable, NovelAI times out.
    Letting any of those end a ten-picture run is the wrong trade, so:

      * the first SHAPE.free_retry_budget attempts after a failure cost nothing;
      * each further attempt costs one round out of the run's own budget;
      * when the budget is gone, so is the run.

    The second rule is what keeps a bug from becoming an unbounded loop: every
    retry past the free ones is paid for out of a number the user chose.
    Failures go into the log too — a run that quietly retried four times and a
    run that went smoothly should not look the same afterwards.
    """
    free_left = SHAPE.free_retry_budget
    while True:
        try:
            prompts_out, images = hooks.redraw(brief)
        except hooks.fatal:
            raise
        except Exception as exc:  # noqa: BLE001 — any failure is a retry, not a crash
            if hooks.hopeless(exc):
                raise
            prompts_out, images, reason = {}, [], str(exc)[:200]
        else:
            if images:
                return prompts_out, images, budget
            reason = "no image came back"

        go_on, budget, free_left = _spend(outcome, round_no, budget, free_left, reason)
        log.warning("reflection: redraw failed in round %d/%d (%s)%s",
                    round_no, rounds, reason, "" if go_on else " — giving up")
        if not go_on:
            return {}, [], budget
        hooks.check_cancel()
        hooks.set_phase("retry:prompting")


def _assemble(base: list[dict], opening: list[dict], groups: list[list[dict]]) -> list[dict]:
    """
    The message array for one reflection call.

    The conversation and the turn's own output always stay: without them the
    model is judging a picture it has no brief for. Only the earlier ROUNDS are
    subject to the window, and by default none of them are dropped — a model
    that cannot see round 1 cannot tell whether round 3 fixed it. The dial
    exists because ten rounds of pictures is a lot of tokens, and the day that
    matters it should be one number to change.
    """
    shown = groups if SHAPE.rounds_shown is None else groups[-SHAPE.rounds_shown:]
    return base + opening + [m for g in shown for m in g]


def _assistant_turn_text(answer: dict, brief: dict) -> str:
    """
    One round of the model's own output, in the shape it output it: the
    lead's answer -- the reply, the entries it wrote, its project edits --
    with the draw as the app settled it. The tags are not part of what it
    answered: the app says them in its own turn.
    """
    return json.dumps(
        {
            "reply": answer.get("reply") or "",
            "settings": [_as_said(e) for e in answer.get("settings") or []],
            "characters": [_as_said(e) for e in answer.get("characters") or []],
            "edits": ({k: [_as_said(e) for e in answer["edits"].get(k) or []] for k in ("settings", "characters")}
                      if isinstance(answer.get("edits"), dict) else None),
            "draw": brief or None,
        },
        ensure_ascii=False,
    )


def _as_said(entry: dict) -> dict:
    """An entry the way the lead wrote it: the tag lists back as one string."""
    out = dict(entry)
    for k in ("positive", "negative"):
        if isinstance(out.get(k), list):
            out[k] = ", ".join(out[k])
    if isinstance(out.get("variants"), list):
        out["variants"] = [_as_said(v) for v in out["variants"]]
    return out


