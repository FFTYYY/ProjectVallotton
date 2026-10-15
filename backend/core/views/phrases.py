"""Global quick-phrase CRUD (title + content, appended to the composer by the UI)."""

from __future__ import annotations

import json
import uuid

from django.db import transaction
from django.db.models import Max
from django.views.decorators.http import require_http_methods

from core.http import api_login_required, json_err, json_ok
from core.models import QuickPhrase
from core import ui_text

MAX_TITLE = 100
MAX_CONTENT = 4000


def _phrase_to_dict(p: QuickPhrase) -> dict:
    return {
        "id": str(p.id),
        "title": p.title,
        "content": p.content,
        "position": p.position,
        "created_at": p.created_at.isoformat(),
    }


def _validate(body: dict, *, partial: bool) -> tuple[dict, str | None]:
    out: dict = {}
    if "title" in body or not partial:
        title = str(body.get("title") or "").strip()
        if not title or len(title) > MAX_TITLE:
            return {}, ui_text.TITLE_REQUIRED.format(max=MAX_TITLE)
        out["title"] = title
    if "content" in body or not partial:
        content = str(body.get("content") or "").rstrip()
        if not content or len(content) > MAX_CONTENT:
            return {}, ui_text.CONTENT_REQUIRED.format(max=MAX_CONTENT)
        out["content"] = content
    if not out:
        return {}, ui_text.NOTHING_TO_UPDATE_PHRASE
    return out, None


@api_login_required
@require_http_methods(["GET", "POST"])
def phrases(request):
    if request.method == "GET":
        return json_ok({"phrases": [_phrase_to_dict(p) for p in QuickPhrase.objects.all()]})

    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return json_err(ui_text.BAD_JSON)
    fields, err = _validate(body, partial=False)
    if err:
        return json_err(err)
    # at the end, where a new one belongs: the list is hand-ordered, and
    # dropping a new phrase into the middle of it would rearrange someone's
    # muscle memory on their behalf
    last = QuickPhrase.objects.aggregate(m=Max("position"))["m"]
    phrase = QuickPhrase.objects.create(position=(last or 0) + 1, **fields)
    return json_ok(_phrase_to_dict(phrase), status=201)


@api_login_required
@require_http_methods(["PATCH", "DELETE"])
def phrase_detail(request, phrase_id: uuid.UUID):
    phrase = QuickPhrase.objects.filter(id=phrase_id).first()
    if not phrase:
        return json_err(ui_text.NO_PHRASE, status=404)

    if request.method == "DELETE":
        phrase.delete()
        return json_ok({"ok": True})

    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return json_err(ui_text.BAD_JSON)
    fields, err = _validate(body, partial=True)
    if err:
        return json_err(err)
    for k, v in fields.items():
        setattr(phrase, k, v)
    phrase.save(update_fields=[*fields.keys(), "updated_at"])
    return json_ok(_phrase_to_dict(phrase))


@api_login_required
@require_http_methods(["POST"])
def reorder(request):
    """
    Put the phrases in the order the browser now shows them.

    The whole order arrives at once, rather than "move this one to index 3".
    The browser has just finished a drag and knows exactly what it is showing;
    sending the result means the two cannot end up believing different things,
    which an incremental move can do the moment two of them race.

    Ids not named keep their place after the named ones. A phrase created in
    another tab mid-drag should not vanish from the list or jump to the front.
    """
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return json_err(ui_text.BAD_JSON)
    ids = body.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        return json_err(ui_text.IDS_NOT_STRINGS)

    with transaction.atomic():
        known = {str(p.id): p for p in QuickPhrase.objects.select_for_update()}
        ordered = [known[i] for i in ids if i in known]
        rest = [p for k, p in known.items() if k not in set(ids)]
        for n, p in enumerate(ordered + sorted(rest, key=lambda p: (p.position, p.created_at))):
            p.position = n
        QuickPhrase.objects.bulk_update(ordered + rest, ["position"])

    return json_ok({"phrases": [_phrase_to_dict(p) for p in QuickPhrase.objects.all()]})
