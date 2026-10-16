"""
The generation pipeline: turn a pending assistant Message into a completed one.

Flow:
  1. build the model's message array from the conversation history
     (user uploads go in as pictures; the latest generated images are injected
     as a clearly-labelled context block so Vallotton can "see" her own work)
  2. ask the LEAD model for this turn: a reply, optionally a plain-language
     drawing brief, optionally library edits to propose      (phase=thinking)
  3. if there is a brief, ask the DRAWING model to turn it plus the selected
     library entries into NovelAI prompts                   (phase=prompting)
  4. call NovelAI for the N requested images in parallel, saving each one the
     moment it arrives                                        (phase=painting)
  5. mark done; auto-title, and note the context budget if it is running out

Library edits are never written here — they are parked as a LibraryProposal
for the user to approve.

Cancellation: the cancel view flips the row to ``cancelled`` immediately, so
the user is never left waiting on a blocking API call. This worker may still
be inside the model/NovelAI at that point; every write it makes afterwards is
guarded by ``status="pending"``, so its late result is discarded rather than
resurrecting the message. Images already generated (and paid for) before the
cancel landed are kept.

Heartbeat: a watchdog thread refreshes ``heartbeat_at`` every 20s for as long
as the job runs, so the reaper (which fails jobs whose heartbeat went silent)
only ever catches workers that really died — never one sitting in a slow call.
"""

from __future__ import annotations

import io
import json
import logging
import secrets
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass

from PIL import Image

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import close_old_connections, connection, transaction
from django.db.models import Max
from django.utils import timezone

from core.prompts import wording
from core.models import (
    Attachment,
    Character,
    Conversation,
    LibraryProposal,
    Message,
    Setting,
)

from . import (
    continuous, drawnote, gensettings, imaging, library, llm, novelai,
    prompt as prompt_slots, proposals, providers, reflection,
)
from core import ui_text
from core.services import notices

log = logging.getLogger(__name__)

@dataclass(frozen=True)
class HistoryShape:
    """
    How much of its own past the lead model is shown.

    Four independent dials, each counting TURNS from the newest backwards,
    with None meaning "every turn that has one". They are separate because the
    four things cost and buy different amounts: a picture description is a few
    hundred words of the model's own reasoning, a page of tags is dense and
    stale the moment the description changes, and an image is expensive.

    Nothing below reads a literal, so changing the shape is changing an
    instance here.
    """

    #: how far back the conversation itself is carried
    max_messages: int = 40
    #: user turns whose uploaded references ride along
    reference_turns: int | None = 1
    #: hard cap on reference images regardless of how many turns qualify
    max_references: int = 6
    #: assistant turns that carry back the drawing model's NovelAI prompts.
    #: None means every one of them.
    #:
    #: Every one, because the lead model is writing the next brief and the tags
    #: are the only record of what the last few actually turned into — a scene
    #: it described three turns ago and a scene it described now differ in ways
    #: that only show up in the tags. It used to see one turn's worth and was
    #: effectively writing blind about everything before that.
    #:
    #: Measured on real conversations: 3,889 characters a turn, about 1,300
    #: tokens — 35% base prompt, 43% character prompts, 21% the negative the
    #: drawing model wrote itself. Nothing in there is boilerplate worth
    #: stripping. At the 40-turn window above that is roughly 52k tokens, which
    #: is 5% of the context this provider has.
    prompt_turns: int | None = None
    #: assistant turns that carry back the images actually generated
    generated_turns: int | None = 1
    #: images attached per qualifying turn
    max_generated_per_turn: int = 2


#: There is no knob for the picture descriptions any more: a description is
#: part of the answer the assistant gave, and a window either carries a turn or
#: it does not. Editing what it said on the way past is the thing this stopped
#: doing. What is left here governs what the APP adds — references, tags,
#: pictures — which is the app's to ration.
HISTORY = HistoryShape()



class NoPictureToEdit(Exception):
    """The drawing an edit continues has no slot table to apply it to."""


class PipelineCancelled(Exception):
    pass


# ---------------------------------------------------------------- helpers


def _is_cancelled(message_id) -> bool:
    # cancelled the moment the row is no longer pending (cancel sets it
    # straight to "cancelled"), or an explicit cancel flag was raised
    return not Message.objects.filter(id=message_id, status="pending").exists()


#: Set while a reflection round is running, so every phase written inside it
#: comes out as "reflecting:2/5:<whatever>". Thread-local because one worker
#: thread owns one turn, and because the alternative — threading a prefix
#: through _paint and everything it calls — would mean every future stage has
#: to remember to do it.
_phase_prefix = threading.local()


@contextmanager
def _phase_scope(prefix: str):
    """Everything written inside this block is reported under `prefix`."""
    previous = getattr(_phase_prefix, "value", "")
    _phase_prefix.value = prefix
    try:
        yield
    finally:
        _phase_prefix.value = previous


def _set_phase(message_id, phase: str) -> None:
    prefix = getattr(_phase_prefix, "value", "")
    if prefix:
        phase = f"{prefix}:{phase}"
    # only advance a still-pending job; a cancelled job must not be revived
    Message.objects.filter(id=message_id, status="pending").update(
        phase=phase[:64], heartbeat_at=timezone.now()
    )


def _check_cancel(message_id) -> None:
    if _is_cancelled(message_id):
        raise PipelineCancelled()


class _Heartbeat:
    """
    Keeps `heartbeat_at` fresh for as long as a job is genuinely running.

    Without this the timestamp only moves on phase changes, and a single slow
    step (the model retrying three times, or a long NovelAI call) looks identical
    to a dead worker — the reaper then fails a healthy job and the user is
    told the backend restarted. The watchdog only ever touches rows that are
    still pending, so it can never revive a cancelled one.
    """

    INTERVAL = 20.0

    def __init__(self, message_id):
        self.message_id = message_id
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        while not self._stop.wait(self.INTERVAL):
            try:
                close_old_connections()
                Message.objects.filter(
                    id=self.message_id, status="pending"
                ).update(heartbeat_at=timezone.now())
            except Exception as exc:  # noqa: BLE001 — never kill the job
                log.warning("heartbeat failed for %s: %s", self.message_id, exc)
        try:
            connection.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "_Heartbeat":
        thread = threading.Thread(target=self._run, daemon=True)
        self._thread = thread
        thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)


_image_data_url = imaging.attachment_data_url


def _newest_turns(msgs: list, matches, limit: int | None) -> set:
    """
    Ids of the newest `limit` messages that match — None means every one.

    One helper for all four dials, so "newest N turns that have X" is written
    once and the four cannot drift into behaving differently.
    """
    picked = set()
    for m in reversed(msgs):
        if not matches(m):
            continue
        picked.add(m.id)
        if limit is not None and len(picked) >= limit:
            break
    return picked


def real_turns(conversation: Conversation):
    """
    The rows that are part of this conversation, as far as anyone is concerned.

    ONE definition, because two callers have to agree about it: the history the
    model is given, and the copy a branch makes. They drifted apart once and
    the comment in the branch still points here.

    A row belongs if it happened and left something behind. That is a fact
    about the row's contents, not about how it ended: a turn stopped half way
    still painted what it painted, and the user is looking at it. Judging it by
    status instead is judging it by a label that is only supposed to agree with
    its contents — and every label here is written under a guard that a stop
    can win.

    The line is drawn where the transcript draws it, because that is the same
    question: a turn is part of this conversation if the user can see it.

      done        all of it — what was said and what was painted
      cancelled   the same: stopping shows the words and the pictures, with a
                  「已停止」 under them
      error       only its pictures. A failed turn's words are behind a fold
                  and are about the failure, not about the picture
      pending     nothing. It is still moving; there is nothing settled to read
    """
    from django.db.models import Exists, OuterRef, Q

    from core.models import Attachment

    has_picture = Exists(
        Attachment.objects.filter(message=OuterRef("pk"), kind="generated")
    )
    return (
        conversation.messages.filter(archived=False)
        # "system" rows are the app's own notes to the user — never model input
        .filter(role__in=("user", "assistant"))
        .exclude(status="pending")
        .annotate(_painted=has_picture)
        .filter(Q(status__in=("done", "cancelled")) | Q(_painted=True))
    )


def assistant_answer(m: Message) -> str:
    """
    What the assistant said that turn, in the shape it said it.

    The row keeps the three parts of that answer in their own columns — the
    reply, the picture description, the library edits — and this puts them
    back together as the object the model actually returned.

    It used to be retold instead: the reply as prose, the description appended
    underneath in a bracketed block, the tags in another. None of that was the
    assistant's writing, and every turn in the window was another example of
    answering in a shape we tell it never to use. It took the hint — five of
    fifty-eight measured calls answered with that block instead of with JSON.
    See reports/21-deepseek-failures.html.

    `edits` is carried as it was, not blanked: knowing which suggestions it has
    already made is how it avoids making them again.
    """
    raw = m.model_raw if isinstance(m.model_raw, dict) else {}
    return json.dumps(
        {
            "reply": m.content or "",
            # null is not our invention — it is the schema's own answer for
            # "no picture this turn", and what the model writes when it means it
            "draw": m.draw_brief or None,
            "edits": raw.get("edits") or None,
        },
        ensure_ascii=False,
    )


#: No i, l, o, 0 or 1: this is read off a screen and typed back by hand.
_ID_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def new_draw_id() -> str:
    """A short name for one drawing, short enough to say out loud."""
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(6))


def _app_note(lines: list[str], image_urls: list[str]) -> dict:
    """
    One turn in the app's own voice.

    Its own message with its own sender, because the app is not the assistant
    and not the user. Anything it wants the model to know about what happened
    between two turns is said here.

    ONE sender, always the same one, whatever the note happens to carry.

    `system` is what these are, and it was tried. It works for a note of plain
    text and is refused outright for a note carrying pictures — measured
    2026-09-16, DeepSeek answers 400 "Image in system message is unsupported",
    and OpenRouter takes it but reads the text as something the user said.

    Sending only the plain ones as `system` was the obvious next move and is
    the wrong one: it makes the app's identity depend on what it is holding, so
    within one conversation the same speaker arrives under two different
    senders and the model is left to work out that they are one voice. A limit
    in the transport is not a reason to have two identities. So every one of
    them is `user`, and the first line is what says who is speaking.

    That first line comes from the caller, not from here: what the app is
    announcing and how it announces itself are the same sentence, and splitting
    them across two files is how the drawing note and the plain note ended up
    opening differently.
    """
    text = "\n".join(lines)
    parts: list[dict] = [{"type": "text", "text": text}]
    for url in image_urls:
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return {"role": "user", "content": parts}


def build_history(
    conversation: Conversation,
    before_seq: int,
    selection=None,
    sticky_refs: bool = False,
    sent: dict | None = None,
) -> list[dict]:
    """
    Build the model's message array from all active messages with seq < before_seq.

    `sticky_refs` is 常驻参考: every reference picture in the window comes along
    instead of only the newest turn's. It widens a dial rather than copying
    anything — each picture stays on the message it was actually given with, so
    it appears exactly once however long the option stays on, and turning it
    off is back to normal on the next turn with nothing to undo.
    """
    # Newest first, then put back in order. Taking the OLDEST max_messages*2 and
    # then slicing the tail off THAT list drops the newest turns once a
    # conversation passes the outer limit — the opposite of what a window is for.
    msgs = list(
        real_turns(conversation)
        .filter(seq__lt=before_seq)
        .prefetch_related("attachments")
        .order_by("-seq")[: HISTORY.max_messages]
    )
    msgs.reverse()

    # Which turns carry what. Every scan runs over this window rather than the
    # whole conversation, so branching from an early message sees the history
    # as it stood at THAT point.
    prompt_ids = _newest_turns(
        # A turn carries tags because it HAS tags, not because the column is
        # non-empty: the same dict also holds the drawing's id and the count,
        # so "there is a nai_prompts" stopped meaning "something was drawn"
        # the moment anything else was settled into it.
        msgs,
        lambda m: m.role == "assistant" and bool((m.nai_prompts or {}).get("base_prompt")),
        HISTORY.prompt_turns,
    )
    # A turn shows its pictures because it HAS pictures. `kind` is supposed to
    # say the same thing and mostly does, but it is written under a
    # status="pending" guard — so a turn stopped at the wrong instant, or one
    # whose worker died before it could tidy up, keeps its pictures and loses
    # the label. The pictures are on screen either way; deciding by the label
    # is how they became invisible to the model while the user was looking
    # straight at them.
    image_ids = _newest_turns(
        msgs,
        lambda m: m.role == "assistant"
        and any(a.kind == "generated" for a in m.attachments.all()),
        HISTORY.generated_turns,
    )

    reference_ids = _newest_turns(
        msgs,
        lambda m: m.role == "user" and any(a.kind == "upload" for a in m.attachments.all()),
        None if sticky_refs else HISTORY.reference_turns,
    )
    # ...and a cap on the images themselves, newest first, whatever the turns say
    uploads_included: set = set()
    budget = HISTORY.max_references
    for m in reversed(msgs):
        if m.id not in reference_ids:
            continue
        for att in m.attachments.all():
            if att.kind == "upload" and budget > 0:
                uploads_included.add(att.id)
                budget -= 1

    if sent is not None:
        # which uploads went, for the row to say (the inspector used to guess)
        sent["references"] = [str(i) for i in uploads_included]
    out: list[dict] = []
    for m in msgs:
        if m.role == "user":
            parts: list[dict] = []
            text = m.content or ""
            atts = [a for a in m.attachments.all() if a.kind == "upload"]
            if text.strip():
                parts.append({"type": "text", "text": text})
            # The picture itself, not a description of it. A dedicated vision
            # model used to stand in front of these; it only ever lost detail
            # once the lead models could see this well themselves.
            for att in atts:
                if att.id not in uploads_included:
                    continue
                url = _image_data_url(att)
                if url:
                    parts.append({"type": "image_url", "image_url": {"url": url}})
            if not parts:
                parts.append({"type": "text", "text": wording.EMPTY_USER_MESSAGE})
            out.append({"role": "user", "content": parts})
            # what the app did with that turn — here, that some of its pictures
            # did not fit. Said by the app, not slipped into the user's words.
            skipped = [a for a in atts if a.id not in uploads_included]
            if skipped:
                out.append(
                    _app_note(
                        [
                            wording.SYSTEM_NOTE_PLAIN,
                            wording.APP_NOTE_REFERENCES_SKIPPED.format(n=len(skipped)),
                        ],
                        [],
                    )
                )
        else:
            out.append({"role": "assistant", "content": assistant_answer(m)})
            # Everything the app did with that answer goes in one turn of its
            # own: the tags the drawing model made of it, and the pictures that
            # came back. Both used to be written into the assistant's message —
            # words it never said, in a shape we then told it not to use.
            urls: list[str] = []
            if m.id in image_ids:
                # the main picture first: it is the one the user chose, and
                # the one the next edit follows, so it must be the one the
                # lead has seen when the window holds fewer than all
                gen_atts = sorted(
                    (a for a in m.attachments.all() if a.kind == "generated"),
                    key=lambda a: (0 if a.position == m.main_index else 1, a.position),
                )[: HISTORY.max_generated_per_turn]
                urls = [u for u in (_image_data_url(a) for a in gen_atts) if u]

            # How the note reads is drawnote's, whole — the very same answer
            # reflection asks it for. All this decides is what there is to say.
            with_tags = m.id in prompt_ids and bool(
                (m.nai_prompts or {}).get("base_prompt")
            )
            if with_tags or urls:
                out.append(
                    _app_note(
                        drawnote.lines(
                            m.nai_prompts, with_tags=with_tags, with_images=bool(urls)
                        ),
                        urls,
                    )
                )

    # tell the lead model what the user has switched on for this scene. It
    # rides with the latest user turn so it reads as part of the request
    # rather than as a standing instruction.
    if selection is not None:
        text = library.as_prompt_text(selection)
        # Said either way. "Nothing is selected" is a fact about this turn; an
        # absent section leaves the model to infer it, and the wrong inference
        # is that the entries from an earlier turn still stand.
        block = (
            wording.LIBRARY_BLOCK_HEADING + "\n" + text if text else wording.LIBRARY_NONE
        )
        for msg in reversed(out):
            if msg["role"] == "user" and isinstance(msg["content"], list):
                msg["content"].append({"type": "text", "text": block})
                break
        else:
            out.append({"role": "user", "content": [{"type": "text", "text": block}]})
    return out


#: How much of an unreadable answer to keep on the row. Long enough to hold a
#: whole lead answer, short enough that a bad week does not fill the database.
UNPARSED_KEPT = 20000


def _keep_unparsed(message_id, exc: Exception) -> None:
    """
    Park the answer a model gave that could not be read.

    The error string carries an excerpt so the transcript can say something
    short. The excerpt is not enough to tell WHY it could not be read — that
    turns on one character — so the answer itself goes where 「检查」 can show
    it. Without this the only way to find out is to wait for it to happen
    again and hope it happens somewhere you can watch.
    """
    raw = getattr(exc, "raw", "") or ""
    if not raw:
        return
    try:
        row = Message.objects.filter(id=message_id).first()
        if row is None:
            return
        Message.objects.filter(id=message_id).update(
            model_raw={
                **(row.model_raw if isinstance(row.model_raw, dict) else {}),
                "unparsed": raw[:UNPARSED_KEPT],
            }
        )
    except Exception:  # noqa: BLE001 — keeping evidence must not add a failure
        log.warning("could not keep the unreadable answer for %s", message_id)


def _save_generated_image(message: Message, png: bytes, seed: int, position: int) -> None:
    att = Attachment(
        message=message,
        kind="generated",
        mime="image/png",
        size_bytes=len(png),
        seed=seed,
        position=position,
    )
    try:
        img = Image.open(io.BytesIO(png))
        att.width, att.height = img.size
    except Exception:  # noqa: BLE001
        pass
    att.file.save(f"{uuid.uuid4().hex}.png", ContentFile(png), save=True)


def _generate_in_thread(*args, **kw) -> tuple[bytes, int]:
    """
    Worker body for the paint pool. Django connections are thread-local, so a
    pool thread that touches the DB (novelai records spend) opens its own and
    must close it — otherwise every parallel turn leaks a connection.
    """
    close_old_connections()
    try:
        return novelai.generate_one(*args, **kw)
    finally:
        connection.close()


def _with_count(message: Message, nai_prompts: dict) -> dict:
    """
    nai_prompts as it will actually be painted.

    How many to paint is the app's call — the 出图数量 control, carried on the
    row as force_n_images — never the model's: neither one is asked for a count
    any more. The number in nai_prompts is the fallback rather than the source,
    and it is there for the callers that set it themselves: MCP's direct
    generate, and a 重绘 replaying a row.

    Its own function because the answer has to travel. _paint used to settle
    the count in a local copy, so the dict the row kept and the dict handed
    back to the caller disagreed — and a reflection round, which carries that
    dict forward and writes it over the row at the end, put the model's count
    back. Three pictures on screen, n_images: 1 on the row, and a 重绘 that
    painted one.
    """
    n = message.force_n_images or int(nai_prompts.get("n_images") or 1)
    n = min(max(n, 1), settings.MAX_IMAGES_PER_REQUEST)

    # The tier rides here for the same reason the count does: 重绘 replays this
    # dict verbatim, so a picture redrawn next week is redrawn at the tier it
    # was made at, not at whatever the conversation is set to by then.
    tier = nai_prompts.get("tier") or gensettings.of(message.conversation)["quality"]

    # And the name of this drawing, made once and kept. Every reflection round
    # comes back through here with new tags, and they are the same drawing —
    # the brief did not change — so the name must not change either, or the
    # thing the user is pointing at gets renamed while they look at it.
    draw_id = nai_prompts.get("draw_id") or new_draw_id()

    if (
        n == nai_prompts.get("n_images")
        and tier == nai_prompts.get("tier")
        and draw_id == nai_prompts.get("draw_id")
    ):
        return nai_prompts
    return {**nai_prompts, "n_images": n, "tier": tier, "draw_id": draw_id}


def _paint(message: Message, nai_prompts: dict, seed: int | None = None) -> int:
    """
    Generate n images for one turn, in parallel, saving each as soon as it
    arrives so a later cancel or failure never throws away work already paid
    for. Returns the number saved.

    `seed` is for the first picture only: it follows the main picture of the
    drawing this one continues (an edit) or of the latest drawing (a new
    picture), so that what was not changed stays where it was. The rest of a
    batch, and every 重绘, roll their own.

    How many to paint is the app's call — the 出图数量 control, carried on the
    row as force_n_images — never the model's: neither one is asked for a count
    any more. The number in nai_prompts is the fallback rather than the source,
    and it is there for the callers that set it themselves: MCP's direct
    generate, and a 重绘 replaying a row.
    """
    settled = _with_count(message, nai_prompts)
    n = int(settled["n_images"])

    # persist the count actually being painted: the UI reads n_images for its
    # progress placeholders, and 重绘 reuses this dict verbatim
    if settled is not nai_prompts:
        nai_prompts = settled
        Message.objects.filter(id=message.id, status="pending").update(
            nai_prompts=nai_prompts
        )

    _check_cancel(message.id)
    _set_phase(message.id, f"painting:0/{n}")

    args = (
        nai_prompts.get("base_prompt", ""),
        nai_prompts.get("character_prompts") or [],
        nai_prompts.get("negative_prompt", ""),
        nai_prompts.get("size", "portrait"),
    )
    tier = nai_prompts.get("tier") or gensettings.DEFAULTS["quality"]

    produced = 0
    errors: list[Exception] = []
    # A picture's position is its place in this message's album, not in this
    # batch. _paint runs more than once on the same row — a reflection round
    # repaints it, and 重绘 adds to it — so starting from zero every time would
    # give two pictures the same place and leave their order to chance.
    taken = [a.position for a in message.attachments.filter(kind="generated")]
    already = (max(taken) + 1) if taken else 0

    def keep(index: int, png: bytes, seed: int) -> bool:
        """
        Save one finished image — even if the turn was cancelled meanwhile.

        By the time an image exists NovelAI has already been billed for it, so
        throwing it away would charge the user for nothing. All N requests are
        in flight before the first result lands, so a cancel cannot avoid the
        cost either; the honest thing is to keep the work. The message itself
        still ends up "cancelled" — it simply carries what it produced.
        """
        _save_generated_image(message, png, seed, already + index)
        return True

    if n == 1:
        png, got = novelai.generate_one(*args, seed=seed, tier=tier)
        if keep(0, png, got):
            produced = 1
    else:
        with ThreadPoolExecutor(max_workers=n) as pool:
            futures = {
                pool.submit(_generate_in_thread, *args, seed=seed if i == 0 else None,
                            tier=tier): i
                for i in range(n)
            }
            done = 0
            for fut in as_completed(futures):
                done += 1
                try:
                    png, seed = fut.result()
                except Exception as exc:  # noqa: BLE001 — one image may fail
                    errors.append(exc)
                    log.warning("one image of %d failed: %s", n, exc)
                    continue
                if keep(futures[fut], png, seed):
                    produced += 1
                _set_phase(message.id, f"painting:{done}/{n}")

    # only fail the turn when nothing at all came back
    if errors and produced == 0:
        raise errors[0]
    if errors:
        # the pictures that failed are said on the row, not only in the log:
        # a user who asked for four and got three is owed the reason
        raw = Message.objects.filter(id=message.id).values_list("model_raw", flat=True).first()
        raw = raw if isinstance(raw, dict) else {}
        Message.objects.filter(id=message.id).update(model_raw={
            **raw, "paint_errors": [ui_text.PAINT_ONE_FAILED.format(n=n, error=str(e)[:200]) for e in errors]})
    _check_cancel(message.id)
    return produced


#: Statuses that say the account cannot have this, now or in a minute. Out of
#: Anlas, key revoked, not allowed. Everything else — a timeout, a 429, a 500,
#: a reply that would not parse — is a bad moment, which is what a retry is for.
_DEAD_END = frozenset({401, 402, 403})


def dead_end(exc: Exception) -> bool:
    """
    Whether trying this again could possibly go differently.

    An empty Anlas balance answers every request the same way, so the free
    retries inside a turn, the run's own retry budget, and the next round of a
    continuous run all spend themselves against it and change nothing — 102 of
    those in one afternoon is what this is here to stop. It is not a failure
    to be absorbed; it is an answer, and the run is over.
    """
    return getattr(exc, "status", None) in _DEAD_END


def _with_retries(what: str, message_id, attempt):
    """
    One step of a turn, with a couple of free goes before it counts as failed.

    The provider already retries an answer it cannot parse, but that is not the
    only way a step comes back useless: a model can return perfectly valid JSON
    with an empty base_prompt, NovelAI can time out, a vendor can be having a
    minute. None of those were retried at all — the turn simply failed, and the
    user pressed 重跑 by hand, which is the same retry with a person doing the
    waiting.

    A cancel is not a failure and is never retried, and neither is an answer
    that will be the same answer next time — see dead_end.
    """
    last: Exception | None = None
    for i in range(settings.FREE_RETRIES_IN_RUN + 1):
        try:
            return attempt()
        except PipelineCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 — deciding whether to retry IS the job
            last = exc
            if dead_end(exc):
                break
            if i >= settings.FREE_RETRIES_IN_RUN:
                break
            log.warning("%s failed (%s), retrying %d/%d",
                        what, str(exc)[:160], i + 1, settings.FREE_RETRIES_IN_RUN)
            _check_cancel(message_id)
            _set_phase(message_id, f"retry:{what}")
    assert last is not None
    raise last


@dataclass
class DrawContext:
    """
    What a picture is made from besides the brief.

    `selection` is the library as switched on for this turn; `source` is the
    picture being changed when this is an edit (None for a new one); `seed` is
    the seed the first picture follows. Built once per turn and handed to every
    draw of that turn, reflection redraws included, so a redraw cannot quietly
    differ from the thing it is redrawing.
    """

    selection: library.Selection
    source: dict | None = None
    seed: int | None = None
    #: what NovelAI received for the picture `source` is -- an edit that
    #: sends the same words again has changed nothing
    source_prompts: dict | None = None
    #: a reflection round repaints the same turn: the drawing keeps its name
    #: (the thing the user is pointing at must not get renamed under them)
    same_drawing: bool = False

    @property
    def lines(self) -> dict:
        return library.lines_map(self.selection)

    @property
    def lines_text(self) -> str:
        return library.as_lines(self.selection)

    @property
    def setting_lines(self) -> dict:
        return library.setting_lines_map(self.selection)

    @property
    def style_lines(self) -> dict:
        return library.style_lines_map(self.selection)

    def truth(self) -> dict[str, list]:
        names = {ch.get("from") for ch in (self.source or {}).get("characters") or []}
        return {n: library.truth_table(self.selection, n) for n in names if n}

    def setting_truth(self) -> dict[str, list]:
        return library.setting_truth(self.selection)


def _prompts_and_paint(message: Message, brief: dict, ctx: DrawContext) -> tuple[dict, list]:
    """
    One picture, start to finish: brief -> tags -> canvas.

    An edit (`ctx.source` set) hands the drawing model the picture being
    changed and takes back a change list; a new picture is written from the
    brief. Either way the style slot is the style entries' lines, filled by
    the app rather than written by the model, and the first picture follows
    `ctx.seed`.
    """
    _set_phase(message.id, "prompting")
    on_retry = lambda _r: _set_phase(message.id, "retry:prompting")  # noqa: E731
    on_lookup = lambda n: _set_phase(message.id, f"lookup:{n}")      # noqa: E731
    # The style slot is the library's: the settings' style lines go in at
    # render time, every time, patched only as far as this picture's setting
    # patches say (prompt.render). The picture itself stores what the model
    # wrote and which lines it picked or patched -- like a character's lines.
    if ctx.source is not None:
        draw = llm.draw_edit(ctx.source, brief, ctx.truth(), ctx.lines_text, ctx.lines,
                             on_retry=on_retry, on_lookup=on_lookup,
                             setting_lines=ctx.setting_lines, setting_truth=ctx.setting_truth(),
                             style_lines=ctx.style_lines)
    else:
        draw = llm.draw_turn(brief, ctx.lines_text, ctx.lines,
                             on_retry=on_retry, on_lookup=on_lookup,
                             setting_lines=ctx.setting_lines, style_lines=ctx.style_lines)
    _check_cancel(message.id)
    picture = prompt_slots.upgrade(draw.get("picture") or {})
    nai_prompts = llm.prompts_from(draw)
    if ctx.same_drawing and isinstance(message.nai_prompts, dict) and message.nai_prompts.get("draw_id"):
        nai_prompts["draw_id"] = message.nai_prompts["draw_id"]
    # The lead wrote a change because the user asked for one. An edit that
    # sends NovelAI the very words it got for the picture being edited is not
    # a judgement that nothing needed doing -- it is the model giving up (one
    # reasoned for 66k characters about clothes it could not find and then
    # changed nothing). Painted, it is the same picture under the same seed,
    # paid for twice. Compared on the words, not the slot table: a line the
    # lead rewrote this turn changes the words without any patch.
    if ctx.source is not None and llm.same_words(nai_prompts, ctx.source_prompts):
        raise llm.ModelError(ui_text.EDIT_CHANGED_NOTHING)
    message.refresh_from_db()
    # settle the count here, so the dict stored, the dict painted and the dict
    # returned to a reflection round are all the same one
    nai_prompts = _with_count(message, nai_prompts)
    fields: dict = {"kind": "image", "nai_prompts": nai_prompts, "draw_brief": brief,
                    # what nai_prompts was assembled from, for the next turn to edit
                    "prompt": picture}
    # The drawing model's working, what it looked up, what it changed and what
    # the slot check had to say are kept beside the lead's rather than inside
    # nai_prompts, which 重绘 replays verbatim. A reflection redraw overwrites
    # them: the turn keeps what is behind the picture it actually kept.
    kept = {k: v for k, v in (
        ("draw_reasoning", draw.get("reasoning")),
        ("draw_lookups", draw.get("lookups")),
        ("draw_patch", draw.get("patch")),
        ("draw_notes", prompt_slots.check(picture, ctx.lines, library.parts_map(ctx.selection),
                                          ctx.setting_lines)
         + prompt_slots.lint(nai_prompts, picture)),
    ) if v}
    before = {a.id for a in message.attachments.filter(kind="generated")}
    try:
        _paint(message, nai_prompts, seed=ctx.seed)
    finally:
        # Written after the paint, not before, and even when the paint was
        # cut short: the row's prompt must be the prompt behind the pictures
        # it holds. A reflection redraw whose paint fails keeps the previous
        # round's pictures, and used to keep this attempt's slot table --
        # the next edit then started from a picture nobody ever saw. What
        # was painted (or part-painted, under a cancel) is what is stored.
        if message.attachments.filter(kind="generated").exclude(id__in=before).exists():
            # a cancel flips the row to "cancelled" at once, and the pictures
            # that landed anyway are kept (they are paid for): their prompt
            # is stored with them, whatever the row's status says by now.
            # model_raw is re-read: the paint may just have left a note on it
            if kept:
                message.refresh_from_db(fields=["model_raw"])
                fields["model_raw"] = {**(message.model_raw or {}), **kept}
            Message.objects.filter(id=message.id, status__in=("pending", "cancelled")).update(**fields)
    message.refresh_from_db()
    fresh = [
        a for a in message.attachments.filter(kind="generated").order_by("position")
        if a.id not in before
    ]
    return nai_prompts, fresh


def main_seed(message: Message | None) -> int | None:
    """The seed of a drawing's main picture, or None when there is none."""
    if message is None:
        return None
    pictures = list(message.attachments.filter(kind="generated").order_by("position"))
    if not pictures:
        return None
    # main_index is a position; a position that is gone (a reflection round
    # discarded it) means the first picture that is left
    main = next((a for a in pictures if a.position == message.main_index), pictures[0])
    return main.seed


def drawing_message(conversation: Conversation, draw_id: str, before_seq: int) -> Message | None:
    """The turn that made drawing `draw_id`, if it is in this conversation."""
    if not draw_id:
        return None
    for m in (real_turns(conversation).filter(role="assistant", seq__lt=before_seq)
              .order_by("-seq")):
        if (m.nai_prompts or {}).get("draw_id") == draw_id:
            return m
    return None


def latest_drawing(conversation: Conversation, before_seq: int) -> Message | None:
    """The most recent turn that produced pictures."""
    for m in (real_turns(conversation).filter(role="assistant", seq__lt=before_seq)
              .order_by("-seq").prefetch_related("attachments")):
        if any(a.kind == "generated" for a in m.attachments.all()):
            return m
    return None


def _discard_images(attachments: list) -> None:
    """
    Drop pictures a later reflection round has replaced.

    Reflection is defined as leaving the turn holding only the last round's
    work, so these have to go — but the file goes with the row. An orphaned
    blob in MEDIA_ROOT is the kind of thing nobody notices for a year.

    The row always goes. The file only goes if this was the last row pointing
    at it: branching gives a new conversation its own rows over the same
    stored file, and there is no reading of this function's own arguments that
    can tell whether one of those exists. Asking is one COUNT, and the thing
    it prevents is a picture disappearing out of a conversation nobody was
    even looking at.
    """
    for att in attachments:
        name = att.file.name
        att.delete()
        if name and Attachment.objects.filter(file=name).exists():
            continue
        try:
            default_storage.delete(name)
        except Exception as exc:  # noqa: BLE001 — a missing file must not fail the turn
            log.warning("could not remove superseded image file: %s", exc)


def _reflection_rounds(message: Message) -> int:
    """What the slider asked for on this turn, clamped to what is allowed."""
    asked = message.force_reflection_rounds or 0
    return min(max(int(asked), 0), settings.MAX_REFLECTION_ROUNDS)


def _snapshot_lines(snapshot: dict | None) -> dict[str, dict[str, str]]:
    """{entry name: {line id: tags}} out of a turn's selection receipt: the
    lines as they read when that picture was drawn."""
    out: dict[str, dict[str, str]] = {}
    for s in (snapshot or {}).get("settings") or []:
        out[str(s.get("title") or "")] = {ln["id"]: ln["tags"] for ln in s.get("tags") or [] if ln.get("id")}
    for c in (snapshot or {}).get("characters") or []:
        book = {ln["id"]: ln["tags"] for ln in c.get("tags") or [] if ln.get("id")}
        for v in c.get("variants") or []:
            book.update({ln["id"]: ln["tags"] for ln in v.get("tags") or [] if ln.get("id")})
        out[str(c.get("name") or "")] = book
    return out


def _source_tags(conversation: Conversation, before_seq: int, copy_from: dict | None,
                 kind: str = "character") -> str:
    """
    The words a `copy_from` points at. For a character: what that drawing's
    subject actually wore -- the lines it used, as they read when it was
    drawn (the turn's receipt), and its look. The pose is not copied: it is
    that picture's moment, not the character. For a setting: what the picture
    said about style, scene and light -- the style lines, the picture's own
    style tags, the picked setting lines, and those slots' sentences.
    """
    if not isinstance(copy_from, dict):
        return ""
    src = drawing_message(conversation, str(copy_from.get("picture") or ""), before_seq)
    if src is None or not isinstance(src.prompt, dict):
        return ""
    picture = prompt_slots.upgrade(src.prompt)
    then = _snapshot_lines(src.selection_snapshot)
    if kind == "setting":
        words: list[str] = []
        prose: list[str] = []
        for pick in picture.get("settings") or []:
            book = then.get(pick.get("from") or "", {})
            words += [book[k] for k in (pick.get("use") or []) if k in book]
        for title, book in then.items():
            # the style lines are in every picture whether picked or not
            parts = {ln["id"]: ln["part"] for s in (src.selection_snapshot or {}).get("settings") or []
                     if s.get("title") == title for ln in s.get("tags") or []}
            words += [t for k, t in book.items() if wording.is_part(parts.get(k), "style")]
        for name in ("style", "scene", "light"):
            half = picture["slots"][name]
            words += prompt_slots.tags(half["tags"])
            prose.append(half["prose"])
        return prompt_slots.glue([prompt_slots.join(words), prompt_slots.sentences(prose)])
    key = str(copy_from.get("subject") or "").strip()
    chars = picture.get("characters") or []
    who = next((c for c in chars if c.get("key") == key), None) or (chars[0] if chars else None)
    if who is None:
        return ""
    book = then.get(who.get("from") or "")
    if book is None:
        # a picture from before receipts carried lines: the entry as it reads now
        book = library.lines_map(library.selection_for(conversation)).get(who.get("from") or "", {})
    # the lines as the entry has them, not as this picture patched them: a
    # patch is this picture's state (sleeves rolled up, cardigan off), and
    # copying it would make the state permanent
    used = [book[k] for k in (who.get("use") or []) if k in book]
    # A subject drawn from lines has a look that is this picture's extras only
    # (a borrowed coat, wet hair) -- the drawing rules say so -- not what it
    # looks like; copied, the extra would become permanent. Only a subject
    # without lines (a passer-by, a prose-only entry) has its whole appearance
    # in look.
    # tags and sentence set apart the way a prompt is: a sentence glued to
    # tags with ", " was copied into a line stop and all, and the line went
    # out to NovelAI as 「...at her waist., sitting」
    if used:
        return prompt_slots.join(used)
    return prompt_slots.glue([prompt_slots.join(prompt_slots.tags(who["look"]["tags"])),
                              prompt_slots.sentences([who["look"]["prose"]])])


def _resolve_copies(conversation: Conversation, message: Message, edits: dict) -> dict:
    """
    Turn every `copy_from` into concrete tag lines, now, while the picture is
    at hand. The identity line is the lead's own words (a name is not a thing
    one sees in a picture); every other line keeps only words the picture had.
    """
    out = {"settings": [], "characters": []}
    misses: list[str] = []

    def missed(item: dict) -> None:
        cf = item.get("copy_from")
        if isinstance(cf, dict) and cf.get("picture"):
            misses.append(ui_text.COPY_FROM_MISSED.format(
                name=item.get("name") or item.get("title") or "", picture=cf.get("picture")))

    for item in edits.get("settings") or []:
        item = dict(item)
        source = _source_tags(conversation, message.seq, item.get("copy_from"), kind="setting")
        if not source:
            missed(item)
        if source:
            lines, dropped = library.copied_lines(source, item.get("tags"))
            item["tags"] = lines
            if dropped:
                item["dropped"] = dropped
        item.pop("copy_from", None)
        out["settings"].append(item)
    for item in edits.get("characters") or []:
        item = dict(item)
        source = _source_tags(conversation, message.seq, item.get("copy_from"))
        if not source:
            missed(item)
        variants = [dict(v) for v in item.get("variants") or []]
        if source:
            taken: set[str] = set()
            raw = item.get("tags") or []
            ident = [x for x in raw if wording.is_part(x.get("part"), "identity")]
            rest = [x for x in raw if not wording.is_part(x.get("part"), "identity")]
            # the identity line's words count as claimed, or the catch-all
            # line at the end would collect them a second time
            for x in ident:
                taken |= {t.strip().lower().replace("_", " ")
                          for t in str(x.get("tags") or "").split(",") if t.strip()}
            lines, dropped = library.copied_lines(source, rest, taken=taken, keep_rest=False)
            item["tags"] = [{"id": "", **x} for x in ident] + lines
            for i, v in enumerate(variants):
                vsrc = _source_tags(conversation, message.seq, v.get("copy_from")) or source
                vlines, vdropped = library.copied_lines(
                    vsrc, v.get("tags"), taken=taken, keep_rest=(i == len(variants) - 1))
                v["tags"] = vlines
                dropped += vdropped
                v.pop("copy_from", None)
            if dropped:
                item["dropped"] = dropped
        else:
            for v in variants:
                v.pop("copy_from", None)
        item.pop("copy_from", None)
        item["variants"] = variants
        out["characters"].append(item)
    if misses:
        raw = Message.objects.filter(id=message.id).values_list("model_raw", flat=True).first()
        raw = raw if isinstance(raw, dict) else {}
        Message.objects.filter(id=message.id).update(model_raw={**raw, "edit_notes": misses})
    return out


def _record_proposal(conversation: Conversation, message: Message, edits: dict) -> None:
    """
    Write the lead model's library edits down, and apply what may be applied.

    Two layers, two rules. The conversation layer is the model's own notes for
    this conversation — a character it just made up, the rain that falls in
    this story — so those are written at once; the user can delete or promote
    them from the sidebar. The project layer is the user's library, so those
    edits are parked for approval, or approved on the user's behalf when the
    setting says so. Either way the proposal row is written first, so nothing
    is lost whichever way the second step goes.
    """
    try:
        edits = _resolve_copies(conversation, message, edits)
        if conversation.folder is None:
            # no project to write to: everything the model records lives in
            # this conversation, and says so
            for kind in ("settings", "characters"):
                for entry in edits.get(kind) or []:
                    entry["scope"] = "conversation"
        edits = _drop_noop_edits(conversation, edits)
        if not edits:
            return
        parked = proposals.record(conversation, message, edits)
        log.info("library proposal recorded for message %s", message.id)
        if parked.status == "pending":
            proposals.auto_approve(parked, gensettings.of(conversation)["auto_library"])
    except Exception as exc:  # noqa: BLE001 — a proposal must not fail the turn
        log.warning("could not record library proposal: %s", exc)
        raw = Message.objects.filter(id=message.id).values_list("model_raw", flat=True).first()
        raw = raw if isinstance(raw, dict) else {}
        Message.objects.filter(id=message.id).update(model_raw={
            **raw, "edit_notes": [*(raw.get("edit_notes") or []),
                                  ui_text.PROPOSAL_NOT_RECORDED.format(error=str(exc)[:200])]})


def _drop_noop_edits(conversation: Conversation, edits: dict) -> dict:
    """
    Strip edits that would not change anything.

    The lead model often restates a setting or a costume it can already see in
    the selected library, which would surface as a proposal the user has to
    dismiss for no reason. Compare against what is stored and keep only the
    parts that actually differ.
    """
    folder = conversation.folder
    reach = [f.id for f in folder.ancestors()] if folder else []

    def same_lines(stored, proposed) -> bool:
        return [(ln["part"], ln["tags"]) for ln in library.lines_of(stored)] == \
            [(ln.get("part"), ln.get("tags")) for ln in library.lines_of(proposed)]

    def find(model, **lookup):
        own = model.objects.filter(conversation=conversation, **lookup).first()
        if own:
            return own
        return model.objects.filter(folder_id__in=reach, conversation__isnull=True,
                                    **lookup).first() if reach else None

    settings_out = []
    for item in edits.get("settings") or []:
        title, content = item.get("title", ""), item.get("content", "")
        existing = find(Setting, title=title)
        if (existing is None or existing.content.strip() != content.strip()
                or not same_lines(existing.tags, item.get("tags"))
                or (item.get("scope") == "project" and existing.conversation_id)):
            settings_out.append(item)

    chars_out = []
    for item in edits.get("characters") or []:
        name = item.get("name", "")
        existing = find(Character, name=name)
        if existing is None:
            chars_out.append(item)
            continue

        desc = (item.get("description") or "").strip()
        # blank description means "leave it alone", so it is never a change
        desc_changed = bool(desc) and desc != existing.description.strip()
        lines_changed = bool(item.get("tags")) and not same_lines(existing.tags, item.get("tags"))
        promoted = item.get("scope") == "project" and bool(existing.conversation_id)

        stored = {v.title: v for v in existing.variants.all()}
        variants = [
            v for v in (item.get("variants") or [])
            if v.get("title", "") not in stored
            or stored[v["title"]].content.strip() != (v.get("content") or "").strip()
            or (v.get("tags") and not same_lines(stored[v["title"]].tags, v.get("tags")))
        ]
        if desc_changed or lines_changed or promoted or variants:
            chars_out.append({**item, "variants": variants})

    if not settings_out and not chars_out:
        return {}
    return {"settings": settings_out, "characters": chars_out}


def _fmt_tokens(n: int) -> str:
    return notices.fmt_tokens(n)


def maybe_context_notice(conversation: Conversation, prompt_tokens: int | None) -> None:
    """
    Append an app-authored note when this conversation's context grows past a
    budget line. Two lines, each fired at most once per conversation:

      long_context  the provider's long-context mark (grok-4.6 prices
                    double past 200k; deepseek-flash has no step, so it is
                    set where a conversation is simply worth noting)
      context_warn  approaching the hard context ceiling (default 80% of 500k)

    Not sent by the assistant and never fed back to the model.
    """
    provider = providers.active()
    limit = provider.context_limit
    if not prompt_tokens or limit <= 0:
        return

    warn_at = int(limit * settings.GROK_CONTEXT_WARN_RATIO)
    if prompt_tokens >= warn_at:
        kind = "context_warn"
        note = {"key": "CONTEXT_NEAR_LIMIT", "used": prompt_tokens, "limit": limit}
    elif prompt_tokens >= provider.long_context_threshold:
        kind = "long_context"
        note = {"key": provider.long_context_key, "threshold": provider.long_context_threshold}
    else:
        return

    try:
        with transaction.atomic():
            locked = Conversation.objects.select_for_update().get(id=conversation.id)
            if locked.messages.filter(role="system", notice_kind=kind).exists():
                return
            agg = locked.messages.aggregate(m=Max("seq"))
            Message.objects.create(
                conversation=locked,
                seq=(agg["m"] or 0) + 1,
                role="system",
                status="done",
                notice_kind=kind,
                notice=note,
                content=notices.render(note),
            )
        log.info("context notice %s emitted for conv %s (%s tokens)",
                 kind, conversation.id, prompt_tokens)
    except Exception as exc:  # noqa: BLE001 — a note must never fail the turn
        log.warning("could not write context notice: %s", exc)


def _finish_stopped(message: Message) -> None:
    """
    The user pressed stop. Work out what that should leave behind.

    Stop means "no more", not "undo". If the turn already produced pictures —
    a reflection round in, or two of four painted — they exist, they were paid
    for, and they are what the user asked for. So the turn finishes as an
    ordinary one.

    That is not only about the label. A cancelled row is excluded from
    build_history, so a picture kept under that status would be invisible to
    the model for the rest of the conversation: you would stop a reflection,
    keep the picture, and then find she could not see it. Finishing properly is
    what makes it a turn rather than a loose file.

    Stopping before anything came back is still a cancel — there is nothing
    there to keep.
    """
    kept = message.attachments.filter(kind="generated").exists()
    if not kept:
        _finish(message.id, status="cancelled", error="")
        return
    # the cancel endpoint already flipped the row, so this update cannot be
    # guarded by status="pending" the way _finish is
    # (`error` on a done row is a note, not a failure: the pictures stay
    # in the lead's history and the transcript says the turn was stopped)
    Message.objects.filter(id=message.id).exclude(status="done").update(
        status="done", error=ui_text.STOPPED_KEPT_PICTURES, phase="", heartbeat_at=timezone.now()
    )
    log.info("message %s stopped, keeping what it made", message.id)


def _finish(message_id, **fields) -> int:
    """
    Finalize a job — only if it is still pending (a cancel wins). Returns the
    number of rows updated, so callers can skip follow-up work (titling,
    notices) for a job that was cancelled out from under them.
    """
    fields.setdefault("phase", "")
    fields["heartbeat_at"] = timezone.now()
    return Message.objects.filter(id=message_id, status="pending").update(**fields)


# ---------------------------------------------------------------- entry points


def run_generation(message_id) -> None:
    """
    Complete a pending assistant message (full pipeline: models → novelai).
    Safe to call in a worker thread or synchronously (MCP path).
    """
    try:
        message = Message.objects.select_related("conversation").get(id=message_id)
    except Message.DoesNotExist:
        return
    if message.status != "pending":
        return

    conversation = message.conversation
    try:
        # the watchdog proves this worker is alive through long blocking calls
        # (model retries, NovelAI) so the stale-job reaper leaves it alone
        with _Heartbeat(message.id):
            _check_cancel(message.id)
            # the layer as this turn finds it, for a branch at the message
            # before: what the user would have continued from
            Message.objects.filter(id=message.id, status="pending").update(
                library_before=library.own_layer(conversation))
            _set_phase(message.id, "thinking")
            _check_cancel(message.id)

            # what the user has switched on in this conversation's library —
            # appended for the lead model, and handed to the drawing model,
            # which has no other way of knowing about it
            selection = library.selection_for(conversation)

            sent_refs: dict = {}
            grok_messages = build_history(
                conversation,
                before_seq=message.seq,
                selection=selection,
                sticky_refs=message.sticky_refs,
                sent=sent_refs,
            )
            # a retry is the user's business: they are watching a spinner
            result = _with_retries(
                "thinking",
                message.id,
                lambda: llm.lead_turn(
                    grok_messages,
                    on_retry=lambda _r: _set_phase(message.id, "retry:thinking"),
                    on_lookup=lambda n: _set_phase(message.id, f"lookup:{n}"),
                ),
            )
            _check_cancel(message.id)

            # guarded by status="pending": a cancel between the grok call and
            # here discards the reply instead of writing it onto a dead row
            prompt_tokens = (result.get("usage") or {}).get("prompt_tokens")
            Message.objects.filter(id=message.id, status="pending").update(
                content=result["reply"],
                model_raw={**result, "references_sent": sent_refs.get("references", [])},
                prompt_tokens=prompt_tokens,
                draw_brief=result.get("draw"),
                selection_snapshot=library.snapshot(selection),
                library_snapshot=library.own_layer(conversation),
                heartbeat_at=timezone.now(),
            )

            # The model may want to write to the library. Project-layer edits
            # are parked for the user to approve (or approved for them, per
            # the setting); conversation-layer ones are the model's own notes
            # for this conversation and are written at once — a character it
            # just made up is drawn from its lines in this very turn.
            touched = False
            if result.get("edits"):
                _record_proposal(conversation, message, result["edits"])
                touched = True
            # A project character the lead named that this conversation had
            # not switched on: switched on now, costume and all, so the
            # picture is drawn from its lines.
            if result.get("draw"):
                named = library.switch_on(conversation, (result["draw"] or {}).get("subjects") or [])
                if named:
                    log.info("switched on by name: %s", "、".join(named))
                    touched = True
            if touched:
                selection = library.selection_for(conversation)
                # The layer as it stands after this turn's notes, which is
                # what a branch made at this message should start from -- and
                # the selection the picture below is drawn from, which is the
                # receipt's business. Taken before the notes, the receipt of a
                # turn that made up a character and drew her at once said she
                # was drawn from nothing.
                Message.objects.filter(id=message.id).update(
                    selection_snapshot=library.snapshot(selection),
                    library_snapshot=library.own_layer(conversation))

            produced = 0
            if result.get("draw"):
                # An edit of a drawing already made, or a new picture. The
                # judgement is the lead's; which picture of that drawing is
                # the app's — its main one — and so is the seed to follow.
                source_msg = drawing_message(conversation, result.get("continues", ""),
                                             message.seq)
                if result.get("continues") and source_msg is None:
                    log.info("continues=%s names no drawing here; drawing new",
                             result["continues"])
                # A change is a change to some picture. When the lead wrote one
                # and named no drawing -- it does, now and then -- the picture
                # it means is the latest one: a change read as a new picture
                # throws away everything it did not mention, which is the
                # expensive mistake; a change applied to the wrong picture is
                # the cheap one.
                if source_msg is None and (result["draw"].get("change") or "").strip():
                    source_msg = latest_drawing(conversation, message.seq)
                # An edit needs the picture's slot table. A drawing without one
                # (a row from before pictures had slots) cannot be edited, and
                # drawing a new picture from the change text alone is not an
                # edit: the user gets a cloud and nothing else, and pays for it.
                if source_msg is not None and not isinstance(source_msg.prompt, dict):
                    raise NoPictureToEdit(ui_text.EDIT_WITHOUT_PICTURE)
                follow = source_msg or latest_drawing(conversation, message.seq)
                ctx = DrawContext(
                    selection=selection,
                    source=(source_msg.prompt or None) if source_msg else None,
                    source_prompts=(source_msg.nai_prompts or None) if source_msg else None,
                    seed=main_seed(follow),
                )
                # the brief carries which drawing it continues, so the
                # transcript can say so and a misjudgement is visible
                if source_msg:
                    result["draw"] = {**result["draw"],
                                      "continues": (source_msg.nai_prompts or {}).get("draw_id")}
                # a second, context-free call turns the brief into tags
                # Reflection runs its own policy on top of this one, so the
                # retries here are for the ordinary turn: inside a run the
                # redraw goes through _redraw_with_retries instead.
                nai_prompts, images = _with_retries(
                    "prompting",
                    message.id,
                    lambda: _prompts_and_paint(message, result["draw"], ctx),
                )
                produced = len(images)

                rounds = _reflection_rounds(message)
                if rounds and produced:
                    outcome = reflection.run(
                        base_history=grok_messages,
                        first_reply=result.get("reply") or "",
                        brief=result["draw"],
                        nai_prompts=nai_prompts,
                        images=images,
                        rounds=rounds,
                        hooks=reflection.Hooks(
                            # a reflection redraw is an edit of the picture it
                            # is looking at, following that picture's seed
                            redraw=lambda brief: _prompts_and_paint(
                                message, brief,
                                DrawContext(
                                    selection=ctx.selection,
                                    source=Message.objects.get(id=message.id).prompt,
                                    source_prompts=Message.objects.get(id=message.id).nai_prompts,
                                    same_drawing=True,
                                    seed=main_seed(Message.objects.get(id=message.id)),
                                ),
                            ),
                            check_cancel=lambda: _check_cancel(message.id),
                            set_phase=lambda phase: _set_phase(message.id, phase),
                            enter_round=_phase_scope,
                            image_url=_image_data_url,
                            discard=_discard_images,
                            # a cancel is a decision, not a hiccup: it must not
                            # be swallowed by the retry policy
                            fatal=(PipelineCancelled,),
                            hopeless=dead_end,
                        ),
                    )
                    # only the last round survives: the turn carries that
                    # brief, those tags and those pictures, and a log of how it
                    # got there that no model will ever read
                    nai_prompts = outcome.nai_prompts
                    produced = len(outcome.images)
                    # the drawing it continues is the turn's, whichever round
                    # drew it: the reflection brief does not say, the first did
                    continued = (result["draw"] or {}).get("continues")
                    Message.objects.filter(id=message.id, status="pending").update(
                        draw_brief=({**outcome.brief, "continues": continued}
                                    if continued and isinstance(outcome.brief, dict) else outcome.brief),
                        nai_prompts=outcome.nai_prompts,
                        reflection_log=outcome.log or None,
                    )
                    log.info(
                        "reflection ran %d round(s) for %s (redrew %d)",
                        outcome.rounds_run,
                        message.id,
                        sum(1 for r in outcome.log if r.get("redrew")),
                    )

            # a zero here means the turn was cancelled while we were finishing —
            # skip the follow-up work (titling costs a model call) in that case
            if _finish(message.id, status="done"):
                log.info(
                    "message %s done (images=%d, conv=%s)",
                    message.id,
                    produced,
                    conversation.id,
                )
                _maybe_title(conversation, message)
                maybe_context_notice(conversation, prompt_tokens)
    except PipelineCancelled:
        _finish_stopped(message)
        log.info("message %s stopped", message.id)
    except Exception as exc:  # noqa: BLE001
        log.exception("pipeline failed for message %s", message.id)
        _keep_unparsed(message.id, exc)
        _finish(message.id, status="error", error=str(exc)[:2000])
        if dead_end(exc):
            # Decided here, holding the exception, rather than later from the
            # error string: a run that keeps going costs one wasted turn per
            # tick for as long as nobody is watching.
            log.warning("continuous: closing the run, this will not get better")
            continuous.close_open_run(message.conversation, "failed")
    finally:
        Conversation.objects.filter(id=conversation.id).update(
            updated_at=timezone.now()
        )
        # A nudge, not the mechanism. However this turn went, a run it belongs
        # to should get on with the next one now rather than in a few seconds —
        # but continuous.tick() would reach exactly the same conclusion from
        # the same rows, so losing this call costs latency and nothing else.
        try:
            continuous.reconcile(message.conversation_id)
        except Exception:  # noqa: BLE001 — a run must not take the turn down
            log.exception("could not reconcile the continuous run for %s", message.id)


def run_redraw(message_id) -> None:
    """
    Complete a pending assistant message that already carries nai_prompts
    (redraw: NovelAI only, new random seeds, no model call).
    """
    try:
        message = Message.objects.select_related("conversation").get(id=message_id)
    except Message.DoesNotExist:
        return
    if message.status != "pending" or not message.nai_prompts:
        return
    try:
        with _Heartbeat(message.id):
            _check_cancel(message.id)
            prompts = message.nai_prompts or {}
            produced = _with_retries(
                "painting", message.id, lambda: _paint(message, prompts)
            )
            _finish(message.id, status="done", kind="image")
            log.info("redraw %s done (images=%d)", message.id, produced)
    except PipelineCancelled:
        _finish_stopped(message)
    except Exception as exc:  # noqa: BLE001
        log.exception("redraw failed for message %s", message.id)
        _keep_unparsed(message.id, exc)
        _finish(message.id, status="error", error=str(exc)[:2000])
    finally:
        Conversation.objects.filter(id=message.conversation_id).update(
            updated_at=timezone.now()
        )


def _maybe_title(conversation: Conversation, message: Message) -> None:
    """Auto-title after the first successful assistant turn."""
    try:
        conversation.refresh_from_db()
        # untitled in whichever language it was made in
        untitled = ui_text.in_every_language("NEW_CONVERSATION_TITLE")
        if conversation.title not in untitled:
            return
        first_user = (
            conversation.messages.filter(role="user", archived=False)
            .order_by("seq")
            .first()
        )
        message.refresh_from_db()
        if not first_user:
            return
        title = llm.make_title(
            first_user.content or wording.TITLE_IMAGE_PLACEHOLDER, message.content or ""
        )
        if title:
            Conversation.objects.filter(id=conversation.id, title__in=untitled).update(
                title=title
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("auto-title failed: %s", exc)
