"""
The WSGI entry point, and the only place background work is started.

`AppConfig.ready()` looks like the natural home for that, and it is not: Django
runs it for *every* management command, so `manage.py shell` on a live backend
would have started a sweeper — and the first thing a sweeper does is conclude
that every turn still marked pending belongs to a process that is gone. That is
true of a process that has just begun serving. It is emphatically not true of a
shell opened beside one that is painting.

Here it cannot be wrong: this module is imported when something is about to
serve requests, and never otherwise. The fact is positional rather than
inferred from `sys.argv`, which is the same reason the rest of this app stopped
inferring things it could instead read.
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vallotton.settings")

application = get_wsgi_application()

from django.conf import settings  # noqa: E402  — after the app registry is ready

if getattr(settings, "JOB_SWEEP_SECONDS", 0):
    from core.services import jobs  # noqa: E402

    jobs.start_sweeper()
