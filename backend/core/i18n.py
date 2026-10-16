"""
Which language the app speaks to the person.

The choice is the account's, not the browser's: it is a column on the one
AppSettings row, the settings page writes it, and every browser that logs in
reads it back. What it governs on this side is `core.ui_text` (every sentence
the backend shows) and the few lines of the prompts that name the language the
model should write in (core.prompts.wording.REPLY_LANGUAGE and friends). The
prompts themselves stay Chinese whatever is chosen.

How a read finds the choice: inside a `scope()` — one per request, opened by
core.middleware.LanguageScope, and one per generation turn, opened by
core.services.jobs — the row is read at most once and the answer kept, so a
request speaks one language from its first sentence to its last and costs one
query for it. Outside any scope (tests, shells, the background threads that
have no turn) every read goes to the database, which is always right and
never hot.

Adding a language: an entry in LANGUAGES, a core/ui_text_<code>.py with every
name of ui_text_zh.py, and a value under the code in each table of wording.py
that is keyed by language. tests/tests_i18n.py names what is missing. The
frontend keeps its own copy of the codes (src/i18n/languages.ts) and its
tests check it against this file.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from dataclasses import dataclass
from types import ModuleType
from collections.abc import Generator

from core import ui_text_en, ui_text_zh


@dataclass(frozen=True)
class Language:
    code: str
    #: how the language names itself; what a switch shows
    label: str
    #: the backend's sentences in it
    texts: ModuleType


LANGUAGES: tuple[Language, ...] = (
    Language("zh", "中文", ui_text_zh),
    Language("en", "English", ui_text_en),
)
#: what an installation speaks until the settings page says otherwise
DEFAULT = "zh"
CODES: tuple[str, ...] = tuple(lang.code for lang in LANGUAGES)
BY_CODE: dict[str, Language] = {lang.code: lang for lang in LANGUAGES}


def is_known(code: object) -> bool:
    return isinstance(code, str) and code in BY_CODE


def normalise(code: object) -> str:
    """The code if it is one of ours, else the default: a row written by a
    version that knew another language still reads."""
    return code if isinstance(code, str) and code in BY_CODE else DEFAULT


class _Scope:
    """One request's view of the choice: filled on first use, or pinned."""

    __slots__ = ("code", "pinned")

    def __init__(self, code: str | None = None, pinned: bool = False):
        self.code = code
        self.pinned = pinned


_scope: ContextVar[_Scope | None] = ContextVar("vallotton_language_scope", default=None)


@contextlib.contextmanager
def scope() -> Generator[None, None, None]:
    """Inside, the stored choice is read at most once, on first use."""
    token = _scope.set(_Scope())
    try:
        yield
    finally:
        _scope.reset(token)


@contextlib.contextmanager
def use(code: str) -> Generator[None, None, None]:
    """Speak `code` inside, whatever is stored. For tests, and for anything
    that must speak one language regardless of the choice."""
    if not is_known(code):
        raise ValueError(f"unknown language {code!r}; known: {', '.join(CODES)}")
    token = _scope.set(_Scope(code, pinned=True))
    try:
        yield
    finally:
        _scope.reset(token)


def stored() -> str:
    """The choice as the database has it right now."""
    from django.db import DatabaseError

    from core.models import AppSettings

    try:
        code = AppSettings.objects.filter(id=1).values_list("language", flat=True).first()
    except DatabaseError:
        # no table yet (first migrate): speak the default rather than fall over
        return DEFAULT
    return normalise(code)


def current() -> str:
    """The code everything reads: the scope's, else the database's."""
    s = _scope.get()
    if s is None:
        return stored()
    if s.code is None:
        s.code = stored()
    return s.code


def texts() -> ModuleType:
    """The ui_text module of the current language."""
    return BY_CODE[current()].texts


def set_language(code: str) -> str:
    """Store the choice. A scope that is open and not pinned speaks it from
    here on, so the request that changed it answers in it."""
    if not is_known(code):
        raise ValueError(f"unknown language {code!r}; known: {', '.join(CODES)}")
    from core.models import AppSettings

    AppSettings.get()
    AppSettings.objects.filter(id=1).update(language=code)
    s = _scope.get()
    if s is not None and not s.pinned:
        s.code = code
    return code
