from django.apps import AppConfig
from core import ui_text


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"
    verbose_name = ui_text.APP_NAME

    # No ready() hook on purpose.
    #
    # The job sweeper used to start here, and ready() runs for every management
    # command — so opening a shell on a live backend started a sweeper, which
    # promptly decided that the turn being painted belonged to a process that
    # had gone. It now starts in vallotton/wsgi.py, which is imported only when
    # something is about to serve.
