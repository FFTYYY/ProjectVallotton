"""
The model providers, and the one the app is configured to use.

Adding a vendor means writing one module here and adding a line to REGISTRY.
Nothing outside this package should name a vendor: ask `active()` for the
provider and branch on its `capabilities`, never on its `name`.
"""

from __future__ import annotations

import threading

from django.conf import settings

from .base import Capabilities, Completion, LLMError, Provider
from .deepseek import DeepSeekProvider
from .fake import FakeProvider
from .xai import GrokProvider

REGISTRY: dict[str, type[Provider]] = {
    GrokProvider.name: GrokProvider,
    DeepSeekProvider.name: DeepSeekProvider,
    # answers instantly, costs nothing, reachable only via LLM_PROVIDER=fake
    FakeProvider.name: FakeProvider,
}

_lock = threading.Lock()
_cache: tuple[str, Provider] | None = None


def active() -> Provider:
    """
    The configured provider, built once per value of LLM_PROVIDER.

    Keyed on the setting rather than cached outright so a test can override it
    and get the provider it asked for.
    """
    global _cache
    name = (settings.LLM_PROVIDER or "").strip().lower()
    cls = REGISTRY.get(name)
    if cls is None:
        raise LLMError(
            f"LLM_PROVIDER={settings.LLM_PROVIDER!r} is not one of: "
            f"{', '.join(sorted(REGISTRY))}"
        )
    with _lock:
        if _cache is None or _cache[0] != name:
            _cache = (name, cls())
        return _cache[1]


__all__ = [
    "Capabilities",
    "Completion",
    "DeepSeekProvider",
    "FakeProvider",
    "GrokProvider",
    "LLMError",
    "Provider",
    "REGISTRY",
    "active",
]
