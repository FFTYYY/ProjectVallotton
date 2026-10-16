"""Small helpers shared by the JSON API views."""

from __future__ import annotations

import functools
import hmac
import uuid

from django.conf import settings
from django.http import HttpRequest, JsonResponse

from core.services import gensettings, prompt as prompt_slots
from core.models import Attachment, Conversation, Folder, Message
from core import ui_text
from core.services import notices


def json_ok(data: dict | list, status: int = 200) -> JsonResponse:
    return JsonResponse(data, status=status, safe=False, json_dumps_params={"ensure_ascii": False})


def json_err(message: str, status: int = 400, code: str = "") -> JsonResponse:
    return JsonResponse(
        {"error": message, "code": code or str(status)},
        status=status,
        json_dumps_params={"ensure_ascii": False},
    )


def api_login_required(view):
    @functools.wraps(view)
    def wrapper(request: HttpRequest, *args, **kwargs):
        if not request.user.is_authenticated:
            return json_err(ui_text.NOT_LOGGED_IN, status=401, code="unauthenticated")
        return view(request, *args, **kwargs)

    return wrapper


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def client_ip(request: HttpRequest) -> str:
    """
    The address the login throttle counts against.

    Never trust the LEFT-most X-Forwarded-For entry: every proxy appends to
    this header, so anything on the left is what the caller sent and can be
    forged (spoof a fresh value per request and the throttle never fires).
    We count from the right instead, skipping the entries our own proxies
    appended.

    TRUSTED_PROXY_HOPS = how many trailing entries to skip:
      1  host nginx (overwrites with its peer) + compose nginx (appends the
         host's address)  ->  ["<real client>", "<host nginx>"]  — our default
      0  compose nginx only, reached directly  ->  ["<real client>"]
    """
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        hops = max(int(getattr(settings, "TRUSTED_PROXY_HOPS", 1)), 0)
        if parts:
            # clamp so a shorter-than-expected chain still yields a real entry
            return parts[max(len(parts) - 1 - hops, 0)][:60]
    return request.META.get("REMOTE_ADDR", "?")[:60]


# ---------------------------------------------------------------- serializers


def attachment_to_dict(att: Attachment) -> dict:
    import os

    basename = os.path.basename(att.file.name or "file")
    return {
        "id": str(att.id),
        # which turn it belongs to: the lightbox shows the whole album and
        # needs to know, for a given picture, whose main picture it could be
        "message_id": str(att.message_id),
        "kind": att.kind,
        "url": f"/api/media/{att.id}/{basename}",
        # what a grid should ask for. The full picture is 1.3 MB at p95 and is
        # drawn at 410px at the widest, so a grid that uses `url` spends a
        # hundred times what it needs and fills the browser's cache with it.
        "thumb_url": f"/api/media/{att.id}/thumb",
        "mime": att.mime,
        "width": att.width,
        "height": att.height,
        "size_bytes": att.size_bytes,
        "seed": att.seed,
        "position": att.position,
    }


def message_to_dict(msg: Message) -> dict:
    return {
        "id": str(msg.id),
        "conversation_id": str(msg.conversation_id),
        "seq": msg.seq,
        "role": msg.role,
        "status": msg.status,
        # Said out loud so a reader that already holds this row can let it go.
        # A failed turn that a retry replaced used to sit in the transcript
        # reading 生成失败 beside the picture that replaced it, because the
        # only way the browser learned about a row was being sent it — and an
        # archived row was never sent again.
        "archived": msg.archived,
        "kind": msg.kind,
        "phase": msg.phase,
        "content": notices.content_of(msg),
        "error": msg.error,
        "client_msg_id": msg.client_msg_id,
        "nai_prompts": msg.nai_prompts,
        # what the lead model asked for, and which library entries were on when
        # it did — so a picture can always say what it was drawn from
        "draw_brief": msg.draw_brief,
        # which of this turn's pictures is the main one, by attachment position
        "main_index": msg.main_index,
        "selection_snapshot": msg.selection_snapshot,
        # what the picture actually took from the library, by entry and line
        "used_entries": prompt_slots.used_entries(msg.prompt, msg.selection_snapshot) if msg.prompt else [],
        # pictures of a batch that failed to paint, said as the row says them
        "paint_errors": ((msg.model_raw or {}).get("paint_errors") or []) if isinstance(msg.model_raw, dict) else [],
        "prompt_tokens": msg.prompt_tokens,
        # what reflection did on this turn, if it ran. Shown under the message
        # and never sent to a model — reflection is internal by definition.
        "reflection_log": msg.reflection_log,
        # set when the app sent this user message itself (continuous mode)
        "auto_sent": msg.auto_sent,
        "notice_kind": msg.notice_kind,
        "created_at": msg.created_at.isoformat(),
        "attachments": [attachment_to_dict(a) for a in msg.attachments.all()],
    }


def folder_breadcrumbs() -> dict[uuid.UUID, list[str]]:
    """
    Every folder's path, in one query.

    A list of conversations would otherwise walk each one's ancestor chain
    separately — up to eight queries per row — so the whole tree is read once
    and the paths are assembled here.
    """
    folders = {f.id: f for f in Folder.objects.all()}

    def path(fid: uuid.UUID | None) -> list[str]:
        out: list[str] = []
        seen: set[uuid.UUID] = set()
        while fid is not None and fid in folders and fid not in seen:
            seen.add(fid)
            node = folders[fid]
            out.append(node.name)
            fid = node.parent_id
        return list(reversed(out))

    return {fid: path(fid) for fid in folders}


def conversation_to_dict(
    conv: Conversation,
    preview: str | None = None,
    crumbs: dict[uuid.UUID, list[str]] | None = None,
) -> dict:
    folder_path: list[str]
    if crumbs is not None:
        folder_path = crumbs.get(conv.folder_id, []) if conv.folder_id else []
    elif conv.folder_id:
        try:
            folder = conv.folder
            folder_path = folder.breadcrumb() if folder else []
        except Folder.DoesNotExist:
            # the folder went away between the query and this line — a stale
            # reference is not worth a 500
            folder_path = []
    else:
        folder_path = []

    run = conv.continuous_run or None
    data = {
        "id": str(conv.id),
        # a run in flight, so the chip can count along with it. The browser
        # cannot advance it — that is the server's job now — it only reads.
        "continuous": {"left": run["left"], "of": run["of"]} if run else None,
        # what this conversation is set to generate with. Always complete and
        # in range, even for rows written before the column existed.
        "gen_settings": gensettings.of(conv),
        "title": conv.title,
        "source": conv.source,
        "is_archived": conv.is_archived,
        "folder_id": str(conv.folder_id) if conv.folder_id else None,
        # full breadcrumb so the header can show where this conversation lives
        "folder_path": folder_path,
        "position": conv.position,
        "created_at": conv.created_at.isoformat(),
        "updated_at": conv.updated_at.isoformat(),
    }
    if preview is not None:
        data["preview"] = preview
    return data
