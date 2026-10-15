"""
The settings page: what a new conversation starts from, and how things stand.

Everything here is app-wide. A conversation's own settings live on the
conversation and are edited from the right rail; this is only the shape a new
one is poured into.
"""

from __future__ import annotations

import json

from django.db.models import Count
from django.views.decorators.http import require_http_methods

from core.http import api_login_required, json_err, json_ok
from core.models import Conversation, Message
from core.services import gensettings, providers
from core import ui_text


def _running() -> dict:
    """
    What the backend is busy with, for someone who closed the tab and came back.

    A turn is running if its row is still pending; a run is armed if the
    conversation carries one. They are different numbers on purpose — a run of
    ten has one turn in flight and nine still to come.
    """
    turns = Message.objects.filter(
        archived=False, role="assistant", status="pending"
    ).aggregate(
        n=Count("id"),
        convs=Count("conversation_id", distinct=True),
    )
    runs = Conversation.objects.filter(continuous_run__isnull=False).count()
    return {
        "turns": turns["n"] or 0,
        "conversations": turns["convs"] or 0,
        "runs": runs,
    }


@api_login_required
@require_http_methods(["GET", "PATCH"])
def app_settings(request):
    if request.method == "PATCH":
        try:
            body = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            return json_err(ui_text.BAD_JSON)
        patch = body.get("gen_defaults")
        if not isinstance(patch, dict):
            return json_err(ui_text.GEN_DEFAULTS_NOT_OBJECT)
        gensettings.set_defaults(patch)

    return json_ok(
        {
            "gen_defaults": gensettings.defaults(),
            "running": _running(),
            "roads": providers.active().roads_status(),
        }
    )


@api_login_required
@require_http_methods(["POST"])
def road(request, name: str):
    """
    Report that a road is out, or that it is back.

    This is not a preference, and it is deliberately not stored. There is one
    authority on which roads work — what the backend last found out, held in
    the provider — and this writes to exactly that, setting the same state a
    request timing out would have set. Everything downstream carries on
    unchanged: the probe still runs, and the cheaper road still comes back the
    moment it answers, whatever anyone clicked.

    What it buys is who noticed. An endpoint that has stopped answering costs a
    full request timeout to discover, and the person watching 「思考中」 usually
    knows first. They are not overruling the backend; they are saving it the
    seven minutes.

    A second, stored copy of "which road to use" was the alternative, and it
    would have been a second authority on the same fact — the one that says
    keep using the fallback while the probe is saying the official road is back.
    """
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return json_err(ui_text.BAD_JSON)
    over = body.get("stepped_over")
    if not isinstance(over, bool):
        return json_err(ui_text.STEPPED_OVER_NOT_BOOL)
    provider = providers.active()
    if not provider.step_over(name, over=over):
        return json_err(ui_text.NO_ROUTE, status=404)
    return json_ok({"roads": provider.roads_status()})
