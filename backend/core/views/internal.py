"""
Internal API used by the MCP server.

Auth: ``Authorization: Bearer <MCP_TOKEN>`` (the same token the MCP server
requires from its own clients). These endpoints are never exposed by nginx —
only the MCP process reaches them over the docker network / localhost.

All requests here run the pipeline SYNCHRONOUSLY (MCP is a blocking
request/response protocol) and images travel as base64 file objects.
"""

from __future__ import annotations

import base64
import functools
import io
import json
import logging
import uuid

from PIL import Image

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from core.http import constant_time_eq, json_err, json_ok
from core.models import Attachment, Conversation, Message
from core.services import gensettings, pipeline
from core.services import quota as quota_service
from core import ui_text

log = logging.getLogger(__name__)


def mcp_token_required(view):
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not settings.MCP_TOKEN:
            return json_err(ui_text.MCP_NOT_CONFIGURED, status=503)
        if not header.startswith("Bearer "):
            return json_err("missing bearer token", status=401)
        token = header[len("Bearer "):].strip()
        if not constant_time_eq(token, settings.MCP_TOKEN):
            return json_err("invalid bearer token", status=401)
        return view(request, *args, **kwargs)

    return wrapper


def _file_obj(filename: str, data: bytes, content_type: str) -> dict:
    return {
        "filename": filename,
        "content_type": content_type,
        "data_base64": base64.b64encode(data).decode("ascii"),
    }


def _attachment_file_obj(att: Attachment) -> dict | None:
    try:
        with att.file.open("rb") as fh:
            data = fh.read()
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read attachment %s: %s", att.id, exc)
        return None
    name = att.file.name.rsplit("/", 1)[-1]
    return _file_obj(name, data, att.mime or "image/png")


def _next_seq(conv: Conversation) -> int:
    agg = conv.messages.aggregate(m=Max("seq"))
    return (agg["m"] or 0) + 1


def _decode_wire_image(obj: dict, index: int) -> tuple[bytes, str, str, int, int]:
    """Validate one wire file object → (data, filename, mime, width, height)."""
    if not isinstance(obj, dict) or not obj.get("data_base64"):
        raise ValueError(ui_text.IMAGE_MISSING_DATA.format(index=index))
    try:
        data = base64.b64decode(obj["data_base64"], validate=True)
    except Exception as exc:
        raise ValueError(ui_text.IMAGE_DECODE_FAILED.format(index=index, error=exc)) from exc
    if len(data) > settings.MAX_UPLOAD_IMAGE_BYTES:
        raise ValueError(ui_text.IMAGE_OVER_LIMIT.format(index=index))
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        img2 = Image.open(io.BytesIO(data))
        width, height = img2.size
        fmt = (img2.format or "png").lower()
    except Exception as exc:
        raise ValueError(ui_text.IMAGE_INVALID.format(index=index, error=exc)) from exc
    mime = f"image/{'jpeg' if fmt == 'jpeg' else fmt}"
    filename = str(obj.get("filename") or f"ref-{index}.{fmt}")[-100:]
    return data, filename, mime, width, height


@csrf_exempt
@mcp_token_required
@require_POST
def chat(request):
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return json_err(ui_text.BAD_JSON_BODY)

    text = str(body.get("message") or "").strip()
    images = body.get("images") or []
    if not text and not images:
        return json_err(ui_text.MESSAGE_OR_IMAGES_REQUIRED)
    if len(images) > settings.MAX_UPLOAD_IMAGES_PER_MESSAGE:
        return json_err(ui_text.TOO_MANY_IMAGES.format(max=settings.MAX_UPLOAD_IMAGES_PER_MESSAGE))

    # how many to paint is the caller's call, never the model's — the same rule
    # the web UI's 出图数量 follows
    try:
        n_images = min(max(int(body.get("n_images") or 1), 1), settings.MAX_IMAGES_PER_REQUEST)
    except (TypeError, ValueError):
        n_images = 1

    decoded = []
    try:
        for i, obj in enumerate(images):
            decoded.append(_decode_wire_image(obj, i))
    except ValueError as exc:
        return json_err(str(exc))

    conv = None
    conv_id = body.get("conversation_id")
    if conv_id:
        try:
            conv = Conversation.objects.filter(id=uuid.UUID(str(conv_id))).first()
        except ValueError:
            return json_err(ui_text.BAD_CONVERSATION_ID)
        if not conv:
            return json_err(ui_text.NO_CONVERSATION, status=404)
    if conv is None:
        conv = Conversation.objects.create(title=ui_text.MCP_CONVERSATION_TITLE, source="mcp")

    with transaction.atomic():
        locked = Conversation.objects.select_for_update().get(id=conv.id)
        if locked.messages.filter(archived=False, status="pending").exists():
            return json_err(ui_text.BUSY_CONVERSATION, status=409)
        seq = _next_seq(locked)
        user_msg = Message.objects.create(
            conversation=locked, seq=seq, role="user", status="done", content=text
        )
        for idx, (data, filename, mime, width, height) in enumerate(decoded):
            att = Attachment(
                message=user_msg,
                kind="upload",
                mime=mime,
                width=width,
                height=height,
                size_bytes=len(data),
                position=idx,
            )
            att.file.save(f"{uuid.uuid4().hex}-{filename}", ContentFile(data), save=True)
        # the conversation's own settings, as the web send uses them: a
        # conversation continued over MCP reflects and keeps references the
        # way it does in the browser
        stored = gensettings.of(locked)
        assistant = Message.objects.create(
            conversation=locked,
            seq=seq + 1,
            role="assistant",
            status="pending",
            force_n_images=n_images,
            force_reflection_rounds=stored["reflection_rounds"],
            sticky_refs=stored["sticky_refs"],
            heartbeat_at=timezone.now(),
        )
        locked.save(update_fields=["updated_at"])

    pipeline.run_generation(assistant.id)  # synchronous
    assistant.refresh_from_db()

    if assistant.status == "error":
        return json_ok(
            {
                "ok": False,
                "conversation_id": str(conv.id),
                "error": assistant.error or ui_text.GENERATION_FAILED,
            }
        )

    files = []
    for att in assistant.attachments.filter(kind="generated"):
        obj = _attachment_file_obj(att)
        if obj:
            files.append(obj)
    return json_ok(
        {
            "ok": True,
            "conversation_id": str(conv.id),
            "is_image_task": assistant.kind == "image",
            "reply": assistant.content,
            "nai_prompts": assistant.nai_prompts,
            "images": files,
        }
    )


@csrf_exempt
@mcp_token_required
@require_POST
def generate(request):
    """Direct NovelAI generation (no model call). Archived as an MCP conversation."""
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return json_err(ui_text.BAD_JSON_BODY)

    base_prompt = str(body.get("base_prompt") or "").strip()
    if not base_prompt:
        return json_err(ui_text.BASE_PROMPT_EMPTY)

    chars = []
    for item in body.get("character_prompts") or []:
        if isinstance(item, str):
            item = {"prompt": item}
        if not isinstance(item, dict):
            continue
        prompt = str(item.get("prompt") or "").strip()
        if not prompt:
            continue
        cx, cy = item.get("center_x"), item.get("center_y")
        if not (isinstance(cx, (int, float)) and isinstance(cy, (int, float))):
            cx = cy = None
        chars.append({"prompt": prompt, "center_x": cx, "center_y": cy})

    size = body.get("size")
    if size not in ("portrait", "landscape", "square"):
        size = "portrait"
    try:
        n_images = min(max(int(body.get("n_images") or 1), 1), settings.MAX_IMAGES_PER_REQUEST)
    except (TypeError, ValueError):
        n_images = 1

    nai_prompts = {
        "base_prompt": base_prompt,
        "character_prompts": chars,
        "negative_prompt": str(body.get("negative_prompt") or "").strip(),
        "size": size,
        "n_images": n_images,
    }

    conv = Conversation.objects.filter(title=ui_text.MCP_DIRECT_TITLE, source="mcp").first()
    if conv is None:
        conv = Conversation.objects.create(title=ui_text.MCP_DIRECT_TITLE, source="mcp")
    with transaction.atomic():
        locked = Conversation.objects.select_for_update().get(id=conv.id)
        seq = _next_seq(locked)
        Message.objects.create(
            conversation=locked,
            seq=seq,
            role="user",
            status="done",
            content=f"[MCP generate]\nbase: {base_prompt}\nchars: "
            f"{[c['prompt'] for c in chars]}\nnegative: {nai_prompts['negative_prompt']}\n"
            f"size: {size}, n: {n_images}",
        )
        assistant = Message.objects.create(
            conversation=locked,
            seq=seq + 1,
            role="assistant",
            status="pending",
            kind="image",
            nai_prompts=nai_prompts,
            heartbeat_at=timezone.now(),
        )
        locked.save(update_fields=["updated_at"])

    pipeline.run_redraw(assistant.id)  # synchronous NovelAI-only run
    assistant.refresh_from_db()

    if assistant.status == "error":
        return json_ok({"ok": False, "error": assistant.error or ui_text.GENERATION_FAILED})

    files = []
    seeds = []
    for att in assistant.attachments.filter(kind="generated"):
        obj = _attachment_file_obj(att)
        if obj:
            files.append(obj)
            seeds.append(att.seed)
    return json_ok({"ok": True, "images": files, "seeds": seeds, "size": size})


@csrf_exempt
@mcp_token_required
@require_GET
def quota(request):
    return json_ok(quota_service.get_status())
