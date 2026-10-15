from django.views.decorators.http import require_GET

from core.http import api_login_required, json_ok
from core.services import quota as quota_service


@api_login_required
@require_GET
def quota(request):
    return json_ok(quota_service.get_status())
