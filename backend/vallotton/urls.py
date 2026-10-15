from django.contrib import admin
from django.urls import include, path, re_path

from core.views import frontend

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("core.urls")),
    # Everything else is the single-page app's own routing (/c/…, /f/…,
    # /settings, /login), answered with index.html -- only when Django is the
    # one serving the frontend (SERVE_FRONTEND); otherwise a 404, as before.
    re_path(r"^(?!api/|admin/|django-static/).*$", frontend.index),
]
