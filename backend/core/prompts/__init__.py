"""
System prompts, kept as plain .txt next to this module so they can be read and
edited without touching code.

  lead.txt            the lead model — persona, conversation, entries, the picture
  lead_reflection.txt what the lead model is asked when it looks at its own picture
  drawing.txt         the drawing model — assembles a picture out of the exposed entries
  patrol.txt          the patrol model — fills an entry's tags and prose from its note
  compressor.txt      the drawing model again, asked to make a prompt fit NovelAI's limit
  entries.txt         how an entry's tags and prose are written: one file, read by the
                      lead and the patrol both, so there is one copy of the rules.
                      lead.txt takes it where the line {{entries.txt}} stands.

Files are read once per process and cached; edit one and restart the backend
(in development the autoreloader does that for you).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from core import i18n
from core.prompts import wording

PROMPT_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=None)
def load(name: str) -> str:
    """Return the text of `<name>.txt`, stripped of a trailing newline."""
    path = PROMPT_DIR / f"{name}.txt"
    return path.read_text(encoding="utf-8").rstrip("\n")


#: the line in lead.txt that entries.txt is dropped in place of
ENTRIES_MARK = "{{entries.txt}}"

#: the line in lead.txt and drawing.txt where the 「使用场景」 section goes.
#: Its text is configuration (LEAD_USAGE_SCENARIO / DRAW_USAGE_SCENARIO in
#: .env): what this installation is for. Empty means no such section.
USAGE_MARK = "{{usage_scenario}}"
USAGE_HEADING = "## 使用场景"


def _with_usage(text: str, scenario: str, file: str) -> str:
    """The marker line becomes the heading plus `scenario`; with no scenario
    the line goes, and the blank line under it with it, so the sections
    around it stay single-spaced."""
    assert USAGE_MARK in text, f"{file} has lost its {USAGE_MARK} line"
    scenario = (scenario or "").strip()
    if scenario:
        return text.replace(USAGE_MARK, f"{USAGE_HEADING}\n{scenario}")
    if USAGE_MARK + "\n\n" in text:
        return text.replace(USAGE_MARK + "\n\n", "", 1)
    return text.replace(USAGE_MARK, "")


def _usage(name: str) -> str:
    from django.conf import settings
    return getattr(settings, name, "") or ""


#: the lines in lead.txt and patrol.txt that name the language of what the
#: model writes for the person: the reply, and the note of a library entry.
#: What goes in is wording.REPLY_LANGUAGE / wording.PROSE_LANGUAGE for the
#: language the account chose (core.i18n); the rest of the prompt is Chinese
#: whatever that is.
REPLY_LANGUAGE_MARK = "{{reply_language}}"
PROSE_LANGUAGE_MARK = "{{prose_language}}"


def _in_language(text: str, file: str, *marks: tuple[str, dict[str, str]]) -> str:
    """Each (marker, table) pair: the marker becomes the table's phrase for
    the current language. A marker a file has lost is a bug, not a prompt."""
    code = i18n.current()
    for mark, table in marks:
        assert mark in text, f"{file} has lost its {mark}"
        text = text.replace(mark, table[code])
    return text


def entries() -> str:
    """entries.txt: how an entry's tags and prose are written. The lead and
    the patrol both read it, so there is one copy of the rules."""
    return load("entries")


def lead() -> str:
    text = _with_usage(load("lead"), _usage("LEAD_USAGE_SCENARIO"), "lead.txt")
    text = _in_language(text, "lead.txt", (REPLY_LANGUAGE_MARK, wording.REPLY_LANGUAGE),
                        (PROSE_LANGUAGE_MARK, wording.PROSE_LANGUAGE))
    assert ENTRIES_MARK in text, "lead.txt has lost its entries.txt line"
    return text.replace(ENTRIES_MARK, entries().rstrip("\n"))


def drawing() -> str:
    return _with_usage(load("drawing"), _usage("DRAW_USAGE_SCENARIO"), "drawing.txt")


def compressor() -> str:
    """The compressor's system prompt: compressor.txt. Handed what the
    drawing model read and the assembled prompt, when it is over NovelAI's
    limit, to make it fit. Nothing in it names a language."""
    return load("compressor")


def patrol() -> str:
    """The patrol's system prompt: patrol.txt, then the same entry-writing
    rules the lead reads. A rule the patrol followed and the lead did not
    (or the other way round) would be the two of them writing two kinds of
    entry for the same note."""
    text = _in_language(load("patrol"), "patrol.txt", (PROSE_LANGUAGE_MARK, wording.PROSE_LANGUAGE))
    return text + "\n\n" + entries().rstrip("\n")


def reflection(*, round: int, max_rounds: int) -> str:
    """
    The reflection turn's user message.

    Reflection reuses lead.txt as its system prompt — it is the same model
    doing the same job, looking at what came back — so the only new text is
    this, and it takes the two numbers that make a round make sense.
    """
    return load("lead_reflection").format(round=round, max_rounds=max_rounds)
