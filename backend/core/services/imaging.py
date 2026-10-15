"""
Turning a stored attachment into something a model can look at.

That is all this is now. There used to be a whole step here: every uploaded
reference was described by a dedicated vision model and the text went to the
lead model in place of the picture, because reading a careful description beat
looking at the image. That stopped being true — the lead models now see well
enough that the middleman only lost detail — so the picture goes straight to
them and this module keeps only the encoder.
"""

from __future__ import annotations

import base64
import io
import logging

from django.core.files.storage import default_storage

log = logging.getLogger(__name__)

#: Longest edge of the copy the grid is shown first.
#:
#: The widest a picture is ever drawn there is 410px — a lone image; two-up is
#: about 200, and a phone gets one column — so a 2x screen wants 820. 768 was
#: under that and looked it. Measured on real pictures: 768 is 81 KB, 1024 is
#: 158 KB, and the service worker's 300 of them go from 24 MB to 46 MB, which
#: a phone still keeps. The full picture, for scale, is 3.6 MB.
#:
#: This is only what arrives FIRST. The grid asks for the full picture right
#: after and swaps it in, so this size decides how good the first paint looks
#: and how much survives in the cache, not the final quality.
THUMB_EDGE = 1024
THUMB_QUALITY = 85


def thumb_name(att) -> str:
    """
    What this attachment's thumbnail is called, at the size we make them now.

    The size is in the name so that changing THUMB_EDGE is enough: a stored
    thumbnail whose name does not match is one made at some other size, and
    it re-makes itself the next time it is asked for. The alternative is a
    column recording which size each one is, which is the same fact written
    twice — and the copy that goes stale is always the one nobody looks at.
    """
    return f"{att.id}-{THUMB_EDGE}.jpg"


def make_thumbnail(att) -> bool:
    """
    Write the small JPEG for an attachment. Says whether there is one now.

    Called the first time one is asked for. Concurrent callers may both do the
    work; they write the same bytes to the same path, so the loser of that race
    costs a little CPU and nothing else — which is cheaper than the lock that
    would prevent it.
    """
    if att.thumb and att.thumb.name.endswith(thumb_name(att)):
        return True
    stale = att.thumb.name if att.thumb else ""
    try:
        from PIL import Image

        with att.file.open("rb") as fh:
            img = Image.open(io.BytesIO(fh.read()))
            img.load()
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((THUMB_EDGE, THUMB_EDGE))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=THUMB_QUALITY)
        from django.core.files.base import ContentFile

        # a same-size file already on disk is the one being replaced; without
        # this Django keeps both and invents a suffix for the new one
        name = thumb_name(att)
        beside = f"conversations/{att.message.conversation_id}/thumbs/{name}"
        if default_storage.exists(beside):
            default_storage.delete(beside)
        att.thumb.save(name, ContentFile(buf.getvalue()), save=True)
        if stale and stale != att.thumb.name:
            # made at a size nothing asks for any more
            try:
                default_storage.delete(stale)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not remove the old thumbnail %s: %s", stale, exc)
        return True
    except Exception as exc:  # noqa: BLE001 — a picture is never worth a 500
        log.warning("could not make a thumbnail for %s: %s", att.id, exc)
        return False


def attachment_data_url(att) -> str | None:
    """
    The picture, as the model is shown it: the stored file, untouched.

    It used to be scaled to 1344px and re-encoded as JPEG. Both of those were
    losses taken for no one's benefit — the model is being asked to judge a
    drawing it is about to redraw, and every artifact that is introduced here
    is one it cannot tell apart from an artifact in the work.

    So this reads the file and sends it. Nothing is decoded, nothing is
    resampled, nothing is re-compressed; the only cost left is base64, which
    measured at 0.7 ms against 47 ms for the encode this replaces.
    """
    try:
        with att.file.open("rb") as fh:
            raw = fh.read()
        mime = att.mime or "image/png"
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    except Exception as exc:  # noqa: BLE001 — a picture is never worth a 500
        log.warning("could not read attachment %s: %s", att.id, exc)
        return None
