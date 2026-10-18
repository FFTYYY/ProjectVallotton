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
    assemble, continuous, drawnote, gensettings, imaging, library, llm, novelai,
    proposals, providers, reflection,
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
    own = llm.as_said({"settings": raw.get("settings"), "characters": raw.get("characters")}) or {}
    return json.dumps(
        {
            "reply": m.content or "",
            "settings": own.get("settings") or [],
            "characters": own.get("characters") or [],
            "edits": llm.as_said(raw.get("edits")) or None,
            # null is not our invention — it is the schema's own answer for
            # "no picture this turn", and what the model writes when it means it
            "draw": m.draw_brief or None,
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
                            m.nai_prompts, with_tags=with_tags, with_images=bool(urls),
                            exposed=library.exposed_keys_of(m),
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
    What a picture is made from besides the lead's description.

    `exposure` is the entries the lead exposed, resolved; `seed` is the seed
    the first picture follows; `source_prompts` is what NovelAI received for
    the picture this one continues, when it continues one -- a picture that
    sends the same words again has changed nothing; `same_drawing` is a
    reflection round repainting the same turn, which keeps the drawing's id.
    """

    exposure: library.Exposure
    seed: int | None = None
    source_prompts: dict | None = None
    same_drawing: bool = False


def _prompts_and_paint(message: Message, spec: dict, ctx: DrawContext) -> tuple[dict, list]:
    """
    One picture, start to finish: description + entries -> prompt -> canvas.

    The drawing model is handed the lead's description and the exposed
    entries and says what this picture does with each; the app assembles
    the prompt (assemble.py), has it cut down when NovelAI would not read it
    whole, and paints. What is stored is what was sent, beside the entries it
    was made from and the answer it was made with.
    """
    _set_phase(message.id, "prompting")
    on_retry = lambda _r: _set_phase(message.id, "retry:prompting")  # noqa: E731
    on_lookup = lambda n: _set_phase(message.id, f"lookup:{n}")      # noqa: E731
    draw = llm.draw(
        spec.get("description") or "", ctx.exposure.entries,
        text=spec.get("text") or "", size=spec.get("size") or "",
        on_retry=on_retry, on_lookup=on_lookup,
    )
    _check_cancel(message.id)
    nai_prompts = dict(draw["nai_prompts"])
    if ctx.same_drawing and isinstance(message.nai_prompts, dict) and message.nai_prompts.get("draw_id"):
        nai_prompts["draw_id"] = message.nai_prompts["draw_id"]
    # The lead asked for a change. A picture that sends NovelAI the very
    # words it got for the picture it continues is not a judgement that
    # nothing needed doing -- it is the model giving up. Painted, it is the
    # same picture under the same seed, paid for twice.
    if ctx.source_prompts is not None and llm.same_words(nai_prompts, ctx.source_prompts):
        raise llm.ModelError(ui_text.EDIT_CHANGED_NOTHING)
    # NovelAI paints with as much of a prompt as it reads and says nothing
    # about the rest, so the app counts first; a prompt it would not read
    # whole goes to the compressor (the drawing model again, told how far over)
    compressed = None
    if novelai.over_limit(novelai.prompt_tokens(nai_prompts)):
        _set_phase(message.id, "compressing")
        compressed = llm.compress(draw["asked"], nai_prompts,
                                  on_retry=lambda _r: _set_phase(message.id, "retry:compressing"))
        nai_prompts = compressed["nai_prompts"]
        _check_cancel(message.id)
    over = novelai.over_limit(novelai.prompt_tokens(nai_prompts))
    message.refresh_from_db()
    # settle the count here, so the dict stored, the dict painted and the dict
    # returned to a reflection round are all the same one
    nai_prompts = _with_count(message, nai_prompts)
    fields: dict = {
        "kind": "image", "nai_prompts": nai_prompts, "draw_brief": spec,
        # what the prompt was made from and made with, for the next picture
        # to inherit (the keys) and for the inspector (the rest)
        "prompt": {
            "description": spec.get("description") or "", "text": spec.get("text") or "",
            "size": spec.get("size") or "", "inherits": spec.get("inherits") or "",
            "exposed_keys": list(ctx.exposure.keys), "exposed": [dict(e) for e in ctx.exposure.entries],
            "answer": draw["answer"], "assembly": draw["assembly"],
        },
    }
    notes = assemble.lint(nai_prompts)
    if over:
        # NovelAI reads the first part of an over-long prompt and paints
        # from that; the person should know the rest went unread
        notes.append(ui_text.PROMPT_OVER_LIMIT.format(tokens=max(n for _, n in over), limit=novelai.limit()))
    if ctx.exposure.unknown:
        notes.append(ui_text.EXPOSE_UNKNOWN.format(keys="、".join(ctx.exposure.unknown)))
    kept = {k: v for k, v in (
        ("draw_reasoning", draw.get("reasoning")),
        ("draw_lookups", draw.get("lookups")),
        ("draw_notes", notes),
        ("compress", {k: compressed[k] for k in ("before", "counts", "changed", "fits", "tokens", "reasoning", "usage")}
         if compressed else None),
    ) if v}
    before = {a.id for a in message.attachments.filter(kind="generated")}
    try:
        _paint(message, nai_prompts, seed=ctx.seed)
    finally:
        # Written after the paint, not before, and even when the paint was
        # cancelled: the drawing model's work is this turn's whatever happened
        # to the canvas, and a later look at the turn should find it.
        raw = Message.objects.filter(id=message.id).values_list("model_raw", flat=True).first()
        raw = raw if isinstance(raw, dict) else {}
        Message.objects.filter(id=message.id).update(model_raw={**raw, **kept}, **fields)
    message.refresh_from_db()
    images = [a for a in message.attachments.filter(kind="generated").order_by("position")
              if a.id not in before]
    return nai_prompts, images


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


def _continues(round_draw: dict | None, own_id: str, first_draw: dict | None) -> str:
    """
    Which drawing a turn continues, for the record (the 改自 mark, the
    inspector): the one a reflection round names if it names another, else
    the one the lead's answer named. A round redraws the same turn, so its
    empty `inherits` -- or one naming this turn's own drawing -- is not a
    claim that the turn is new.
    """
    named = str((round_draw or {}).get("inherits") or "")
    if named and named != own_id:
        return named
    return str((first_draw or {}).get("inherits") or "")


def _exposure_check(conversation: Conversation, staged: library.Staged | None = None):
    """
    The lead's answer, checked before anything is drawn: the keys its draw
    exposes that the library does not hold, the turn has not written
    (`staged`, read when the check runs) and the answer does not write.
    Such a key is drawn as nothing -- a costume the picture silently lacks
    -- so the lead is told which, once, and answers again.
    """
    def check(answer: dict) -> str:
        missing = library.unwritten_exposure(conversation, answer, staged)
        return wording.EXPOSE_MISSING.format(keys="、".join(missing)) if missing else ""
    return check


def _write_library(conversation: Conversation, message: Message, result: dict) -> dict[str, dict]:
    """
    One answer's writes, staged and committed at once (the turn itself
    stages through reflection and commits at its end; see run_generation).
    Returns the project overlay the answer's edits make.
    """
    staged = library.Staged(seq=message.seq, in_project=conversation.folder is not None)
    staged.add(result, conversation)
    overlay = _overlay(conversation, staged)
    _commit_library(conversation, message, staged)
    return overlay


def _overlay(conversation: Conversation, staged: library.Staged) -> dict[str, dict]:
    """The project entries as this turn's edits leave them, for the exposure:
    the edit comes before the exposure whatever becomes of the card. An edit
    that changes nothing is no edit."""
    edits = staged.edits()
    edits = _drop_noop_edits(conversation, edits) if edits else None
    return library.overlay_from_edits(edits) if edits else {}


def _commit_library(conversation: Conversation, message: Message, staged: library.Staged) -> None:
    """
    What the turn wrote goes into the library, once.

    The conversation's own entries are written, overriding any of the same
    key -- they are the lead's notes for this conversation. Edits to the
    project become one proposal, or are approved on the user's behalf when
    the setting says so. What was done rides on the row for the inspector,
    and the row's copy of the layer (for a branch) is taken after.
    """
    settings_in, characters_in = staged.conversation_lists()
    notes: list[str] = []
    try:
        done = library.write_conversation_entries(conversation, staged.seq, settings_in, characters_in)
        if done:
            notes.append(ui_text.LIBRARY_WRITTEN.format(what="、".join(done)))
    except Exception as exc:  # noqa: BLE001 -- a bad entry must not fail the turn
        log.warning("could not write the conversation's entries: %s", exc)
        notes.append(ui_text.PROPOSAL_NOT_RECORDED.format(error=str(exc)[:200]))
    edits = staged.edits()
    if edits:
        edits = _drop_noop_edits(conversation, edits)
    if edits:
        try:
            parked = proposals.record(conversation, message, edits)
            log.info("library proposal recorded for message %s", message.id)
            proposals.auto_approve(parked, gensettings.of(conversation)["auto_library"])
        except Exception as exc:  # noqa: BLE001 -- a proposal must not fail the turn
            log.warning("could not record library proposal: %s", exc)
            notes.append(ui_text.PROPOSAL_NOT_RECORDED.format(error=str(exc)[:200]))
    if notes:
        raw = Message.objects.filter(id=message.id).values_list("model_raw", flat=True).first()
        raw = raw if isinstance(raw, dict) else {}
        Message.objects.filter(id=message.id).update(
            model_raw={**raw, "edit_notes": [*(raw.get("edit_notes") or []), *notes]})
    Message.objects.filter(id=message.id).update(library_snapshot=library.own_layer(conversation))


def _drop_noop_edits(conversation: Conversation, edits: dict) -> dict | None:
    """
    Strip project edits that would not change anything: the lead now and
    then restates an entry it can already see, which would surface as a
    proposal the user has to dismiss for no reason.
    """
    def same(obj, item: dict) -> bool:
        e = library.entry_dict(obj)
        note = str(item.get("note") or "").strip()
        return ((not note or library.same_words(note, e["note"]))
                and library.split_tags(item.get("positive")) == e["positive"]
                and library.split_tags(item.get("negative")) == e["negative"]
                and str(item.get("prose") or "").strip() == e["prose"])

    settings_out = []
    for item in edits.get("settings") or []:
        existing = library.find_setting(conversation, library.PROJECT_PREFIX + library.parse_key(str(item.get("key") or ""))[1])
        if existing is None or library.layer_of(existing) != "project" or not same(existing, item):
            settings_out.append(item)
    chars_out = []
    for item in edits.get("characters") or []:
        _, name, _ = library.parse_key(str(item.get("key") or ""))
        existing = library.find_character(conversation, library.PROJECT_PREFIX + name)
        if existing is None or library.layer_of(existing) != "project" or (
                not item.get("carrier") and not same(existing, item)):
            chars_out.append(item)
            continue
        stored = {v.title: v for v in existing.variants.all()}
        variants = []
        for v in item.get("variants") or []:
            _, _, title = library.parse_key(str(v.get("key") or ""))
            title = title or str(v.get("key") or "").strip()
            if title not in stored or not same(stored[title], v):
                variants.append(v)
        if variants:
            chars_out.append({**item, "variants": variants})
    if not settings_out and not chars_out:
        return None
    return {"settings": settings_out, "characters": chars_out}


def _draw_context(conversation: Conversation, message: Message, spec: dict, overlay: dict[str, dict],
                  staged: library.Staged | None = None) -> DrawContext:
    """
    The picture's context out of the lead's `draw`: the entries it exposes
    (on top of the ones the picture it continues exposed), the seed to
    follow and the words of the picture continued.

    The seed follows the main picture of the drawing continued, else of the
    latest drawing: a conversation keeps one seed until the user picks a
    different main picture. A drawing from before exposures were stored
    cannot be continued, only followed: the picture is drawn new.
    """
    wanted = spec.get("inherits") or ""
    source = drawing_message(conversation, wanted, message.seq)
    own = (message.nai_prompts or {}).get("draw_id") if isinstance(message.nai_prompts, dict) else None
    if source is None and wanted and wanted == own:
        # a reflection round continues the picture it is looking at: this
        # very turn, still pending, which real_turns does not list
        source = message
    if wanted and source is None:
        log.info("inherits=%s names no drawing here; drawing new", wanted)
    inherited = library.exposed_keys_of(source)
    exposure = library.resolve_exposure(conversation, spec.get("expose") or [], inherited=inherited,
                                        overlay=overlay, staged=staged)
    follow = source or latest_drawing(conversation, message.seq)
    return DrawContext(
        exposure=exposure,
        seed=main_seed(follow),
        # the words of a settled drawing, for the same-words check; a round
        # repainting its own turn is judged by the lead, not by that check
        source_prompts=(source.nai_prompts or None) if source is not None and source is not message and inherited else None,
    )


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


def _draw_turn(conversation: Conversation, message: Message, result: dict, staged: library.Staged,
               grok_messages: list[dict]) -> int:
    """
    The pictures of one turn: the lead's draw, then reflection. Everything
    is drawn from the library with the turn's staged writes laid over it;
    nothing is written to the library here (see run_generation). Returns
    how many pictures the turn kept.
    """
    produced = 0
    if result.get("draw"):
        ctx = _draw_context(conversation, message, result["draw"], _overlay(conversation, staged), staged)
        Message.objects.filter(id=message.id).update(selection_snapshot=library.snapshot(ctx.exposure))
        nai_prompts, images = _with_retries(
            "prompting",
            message.id,
            lambda: _prompts_and_paint(message, result["draw"], ctx),
        )
        produced = len(images)

        rounds = _reflection_rounds(message)
        if rounds and produced:
            def redraw(verdict: dict) -> tuple[dict, list]:
                # a reflection round may write entries too, and
                # continues the picture it is looking at unless it
                # names another; same drawing, same seed
                current = Message.objects.get(id=message.id)
                # the record of the picture the turn is holding, put
                # back when this round paints nothing: a round that
                # failed did not happen, and the row must go on
                # describing the picture it still has
                held = {f: getattr(current, f) for f in
                        ("kind", "nai_prompts", "draw_brief", "prompt", "model_raw", "selection_snapshot")}
                # the round's writes join the turn's; if its picture is not
                # kept, they go with it
                before = staged.copy()
                try:
                    staged.add(verdict, conversation)
                    lay = _overlay(conversation, staged)
                    spec = dict(verdict.get("draw") or {})
                    own_id = (current.nai_prompts or {}).get("draw_id") or ""
                    # the round's exposure and seed come from the picture it
                    # is looking at; the record says what the turn continues
                    rctx = _draw_context(
                        conversation, current, {**spec, "inherits": spec.get("inherits") or own_id}, lay, staged)
                    spec["inherits"] = _continues(spec, own_id, result.get("draw"))
                    rctx.same_drawing = True
                    rctx.seed = main_seed(current)
                    Message.objects.filter(id=message.id).update(selection_snapshot=library.snapshot(rctx.exposure))
                    prompts_out, painted = _prompts_and_paint(current, spec, rctx)
                except BaseException:
                    staged.restore(before)
                    Message.objects.filter(id=message.id).update(**held)
                    raise
                if not painted:
                    staged.restore(before)
                    Message.objects.filter(id=message.id).update(**held)
                return prompts_out, painted

            outcome = reflection.run(
                base_history=grok_messages,
                first_reply=result.get("reply") or "",
                first_answer=result,
                brief=result["draw"],
                nai_prompts=nai_prompts,
                images=images,
                rounds=rounds,
                hooks=reflection.Hooks(
                    redraw=redraw,
                    check_cancel=lambda: _check_cancel(message.id),
                    set_phase=lambda phase: _set_phase(message.id, phase),
                    enter_round=_phase_scope,
                    image_url=_image_data_url,
                    discard=_discard_images,
                    fatal=(PipelineCancelled,),
                    hopeless=dead_end,
                    check_answer=_exposure_check(conversation, staged),
                ),
            )
            nai_prompts = outcome.nai_prompts
            produced = len(outcome.images)
            kept_brief = outcome.brief
            if kept_brief:
                kept_brief = {**kept_brief, "inherits": _continues(
                    kept_brief, (nai_prompts or {}).get("draw_id") or "", result.get("draw"))}
            Message.objects.filter(id=message.id, status="pending").update(
                draw_brief=kept_brief,
                nai_prompts=outcome.nai_prompts,
                reflection_log=outcome.log or None,
            )
            log.info(
                "reflection ran %d round(s) for %s (redrew %d)",
                outcome.rounds_run,
                message.id,
                sum(1 for r in outcome.log if r.get("redrew")),
            )
    return produced


def run_generation(message_id) -> None:
    """
    Complete a pending assistant message (full pipeline: models -> novelai).
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
            selection = library.selection_for(conversation)
            sent_refs: dict = {}
            grok_messages = build_history(
                conversation,
                before_seq=message.seq,
                selection=selection,
                sticky_refs=message.sticky_refs,
                sent=sent_refs,
            )
            staged = library.Staged(seq=message.seq, in_project=conversation.folder is not None)
            result = _with_retries(
                "thinking",
                message.id,
                lambda: llm.lead_turn(
                    grok_messages,
                    on_retry=lambda _r: _set_phase(message.id, "retry:thinking"),
                    on_lookup=lambda n: _set_phase(message.id, f"lookup:{n}"),
                    check=_exposure_check(conversation, staged),
                ),
            )
            _check_cancel(message.id)
            prompt_tokens = (result.get("usage") or {}).get("prompt_tokens")
            Message.objects.filter(id=message.id, status="pending").update(
                content=result["reply"],
                model_raw={**result, "references_sent": sent_refs.get("references", [])},
                prompt_tokens=prompt_tokens,
                draw_brief=result.get("draw"),
                heartbeat_at=timezone.now(),
            )
            # What the lead wrote is this turn's: laid over the library for
            # everything it draws, and written into the library once the
            # turn's pictures are settled -- after reflection, or when the
            # turn is stopped or fails. A reflection round's writes join the
            # turn's only if its picture is kept.
            staged.add(result, conversation)
            try:
                produced = _draw_turn(conversation, message, result, staged, grok_messages)
            finally:
                _commit_library(conversation, message, staged)
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
            log.warning("continuous: closing the run, this will not get better")
            continuous.close_open_run(message.conversation, "failed")
    finally:
        Conversation.objects.filter(id=conversation.id).update(
            updated_at=timezone.now()
        )
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
