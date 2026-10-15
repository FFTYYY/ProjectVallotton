"""Authenticated media serving (uploads + generated images)."""

from __future__ import annotations

import uuid
from urllib.parse import quote

from django.conf import settings
from django.http import FileResponse, HttpResponse, HttpResponseNotModified
from django.views.decorators.http import require_GET

from core.http import api_login_required, json_err
from core.models import Attachment
from core.services import imaging
from core import ui_text


def _cacheable(resp, att: Attachment, tag: str | None = None):
    """
    Say, as precisely as the protocol allows, that this file never changes.

    Three things have to be true together, and each one covers a different way
    the browser can end up downloading a 3.6 MB PNG it already has:

      * ``immutable`` — do not revalidate at all while it is fresh;
      * an ``ETag`` — when it is no longer fresh, or was evicted and fetched
        again, ask in one conditional request instead of downloading it. The
        id is the whole etag because the bytes under an attachment are written
        once and never edited, so there is nothing else for it to depend on;
      * ``same_for_everyone`` — read by core.middleware.DoesNotVaryByWhoAsked,
        which drops the ``Vary: Cookie`` that would otherwise make the cache
        key the entire cookie string. Safe because ``private`` already keeps
        the response out of any cache shared between people.
    """
    resp["Cache-Control"] = "private, max-age=31536000, immutable"
    resp["ETag"] = tag or f'"{att.id}"'
    resp.same_for_everyone = True
    return resp


def _serve(request, att: Attachment, stored, content_type: str, tag: str | None = None):
    """
    Answer with one stored file, saying everything that keeps it cached.

    Shared by the picture and its thumbnail because the only things that differ
    between them are which file and which etag — and the moment those two were
    two functions, one of them would grow a caching header the other did not.
    """
    tag = tag or f'"{att.id}"'
    # The browser has it and is only checking. Answering in a few bytes is the
    # difference this makes; the response still carries the caching headers,
    # because a 304 that says nothing about freshness buys only this one round
    # trip and leaves the next one to ask again.
    if tag in (request.headers.get("If-None-Match") or ""):
        return _cacheable(HttpResponseNotModified(), att, tag=tag)

    if settings.MEDIA_ACCEL:
        # nginx streams the file from the shared volume (location is `internal`)
        resp = HttpResponse()
        resp["Content-Type"] = content_type
        resp["X-Accel-Redirect"] = "/_protected_media/" + quote(stored.name)
        return _cacheable(resp, att, tag=tag)

    try:
        fh = stored.open("rb")
    except FileNotFoundError:
        return None
    return _cacheable(FileResponse(fh, content_type=content_type), att, tag=tag)


@api_login_required
@require_GET
def media(request, att_id: uuid.UUID, filename: str):
    """
    The picture itself, at the size it was made.

    `filename` is in the URL so that a saved file has a name and so that the
    path changes when the picture does; nothing in here reads it.
    """
    del filename
    att = Attachment.objects.filter(id=att_id).first()
    if not att or not att.file:
        return json_err(ui_text.NO_FILE, status=404)
    return _serve(request, att, att.file, att.mime or "application/octet-stream") or (
        json_err(ui_text.FILE_LOST, status=404)
    )


@api_login_required
@require_GET
def media_thumb(request, att_id: uuid.UUID):
    """
    A small JPEG of the same picture, for the grid.

    Made on the first request rather than when the picture was generated: that
    way every picture already on disk gets one, and no turn ever waits on it.

    Falls back to the original when one cannot be made. A grid that is slow is
    worse than a grid that is fast; a grid with a hole in it is worse than
    both.
    """
    att = Attachment.objects.filter(id=att_id).first()
    if not att or not att.file:
        return json_err(ui_text.NO_FILE, status=404)

    def original():
        return _serve(request, att, att.file, att.mime or "image/png") or json_err(
            ui_text.FILE_LOST, status=404
        )

    # `make_thumbnail` is the one that knows whether what is stored is still
    # the right size, so it is always asked rather than guarded on `att.thumb`
    if not imaging.make_thumbnail(att):
        return original()

    resp = _serve(request, att, att.thumb, "image/jpeg", tag=f'"{att.id}-t"')
    if resp is None:
        # the row says there is one and the file is gone. Forget it and let the
        # next request make it again rather than 404 a picture that exists.
        Attachment.objects.filter(id=att.id).update(thumb="")
        return original()
    return resp
