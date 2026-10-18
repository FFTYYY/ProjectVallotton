"""Conversation / message API."""

from __future__ import annotations

import io
import json
import os
import logging
import uuid

# hard dependency — a missing Pillow must fail at startup, not disguise
# itself as "invalid image" at upload time
from PIL import Image

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import Max, Min
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from core.http import (
    api_login_required,
    conversation_to_dict,
    folder_breadcrumbs,
    json_err,
    json_ok,
    message_to_dict,
)
from core.models import Attachment, Conversation, Folder, LibraryProposal, Message
from core.services import danbooru, continuous, gensettings, jobs, library, llm, pipeline
from core import ui_text

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- conversations

MAX_TITLE = 200


def _previews_for(convs) -> dict:
    """
    The last thing said in each of these conversations, in one query.

    One query per row was fine when the list was loaded by hand; the left rail
    now re-reads it every 45 seconds and on every return to the tab, so fifty
    rows meant fifty round trips on a clock. The breadcrumb helper beside this
    one already worked this way — this is the same shape.
    """
    ids = [c.id for c in convs]
    if not ids:
        return {}
    rows = (
        Message.objects.filter(conversation_id__in=ids, archived=False)
        # app notes are not part of the conversation's content
        .filter(role__in=("user", "assistant"))
        .exclude(status="pending")
        .order_by("conversation_id", "-seq")
        .values("conversation_id", "content", "kind")
    )
    out: dict = {}
    for row in rows:
        cid = row["conversation_id"]
        if cid in out:  # ordered newest-first within each conversation
            continue
        out[cid] = (row["content"] or (ui_text.IMAGE_PLACEHOLDER if row["kind"] == "image" else ""))[:80]
    return out


@api_login_required
@require_http_methods(["GET", "POST"])
def conversations(request):
    if request.method == "POST":
        try:
            body = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            body = {}
        title = str(body.get("title") or ui_text.NEW_CONVERSATION_TITLE)[:200]
        folder: Folder | None = None
        if body.get("folder_id"):
            try:
                folder = Folder.objects.filter(
                    id=uuid.UUID(str(body["folder_id"]))
                ).first()
            except ValueError:
                return json_err(ui_text.BAD_FOLDER_ID)
            if not folder:
                return json_err(ui_text.NO_FOLDER, status=404)
        # a new conversation sits right after the last folder, i.e. at the top
        # of its level's conversations
        top = Conversation.objects.filter(folder=folder).aggregate(
            m=Min("position")
        )["m"]
        # A new conversation starts from the defaults set on the settings page,
        # and owns them from here on — changing the defaults later does not
        # reach back into conversations that already exist.
        #
        # The browser may send a set of its own on top: the new-chat page has
        # the same four controls, and what it was showing is what the first
        # turn is sent with. Without this the row said one thing and that turn
        # did another, and the next turn in the same conversation quietly went
        # back to the defaults.
        start = gensettings.defaults()
        asked = body.get("gen_settings")
        if isinstance(asked, dict):
            start = gensettings.clean({**start, **asked})
        conv = Conversation.objects.create(
            title=title, source="web", folder=folder, position=(top or 0) - 1,
            gen_settings=start,
        )
        # A conversation starts with nothing from the library switched on,
        # inside a project or not: entries come on one by one as the lead
        # names them (library.switch_on) or as the user ticks them. It used
        # to start with every setting of the project on, and the user asked
        # for that to go (2026-10-17).
        return json_ok(conversation_to_dict(conv), status=201)

    scope = request.GET.get("scope", "active")  # active | archived | all
    try:
        limit = min(max(int(request.GET.get("limit", "50")), 1), 200)
        offset = max(int(request.GET.get("offset", "0")), 0)
    except ValueError:
        return json_err(ui_text.BAD_PAGING)

    qs = Conversation.objects.all()
    if scope == "archived":
        qs = qs.filter(is_archived=True)
    elif scope != "all":
        qs = qs.filter(is_archived=False)

    total = qs.count()
    page = list(qs[offset : offset + limit])
    crumbs = folder_breadcrumbs()
    previews = _previews_for(page)
    items = [
        conversation_to_dict(c, preview=previews.get(c.id, ""), crumbs=crumbs)
        for c in page
    ]
    return json_ok(
        {"conversations": items, "total": total, "has_more": offset + limit < total}
    )


@api_login_required
@require_http_methods(["PATCH"])
def conversation_detail(request, conv_id: uuid.UUID):
    """
    Rename a conversation, or change its generation settings.

    No DELETE. Conversations are archived and never deleted — that is the rule
    the whole app is built on, and an endpoint that quietly contradicted it,
    with no confirmation and nothing written down, made the rule a sentence
    rather than a fact. Archiving is at /archive, and it is reversible.
    """
    conv = Conversation.objects.filter(id=conv_id).first()
    if not conv:
        return json_err(ui_text.NO_CONVERSATION, status=404)

    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return json_err(ui_text.BAD_JSON)

    fields = []
    if "title" in body:
        title = str(body.get("title") or "").strip()
        if not title:
            return json_err(ui_text.TITLE_EMPTY)
        conv.title = title[:MAX_TITLE]
        fields.append("title")
    # the right rail sends one control at a time; merge rather than replace
    if "gen_settings" in body:
        patch = body.get("gen_settings")
        if not isinstance(patch, dict):
            return json_err(ui_text.GEN_SETTINGS_NOT_OBJECT)
        gensettings.merge(conv, patch)
        fields.append("gen_settings")
    if not fields:
        return json_err(ui_text.NOTHING_TO_UPDATE_CONVERSATION)
    # gen_settings is already stored by merge(); saving it again would write
    # the same value, but updated_at still has to move
    conv.save(update_fields=[f for f in fields if f != "gen_settings"] + ["updated_at"])
    return json_ok(conversation_to_dict(conv))


@api_login_required
@require_POST
def archive_conversation(request, conv_id: uuid.UUID):
    """Hide a conversation from the normal list. It is never deleted."""
    conv = Conversation.objects.filter(id=conv_id).first()
    if not conv:
        return json_err(ui_text.NO_CONVERSATION, status=404)
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return json_err(ui_text.BAD_JSON)
    conv.is_archived = bool(body.get("archived", True))
    conv.save(update_fields=["is_archived", "updated_at"])
    return json_ok(conversation_to_dict(conv))


# ---------------------------------------------------------------- messages


def _validate_upload(f) -> tuple[str, int, int] | str:
    """Return (mime, width, height) for a valid image, or an error string."""
    data = f.read()
    f.seek(0)
    if len(data) > settings.MAX_UPLOAD_IMAGE_BYTES:
        return ui_text.IMAGE_TOO_BIG.format(mb=settings.MAX_UPLOAD_IMAGE_BYTES // (1024 * 1024))
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        fmt = (img.format or "").lower()
        img2 = Image.open(io.BytesIO(data))
        width, height = img2.size
    except Exception as exc:  # noqa: BLE001
        log.warning("upload rejected (%s): %s", getattr(f, "name", "?"), exc)
        return ui_text.NOT_AN_IMAGE
    if fmt not in ("png", "jpeg", "webp", "gif", "bmp"):
        return ui_text.UNSUPPORTED_IMAGE_FORMAT.format(fmt=fmt or "?")
    mime = f"image/{'jpeg' if fmt == 'jpeg' else fmt}"
    return mime, width, height


def _next_seq(conv: Conversation) -> int:
    agg = conv.messages.aggregate(m=Max("seq"))
    return (agg["m"] or 0) + 1


def _parse_n_images(raw) -> int | None:
    """Clamp a user-forced image count to 1..4; None if unset/invalid."""
    if raw in (None, "", "0"):
        return None
    try:
        return min(max(int(raw), 1), 4)
    except (TypeError, ValueError):
        return None


def _parse_flag(raw) -> bool | None:
    """
    A checkbox as it arrives over the wire. None means "the caller said
    nothing", which is different from "the caller said no" — a rerun has to
    tell those apart to know whether to fall back.
    """
    if raw in (None, ""):
        return None
    return str(raw).lower() in ("1", "true", "yes", "on")


def _parse_continuous_rounds(raw) -> int | None:
    """
    Clamp the continuous slider to 1..MAX_CONTINUOUS_ROUNDS.

    Its own function because it has its own ceiling. Both sliders went through
    the reflection parser, which clamps at MAX_REFLECTION_ROUNDS — so asking
    for more than ten pictures quietly got you ten, and the slider went to
    twenty saying otherwise.
    """
    if raw in (None, ""):
        return None
    try:
        return min(max(int(raw), 1), settings.MAX_CONTINUOUS_ROUNDS)
    except (TypeError, ValueError):
        return None


def _parse_auto_sent(raw) -> dict | None:
    """
    The "the app pressed send, not the user" marker, as {"round": n, "of": m}.

    Validated rather than trusted: it is written straight onto a row that the
    transcript reads back, and a malformed one would show up as a note nobody
    can explain. Anything that is not two sane numbers is treated as absent —
    a wrong note is worse than no note.
    """
    if raw in (None, ""):
        return None
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        round_no = int(parsed["round"])
        of = int(parsed["of"])
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None
    if not 1 <= round_no <= of <= settings.MAX_CONTINUOUS_ROUNDS:
        return None
    return {"round": round_no, "of": of}


def _parse_reflection_rounds(raw) -> int | None:
    """
    Clamp the reflection slider to 0..MAX_REFLECTION_ROUNDS.

    0 is a real answer here, not "unset": it is what the slider says when the
    feature is off, and storing it says the turn was sent with reflection off
    rather than sent before the feature existed.
    """
    if raw in (None, ""):
        return None
    try:
        return min(max(int(raw), 0), settings.MAX_REFLECTION_ROUNDS)
    except (TypeError, ValueError):
        return None


@api_login_required
@require_http_methods(["GET", "POST"])
def messages(request, conv_id: uuid.UUID):
    conv = Conversation.objects.filter(id=conv_id).first()
    if not conv:
        return json_err(ui_text.NO_CONVERSATION, status=404)

    if request.method == "GET":
        # No reaping here. This runs on a 1.3-second poll, and hanging a
        # write-capable sweep off it was only ever a way to surface a dead job
        # sooner. A restart — by far the common case — is now concluded at
        # start-up by jobs.adopt_orphans(), instantly and without a clock; what
        # is left waits for the sweeper, which is the right place for it.
        try:
            after_seq = int(request.GET.get("after_seq", "0"))
            before_seq = int(request.GET.get("before_seq", "0"))
            limit = min(max(int(request.GET.get("limit", "60")), 1), 200)
        except ValueError:
            return json_err(ui_text.BAD_SEQ_PAGING)

        base = conv.messages.filter(archived=False).prefetch_related("attachments")
        has_more_before = False
        if after_seq:
            # Incremental poll: everything newer — archived rows included, and
            # only here. This is the one caller that already holds the older
            # rows, so it is the one caller that needs to be told when one goes
            # away. The paging branches below are building a view from nothing
            # and have no use for rows that are not in it.
            window = list(
                conv.messages.filter(seq__gt=after_seq)
                .prefetch_related("attachments")
                .order_by("seq")
            )
        elif before_seq:
            # older-history page, newest-first then reversed
            window = list(reversed(base.filter(seq__lt=before_seq).order_by("-seq")[:limit]))
            if window:
                has_more_before = base.filter(seq__lt=window[0].seq).exists()
        else:
            # initial load: the last `limit` messages
            window = list(reversed(base.order_by("-seq")[:limit]))
            if window:
                has_more_before = base.filter(seq__lt=window[0].seq).exists()

        # A turn being made is shown whatever else is true of it. has_pending
        # ignores `archived` — it is a question about work, not about
        # visibility — and a window that did not would leave the composer
        # locked with nothing on screen to stop.
        seen = {m.id for m in window}
        window += [
            m
            for m in conv.messages.filter(status="pending")
            .prefetch_related("attachments")
            .order_by("seq")
            if m.id not in seen
        ]

        # "is a turn running" is not a question about visibility — see the
        # same reasoning in continuous.reconcile. Archiving a running turn
        # must not make the app believe nothing is being made.
        has_pending = conv.messages.filter(status="pending").exists()
        return json_ok(
            {
                "conversation": conversation_to_dict(conv),
                "messages": [message_to_dict(m) for m in window],
                "has_pending": has_pending,
                "has_more_before": has_more_before,
                # How many rows are live, so a browser can tell that one it is
                # showing has gone.
                #
                # An incremental poll is only told about rows newer than the
                # waterline it asks from, and a finished turn sits below it —
                # 重跑 archives exactly those. Rather than teach every path
                # that archives something to announce itself (and rely on the
                # browser hearing it), the answer carries the count. Holding
                # more than exist is impossible unless something went, and
                # then the window is simply read again.
                "active_count": conv.messages.filter(archived=False).count(),
            }
        )

    # ---- POST: send a user message and start generation
    text = str(request.POST.get("text") or "").strip()
    client_msg_id = str(request.POST.get("client_msg_id") or "")[:64]
    files = request.FILES.getlist("images")
    # Anything the caller leaves out falls back to what this conversation is
    # set to — the settings live here now, not in whichever browser is open.
    stored = gensettings.of(conv)
    force_n = _parse_n_images(request.POST.get("n_images")) or stored["n_images"]
    force_reflection = _parse_reflection_rounds(request.POST.get("reflection_rounds"))
    if force_reflection is None:
        force_reflection = stored["reflection_rounds"]
    auto_sent = _parse_auto_sent(request.POST.get("auto_sent"))
    sticky_refs = _parse_flag(request.POST.get("sticky_refs"))
    if sticky_refs is None:
        sticky_refs = stored["sticky_refs"]
    continuous_rounds = (
        _parse_continuous_rounds(request.POST.get("continuous_rounds"))
        or stored["continuous_rounds"]
    )

    if not text and not files:
        return json_err(ui_text.MESSAGE_EMPTY)
    if len(files) > settings.MAX_UPLOAD_IMAGES_PER_MESSAGE:
        return json_err(ui_text.TOO_MANY_IMAGES_UPLOAD.format(max=settings.MAX_UPLOAD_IMAGES_PER_MESSAGE))

    # idempotent retry: same client_msg_id → return the messages already created
    if client_msg_id:
        existing = conv.messages.filter(client_msg_id=client_msg_id).first()
        if existing:
            pair = list(
                conv.messages.filter(seq__gte=existing.seq, seq__lte=existing.seq + 1)
                .prefetch_related("attachments")
                .order_by("seq")
            )
            return json_ok({"messages": [message_to_dict(m) for m in pair]})

    validated = []
    for f in files:
        info = _validate_upload(f)
        if isinstance(info, str):
            return json_err(ui_text.FILE_REJECTED.format(name=f.name, why=info))
        validated.append((f, *info))

    try:
        with transaction.atomic():
            locked_conv = Conversation.objects.select_for_update().get(id=conv.id)
            if locked_conv.messages.filter(status="pending").exists():
                return json_err(
                    ui_text.BUSY_REPLYING, status=409, code="busy"
                )
            # before the rows, not after: the opening note belongs above the
            # message that starts the run, and closing any previous run here
            # keeps that note above this message too rather than under it
            continuous.arm(
                locked_conv,
                continuous_rounds,
                n_images=force_n,
                reflection_rounds=force_reflection,
                sticky_refs=sticky_refs,
            )
            seq = _next_seq(locked_conv)
            user_msg = Message.objects.create(
                conversation=locked_conv,
                seq=seq,
                role="user",
                status="done",
                content=text,
                client_msg_id=client_msg_id,
                auto_sent=auto_sent,
            )
            for idx, (f, mime, width, height) in enumerate(validated):
                att = Attachment(
                    message=user_msg,
                    kind="upload",
                    mime=mime,
                    width=width,
                    height=height,
                    size_bytes=f.size,
                    position=idx,
                )
                safe_name = f"{uuid.uuid4().hex}-{(f.name or 'image')[-80:]}"
                att.file.save(safe_name, f, save=True)
            assistant_msg = Message.objects.create(
                conversation=locked_conv,
                seq=seq + 1,
                role="assistant",
                status="pending",
                force_n_images=force_n,
                force_reflection_rounds=force_reflection,
                sticky_refs=sticky_refs,
                heartbeat_at=timezone.now(),
            )
            locked_conv.save(update_fields=["updated_at"])
    except IntegrityError:
        # lost a race on client_msg_id — fall back to the idempotent path.
        # The guard matters: the unique index skips blank ids, so an empty
        # client_msg_id would match every id-less row and hand back the
        # conversation's FIRST message as if it were this send.
        existing = (
            conv.messages.filter(client_msg_id=client_msg_id).first()
            if client_msg_id
            else None
        )
        if existing:
            pair = list(
                conv.messages.filter(seq__gte=existing.seq, seq__lte=existing.seq + 1)
                .prefetch_related("attachments")
                .order_by("seq")
            )
            return json_ok({"messages": [message_to_dict(m) for m in pair]})
        raise

    jobs.start_generation(assistant_msg.id)
    user_msg.refresh_from_db()
    return json_ok(
        {"messages": [message_to_dict(user_msg), message_to_dict(assistant_msg)]},
        status=201,
    )


@api_login_required
@require_POST
def cancel_message(request, msg_id: uuid.UUID):
    msg = Message.objects.filter(id=msg_id, role="assistant").first()
    if not msg:
        return json_err(ui_text.NO_MESSAGE, status=404)
    if msg.status != "pending":
        return json_ok({"ok": True, "status": msg.status})
    # Mark cancelled IMMEDIATELY so the user is unblocked at once. The worker
    # thread may still be inside a blocking model/novelai call, but every write
    # it makes is guarded by status="pending", so once we flip the row here its
    # eventual result is discarded (no-op updates, no orphan attachments).
    Message.objects.filter(id=msg.id, status="pending").update(
        status="cancelled", cancel_requested=True, phase=""
    )
    # stop means stop: without this the run would send the next instruction a
    # second after the user said no
    continuous.stop(msg.conversation_id)
    return json_ok({"ok": True, "status": "cancelled"})


@api_login_required
@require_GET
def message_raw(request, msg_id: uuid.UUID):
    """
    Everything the two models actually said for this turn.

    Kept off message_to_dict on purpose: model_raw carries the whole lead
    result, and every poll returns every message. Nobody needs it until
    somebody asks to look.
    """
    msg = Message.objects.filter(id=msg_id, role="assistant").first()
    if not msg:
        return json_err(ui_text.NO_MESSAGE, status=404)

    raw = msg.model_raw if isinstance(msg.model_raw, dict) else {}

    # The references this turn was given. They live on the user message that
    # prompted it, not on this row. `description` is only ever set on turns from
    # when a separate vision model stood in front of the picture; these days the
    # lead model is handed the picture itself and there is nothing to quote.
    prompt_msg = (
        msg.conversation.messages.filter(archived=False, role="user", seq__lt=msg.seq)
        .order_by("-seq")
        .prefetch_related("attachments")
        .first()
    )
    references = []
    if prompt_msg:
        uploads = [a for a in prompt_msg.attachments.all() if a.kind == "upload"]
        for i, att in enumerate(uploads, start=1):
            references.append(
                {
                    "index": i,
                    "size": [att.width, att.height],
                    "url": f"/api/media/{att.id}/{os.path.basename(att.file.name or '')}",
                    "thumb_url": f"/api/media/{att.id}/thumb",
                    "description": att.description or None,
                }
            )

    references_sent = None
    if isinstance(raw.get("references_sent"), list):
        references_sent = [
            {
                "url": f"/api/media/{att.id}/{os.path.basename(att.file.name or '')}",
                "thumb_url": f"/api/media/{att.id}/thumb",
                "description": att.description or None,
            }
            for att in Attachment.objects.filter(id__in=raw["references_sent"]).order_by("position")
        ]
    return json_ok(
        {
            "lead": {
                "reply": raw.get("reply"),
                "draw": raw.get("draw"),
                # the entries it wrote into this conversation's library
                "settings": raw.get("settings") or [],
                "characters": raw.get("characters") or [],
                "edits": raw.get("edits"),
                "usage": raw.get("usage"),
                # the answer before the app tidied it
                "raw": raw.get("raw_answer"),
                # what the app sent back about an answer that did not hold
                # together, and the answer it sent back
                "checks": raw.get("checks") or [],
            },
            # the brief behind the pictures that were kept: after reflection
            # this is the last round's, not the lead's first
            "draw_brief": msg.draw_brief,
            # what the paint and the proposal had to say about this turn
            "paint_errors": raw.get("paint_errors") or [],
            "edit_notes": raw.get("edit_notes") or [],
            # each model's own working, when it hands any back. This is the
            # honest answer to "why these tags" — the alternative is guessing
            # from the output.
            "reasoning": {
                "lead": raw.get("reasoning") or "",
                "draw": raw.get("draw_reasoning") or "",
            },
            # the drawing model's side of the turn, for the same reason
            "drawing": msg.nai_prompts,
            # Tags in those prompts that the site does not have. A tag nobody
            # trained on means nothing to the model, so a word in here is the
            # likeliest reason a picture missed something that was asked for.
            "unknown_tags": llm.unknown_tags(msg.nai_prompts, (msg.prompt or {}).get("assembly")
                                             if isinstance(msg.prompt, dict) else None),
            # an empty index reports nothing; said so, or a clean list reads as a clean prompt
            "tag_index_missing": not len(danbooru.index()),
            # and what each model looked up before answering, one entry per
            # lookup. Same question as the working above, answered from the
            # other side: not "why these tags" but "what did it actually check".
            "lookups": {"lead": raw.get("lookups") or [], "draw": raw.get("draw_lookups") or []},
            # the compressor's pass, when the prompt was over NovelAI's limit:
            # the prompt it was handed, the counts before and after, its
            # working; null on every other turn
            "compress": raw.get("compress"),
            # how the prompt was made: the lead's description, the exposed
            # entries as they read, the drawing model's answer and what each
            # entry came to. This is where "why is she not wearing the ring"
            # is answered.
            "picture": msg.prompt,
            "notes": raw.get("draw_notes") or [],
            # an answer that could not be read, kept whole. The transcript can
            # only afford a line of it, and a line is not enough to say which
            # character broke it.
            "unparsed": raw.get("unparsed") or "",
            # what the lead model was shown, not a transcript of it
            # the uploads the lead was actually given this turn, when the row
            # recorded them; the latest message's uploads are a guess
            "references": references_sent if references_sent is not None else references,
            "selection_snapshot": msg.selection_snapshot,
            # what the turn said to itself between the picture it first made
            # and the one it kept. The transcript carries only the tally; the
            # rounds themselves are long, and this is where long things go.
            "reflection": msg.reflection_log,
            "prompt_tokens": msg.prompt_tokens,
            "created_at": msg.created_at.isoformat(),
        }
    )


@api_login_required
@require_POST
def redraw_message(request, msg_id: uuid.UUID):
    """Re-run NovelAI with the same prompts (new seeds) as a new message."""
    src = Message.objects.filter(id=msg_id, role="assistant").first()
    if not src:
        return json_err(ui_text.NO_MESSAGE, status=404)
    # Not "does it have prompts" but "can those prompts still paint": a row
    # can predate the shape the app expects, and an empty base_prompt sent to
    # NovelAI is a picture of nothing, billed for.
    if not llm.can_paint(src.nai_prompts):
        return json_err(ui_text.NOTHING_TO_REDRAW)

    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        body = {}
    prompts = dict(src.nai_prompts or {})
    n_images = body.get("n_images")
    if isinstance(n_images, int) and 1 <= n_images <= settings.MAX_IMAGES_PER_REQUEST:
        prompts["n_images"] = n_images

    with transaction.atomic():
        conv = Conversation.objects.select_for_update().get(id=src.conversation_id)
        if conv.messages.filter(status="pending").exists():
            return json_err(ui_text.BUSY, status=409, code="busy")
        # Into the turn it came from, not a new one.
        #
        # A redraw is another go at a picture that already exists, so it joins
        # that picture — the same thing as having asked for one more image on
        # that turn. A new message said otherwise to everything downstream: the
        # lead model reads the newest picture turn back as "what you just
        # made", so redrawing something from half an hour ago put an old
        # picture at the end of the conversation wearing that label. It also
        # left a wordless bubble in the transcript, and a row that had to keep
        # explaining that it belonged to no continuous run.
        Message.objects.filter(id=src.id).update(
            status="pending",
            phase="",
            error="",
            kind="image",
            nai_prompts=prompts,
            # the count is on the row as well as inside the prompts, so the row
            # says what it is for rather than depending on a nested key
            force_n_images=_parse_n_images(prompts.get("n_images")),
            # already true of a finished turn; said again because this row is
            # about to look like work in progress, and a run must not adopt it
            run_settled=True,
            cancel_requested=False,
            heartbeat_at=timezone.now(),
        )
        conv.save(update_fields=["updated_at"])

    jobs.start_redraw(src.id)
    src.refresh_from_db()
    return json_ok({"messages": [message_to_dict(src)]}, status=201)


@api_login_required
@require_POST
def set_main_image(request, msg_id: uuid.UUID):
    """
    Make one of a turn's pictures its main one.

    Everything that refers to "the picture of this turn" — an edit next turn,
    the seed the next picture follows — means the main one, so this is the
    one place a user says which picture they are talking about. The transcript
    shows it first; the history tells the model which it is.
    """
    msg = Message.objects.filter(id=msg_id, role="assistant").first()
    if not msg:
        return json_err(ui_text.NO_MESSAGE, status=404)
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return json_err(ui_text.BAD_JSON)
    # by the picture's position in the turn's album, which is what the row
    # stores and what survives a reflection round discarding earlier ones
    index = body.get("index")
    if not isinstance(index, int) or not msg.attachments.filter(
        kind="generated", position=index
    ).exists():
        return json_err(ui_text.NO_SUCH_PICTURE)
    if index != msg.main_index:
        Message.objects.filter(id=msg.id).update(main_index=index)
        msg.refresh_from_db()
    return json_ok({"messages": [message_to_dict(msg)]})


@api_login_required
@require_POST
def rerun_conversation(request, conv_id: uuid.UUID):
    """
    Regenerate the response to the latest user message: archive every active
    assistant message after it, then run the full pipeline again.
    """
    conv = Conversation.objects.filter(id=conv_id).first()
    if not conv:
        return json_err(ui_text.NO_CONVERSATION, status=404)

    # How many to paint belongs to the caller, and a rerun is a caller. Without
    # this the row carried no count at all and _paint fell back to the one in
    # nai_prompts — which for a fresh turn is 1, so every rerun quietly painted
    # a single image no matter what 出图数量 said.
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        body = {}
    force_n = _parse_n_images(body.get("n_images"))
    force_reflection = _parse_reflection_rounds(body.get("reflection_rounds"))
    # A 重跑 is the user asking again, under whatever the sliders say now:
    # what the caller sends, then what this conversation is set to. The turn
    # being replaced is not consulted -- not for the count, not for the
    # rounds. (It used to be the fallback; it no longer is.)
    stored = gensettings.of(conv)
    if force_n is None:
        force_n = stored["n_images"]
    if force_reflection is None:
        force_reflection = stored["reflection_rounds"]
    sticky_refs = _parse_flag(body.get("sticky_refs"))
    if sticky_refs is None:
        sticky_refs = stored["sticky_refs"]
    continuous_rounds = (
        _parse_continuous_rounds(body.get("continuous_rounds"))
        or stored["continuous_rounds"]
    )

    with transaction.atomic():
        locked_conv = Conversation.objects.select_for_update().get(id=conv.id)
        if locked_conv.messages.filter(status="pending").exists():
            return json_err(ui_text.BUSY, status=409, code="busy")
        last_user = (
            locked_conv.messages.filter(archived=False, role="user")
            .order_by("-seq")
            .first()
        )
        if not last_user:
            return json_err(ui_text.NO_USER_MESSAGE_YET)
        # What the turns being replaced wrote into this conversation's own
        # entries goes with them, latest first; what the user changed by hand
        # since stays, and the new turn reads the library as it is now.
        replaced = locked_conv.messages.filter(
            archived=False, role="assistant", seq__gt=last_user.seq).order_by("-seq")
        for turn in replaced:
            if isinstance(turn.library_before, dict) and isinstance(turn.library_snapshot, dict):
                taken = library.take_back(locked_conv, turn.library_before, turn.library_snapshot)
                if taken:
                    log.info("rerun took back turn %s's entries: %s", turn.id, "、".join(taken))
        # Archive the assistant turn AND any app notes that followed it, so a
        # rerun starts from a clean tail — except the notes that open and close
        # a continuous run. Those record what the app did, not the answer being
        # replaced, and a run does not un-happen because one of its turns is
        # being asked again. Rerunning the last turn of a finished run used to
        # swallow its 「连续生成完成」, leaving the opening note up there with
        # nothing ever answering it.
        locked_conv.messages.filter(
            archived=False, role__in=("assistant", "system"), seq__gt=last_user.seq
        ).exclude(notice_kind__startswith="continuous").update(archived=True)
        # 重跑 is the user asking again, so it starts a fresh run too — and
        # ends the old one first, with the note that says so
        continuous.arm(
            locked_conv,
            continuous_rounds,
            n_images=force_n,
            reflection_rounds=force_reflection,
            sticky_refs=sticky_refs,
        )
        new_msg = Message.objects.create(
            conversation=locked_conv,
            seq=_next_seq(locked_conv),
            role="assistant",
            status="pending",
            force_n_images=force_n,
            force_reflection_rounds=force_reflection,
            sticky_refs=sticky_refs,
            heartbeat_at=timezone.now(),
        )
        locked_conv.save(update_fields=["updated_at"])

    jobs.start_generation(new_msg.id)
    return json_ok({"messages": [message_to_dict(new_msg)]}, status=201)


def _clone_attachment(att: Attachment, new_message: Message) -> None:
    """
    A second row for the same picture. The file is not copied.

    It used to be: every branch read every picture and wrote it out again under
    a new name. A twenty-turn conversation branched once left eighty more
    files on disk, byte for byte identical to eighty that were already there.

    Sharing is safe because of what a picture is here. It is written once and
    never edited — the bytes that came back from NovelAI — and the only code in
    this app that deletes an image file is pipeline._discard_images, which
    drops the pictures a reflection round replaced, while that turn is still
    pending. A branch copies finished turns, so it can never hold one of those.

    THE CONSTRAINT, said here because this is what depends on it: if anything
    ever deletes an attachment's file for a reason other than that one, these
    rows need reference counting first. Deleting a conversation would be such
    a reason — which is part of why conversations are archived and never
    deleted.
    """
    Attachment.objects.create(
        message=new_message,
        kind=att.kind,
        mime=att.mime,
        width=att.width,
        height=att.height,
        size_bytes=att.size_bytes,
        seed=att.seed,
        position=att.position,
        description=att.description,
        # the same stored file: a FileField holds a path, and two rows may
        # hold the same one
        file=att.file.name,
    )


@api_login_required
@require_POST
def branch_conversation(request, msg_id: uuid.UUID):
    """
    Deep-copy the conversation up to and including message `msg_id` into a
    brand-new, fully independent conversation (messages, attachment files,
    prompts). It lands in the SAME folder and inherits the same library
    selection, so the branch can carry on where the original left off.
    Continuing the branch never touches the original.
    """
    src_msg = Message.objects.select_related("conversation").filter(id=msg_id).first()
    if not src_msg:
        return json_err(ui_text.NO_MESSAGE, status=404)
    src_conv = src_msg.conversation

    # the same rows the model is given — one definition, see pipeline.real_turns
    src_messages = list(
        pipeline.real_turns(src_conv)
        .filter(seq__lte=src_msg.seq)
        .prefetch_related("attachments")
        .order_by("seq")
    )
    if not src_messages:
        return json_err(ui_text.NOTHING_TO_BRANCH)

    with transaction.atomic():
        new_conv = Conversation.objects.create(
            title=ui_text.BRANCH_TITLE.format(title=src_conv.title or ui_text.CONVERSATION_WORD)[:MAX_TITLE],
            source="web",
            folder=src_conv.folder,
            position=src_conv.position,
            # a branch carries on the same piece of work, so it carries on
            # under the same settings rather than resetting to the defaults
            gen_settings=gensettings.of(src_conv),
        )
        # The conversation layer as it stood at this message, rebuilt in the
        # branch with its ids kept, so the pictures copied below still point
        # at their lines. The project layer is shared, so of it only the
        # selection is carried. A history from before the layer was recorded
        # per turn falls back to the source's entries as they are now.
        anchor = next(
            (m for m in reversed(src_messages)
             if m.role == "assistant" and isinstance(m.library_snapshot, dict)),
            None,
        )
        # The layer a branch at this message starts from is the layer as it
        # stood just before the NEXT message was sent: this turn's own edits,
        # and whatever the user changed by hand before carrying on. The next
        # turn recorded exactly that at its start (library_before). With no
        # next message that moment is now. A next turn from before that was
        # recorded falls back to this turn's snapshot (its edits, without the
        # hand edits made after it).
        following = (pipeline.real_turns(src_conv).filter(role="assistant", seq__gt=src_msg.seq)
                     .order_by("seq").first())
        from_end = following is None
        if from_end:
            own = library.own_layer(src_conv)
        elif isinstance(following.library_before, dict):
            own = following.library_before
        elif anchor is not None and isinstance(anchor.library_snapshot, dict):
            own = anchor.library_snapshot
        else:
            own = library.own_layer(src_conv)
        own_settings, own_variants = library.rebuild_own_layer(own, new_conv)
        # Of the project's entries, the ones the source has switched on now.
        # A message's selection_snapshot is no record of that: it is the
        # receipt of what its picture was drawn from, and reading it as the
        # selection switched off every project entry the picture did not use.
        project_settings = list(src_conv.selected_settings.filter(conversation__isnull=True))
        project_variants = list(src_conv.selected_variants.filter(character__conversation__isnull=True))
        new_conv.selected_settings.set([*project_settings, *own_settings])
        new_conv.selected_variants.set([*project_variants, *own_variants])
        new_seq = 0
        copied: dict = {}
        for m in src_messages:
            new_seq += 1
            copy = Message.objects.create(
                conversation=new_conv,
                seq=new_seq,
                role=m.role,
                # a failed turn that kept its pictures, a stopped one: the
                # branch shows them as the original does
                status=m.status if m.status in ("done", "cancelled", "error") else "done",
                error=m.error,
                kind=m.kind,
                content=m.content,
                model_raw=m.model_raw,
                nai_prompts=m.nai_prompts,
                # the lead model reads its own picture descriptions back out of
                # the history; a branch that drops them starts amnesiac about
                # every scene it is supposed to be continuing
                draw_brief=m.draw_brief,
                # the slot table nai_prompts was assembled from: what the next
                # edit in the branch changes. Without it the first edit in a
                # branch found no picture to edit and drew a new one from the
                # change text alone -- a cloud, and nothing else.
                prompt=m.prompt,
                # which of the turn's pictures the user chose: the seed and
                # the composition an edit in the branch follows
                main_index=m.main_index,
                selection_snapshot=m.selection_snapshot,
                library_snapshot=m.library_snapshot,
                library_before=m.library_before,
                prompt_tokens=m.prompt_tokens,
                # how this picture came to be this picture, carried with it
                reflection_log=m.reflection_log,
                auto_sent=m.auto_sent,
                # A copy belongs to no run: it is history the branch inherited,
                # not work anything is waiting on. Left at the column's default
                # a continuous run started in the branch counted it as one of
                # its own rounds. arm() settles whatever it finds for the same
                # reason — this says it at the other end too, so a copy is
                # never briefly countable.
                run_settled=True,
            )
            for att in m.attachments.all():
                _clone_attachment(att, copy)
            copied[m.id] = copy
        # The cards too: the lead reads its own edits back out of the history
        # (model_raw.edits) and would believe it had proposed a project edit
        # there was no card to approve.
        for p in LibraryProposal.objects.filter(message_id__in=list(copied)).order_by("created_at"):
            LibraryProposal.objects.create(
                conversation=new_conv, message=copied[p.message_id], folder=p.folder,
                payload=p.payload, status=p.status, applied_summary=p.applied_summary,
                resolved_at=p.resolved_at,
            )

    return json_ok(conversation_to_dict(new_conv), status=201)
