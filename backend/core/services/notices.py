"""
The app's own rows in a transcript: that a continuous run started or how it
ended, that the context is getting long.

A note is stored twice on its Message row. `notice` holds what happened — a
key and the numbers — and `content` holds the sentence in the language of the
moment it was written. A reader is shown the sentence made from `notice` now,
in the language the account speaks now (core.i18n), so a transcript does not
keep rows in the language of the day they were written; a row from before
`notice` existed is shown as its `content`. The written sentence is what the
admin and the logs see.

Nothing here reaches a model: these rows are the app talking to the person.
"""

from __future__ import annotations

from core import ui_text


def fmt_tokens(n: int) -> str:
    """A token count the way the current language shortens it: 12.3 万, 123.5k."""
    for unit, template in ui_text.TOKENS_SHORT_UNITS:
        if n >= unit:
            return template.format(n=n / unit)
    return str(n)


def render(note: object) -> str:
    """The note's sentence in the current language; "" for one it does not know."""
    if not isinstance(note, dict):
        return ""
    key = note.get("key")
    try:
        if key == "CONTINUOUS_START":
            return ui_text.CONTINUOUS_START.format(of=note["of"])
        if key == "CONTINUOUS_NO_PHRASE":
            return ui_text.CONTINUOUS_NO_PHRASE
        if key == "CONTINUOUS_SUMMARY":
            heads = ui_text.CONTINUOUS_HEADS
            return ui_text.CONTINUOUS_SUMMARY.format(
                head=heads.get(note.get("how"), heads["stopped"]),
                of=note["of"], done=note["done"], failed=note["failed"])
        if key == "CONTEXT_NEAR_LIMIT":
            return ui_text.CONTEXT_NEAR_LIMIT.format(used=fmt_tokens(note["used"]), limit=fmt_tokens(note["limit"]))
        if key == "LONG_CONTEXT":
            return ui_text.LONG_CONTEXT.format(threshold=fmt_tokens(note["threshold"]))
        if key == "LONG_CONTEXT_PRICED":
            return ui_text.LONG_CONTEXT_PRICED.format(threshold=fmt_tokens(note["threshold"]))
    except (KeyError, TypeError):
        return ""
    return ""


def content_of(msg) -> str:
    """What a reader is shown for a row: a note made now, else what was written."""
    if msg.role == "system":
        return render(msg.notice) or msg.content
    return msg.content
