"""
Sync the two accounts from .env into the database (idempotent; runs on every
container start). .env is the single source of truth for credentials:
- APP_USERNAME / APP_PASSWORD      → frontend login (non-staff user)
- ADMIN_USERNAME / ADMIN_PASSWORD  → Django admin superuser
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Provision the app + admin accounts from environment variables."

    def handle(self, *args, **options):
        User = get_user_model()

        if not settings.APP_PASSWORD or not settings.ADMIN_PASSWORD:
            self.stderr.write(
                "APP_PASSWORD / ADMIN_PASSWORD 未设置 — 拒绝启动引导。"
            )
            raise SystemExit(2)

        app_user, created = User.objects.get_or_create(
            username=settings.APP_USERNAME,
            defaults={"is_staff": False, "is_superuser": False},
        )
        app_user.is_staff = False
        app_user.is_superuser = False
        app_user.set_password(settings.APP_PASSWORD)
        app_user.save()
        self.stdout.write(f"app user {'created' if created else 'synced'}: {app_user.username}")

        admin_user, created = User.objects.get_or_create(
            username=settings.ADMIN_USERNAME,
            defaults={"is_staff": True, "is_superuser": True},
        )
        admin_user.is_staff = True
        admin_user.is_superuser = True
        admin_user.set_password(settings.ADMIN_PASSWORD)
        admin_user.save()
        self.stdout.write(
            f"admin user {'created' if created else 'synced'}: {admin_user.username}"
        )
