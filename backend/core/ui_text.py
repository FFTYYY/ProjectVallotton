"""
Every sentence the backend hands to a person, in the language the account
chose: `ui_text.NO_CONVERSATION` is the Chinese or the English sentence
according to core.i18n.current() at the moment it is read.

The sentences are in ui_text_zh.py and ui_text_en.py, one module per language
with the same names; this module only looks a name up in the right one. Prompt
wording lives in core/prompts/wording.py; this is the other voice, the one the
browser shows.

Read a name where you use it, not at import: a module-level
`X = ui_text.SOMETHING` is one language for ever. The Django admin wants
exactly that (it is served in Chinese, like Django's own admin) and imports
ui_text_zh by name.

Templates take their values by name: `NAME_REQUIRED.format(max=MAX_NAME)`.
"""

from __future__ import annotations

from typing import Any

from core import i18n


def __getattr__(name: str) -> Any:
    if name.startswith("_"):
        raise AttributeError(name)
    try:
        return getattr(i18n.texts(), name)
    except AttributeError:
        raise AttributeError(f"module 'core.ui_text' has no attribute {name!r}") from None


def __dir__() -> list[str]:
    return sorted(set(globals()) | {n for n in dir(i18n.texts()) if not n.startswith("_")})


def in_every_language(name: str) -> tuple[Any, ...]:
    """What `name` says in each language, for recognising something written
    down earlier: a conversation titled 新对话 is still untitled after the
    switch to English."""
    return tuple(getattr(lang.texts, name) for lang in i18n.LANGUAGES)


def new_conversation_title() -> str:
    """Conversation.title's default: a function, so it is read when the row
    is made and not when the model is imported."""
    return __getattr__("NEW_CONVERSATION_TITLE")
