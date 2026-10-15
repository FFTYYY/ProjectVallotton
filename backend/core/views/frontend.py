"""
The built frontend, when Django serves it itself.

The local form of the app (quickstart.py) runs one process: Django answers
the API, whitenoise hands out the files under frontend/dist, and this view
answers every other path with index.html so the single-page app can route
/c/…, /f/…, /settings and /login on its own. In production the switch is off,
nginx serves dist, and this view is a 404 for anything that reaches it.

index.html is never cached: it names the hashed asset files of one build, and
a browser holding an old one after an update would ask for files that no
longer exist. The assets themselves carry a hash in their name, so whitenoise
may cache those as it likes.
"""

from django.conf import settings
from django.http import Http404, HttpResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET


@never_cache
@require_GET
def index(request):
    if not settings.SERVE_FRONTEND:
        raise Http404("not serving the frontend")
    try:
        body = (settings.FRONTEND_DIST / "index.html").read_bytes()
    except OSError:
        raise Http404("frontend not built")
    return HttpResponse(body, content_type="text/html; charset=utf-8")
