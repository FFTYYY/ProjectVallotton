"""Session auth for the single configured app user, with a DB-backed throttle."""

from __future__ import annotations

import json
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model, login as django_login, logout as django_logout
from django.db import transaction
from django.middleware.csrf import get_token
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from core.http import client_ip, constant_time_eq, json_err, json_ok
from core.models import LoginAttempt
from core import ui_text

log = logging.getLogger(__name__)

MAX_FAILS = 8
LOCK_MINUTES = 15


@require_GET
def csrf(request):
    return json_ok({"csrfToken": get_token(request)})


def _locked(ip: str) -> int:
    """Seconds remaining if locked, else 0."""
    row = LoginAttempt.objects.filter(key=f"ip:{ip}").first()
    if row and row.locked_until and row.locked_until > timezone.now():
        return int((row.locked_until - timezone.now()).total_seconds())
    return 0


def _register_fail(ip: str) -> None:
    """
    Count one failure for this address.

    Done under a row lock: parallel guesses would otherwise all read the same
    `fails` value, each write back 1, and the lock would never engage — the
    whole point of the throttle is to survive a concurrent attack.
    """
    key = f"ip:{ip}"
    with transaction.atomic():
        LoginAttempt.objects.get_or_create(key=key)
        row = LoginAttempt.objects.select_for_update().get(key=key)
        row.fails += 1
        if row.fails >= MAX_FAILS:
            row.locked_until = timezone.now() + timedelta(minutes=LOCK_MINUTES)
            row.fails = 0
            log.warning("login throttle: locked %s for %d min", ip, LOCK_MINUTES)
        row.save(update_fields=["fails", "locked_until", "updated_at"])


@require_POST
def login(request):
    ip = client_ip(request)
    wait = _locked(ip)
    if wait:
        return json_err(ui_text.TOO_MANY_ATTEMPTS.format(minutes=wait // 60 + 1), status=429)

    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return json_err(ui_text.BAD_JSON)
    username = str(body.get("username") or "")
    password = str(body.get("password") or "")

    ok = constant_time_eq(username, settings.APP_USERNAME) and constant_time_eq(
        password, settings.APP_PASSWORD
    )
    if not ok or not settings.APP_PASSWORD:
        _register_fail(ip)
        return json_err(ui_text.BAD_CREDENTIALS, status=401, code="bad_credentials")

    User = get_user_model()
    user, _ = User.objects.get_or_create(
        username=settings.APP_USERNAME, defaults={"is_staff": False}
    )
    django_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    LoginAttempt.objects.filter(key=f"ip:{ip}").delete()
    return json_ok({"ok": True, "username": user.username})


@require_POST
def logout(request):
    django_logout(request)
    return json_ok({"ok": True})


@require_GET
def me(request):
    if not request.user.is_authenticated:
        return json_ok({"authenticated": False})
    return json_ok({"authenticated": True, "username": request.user.username})
