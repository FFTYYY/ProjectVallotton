"""
System prompts, kept as plain .txt next to this module so they can be read and
edited without touching code.

  lead.txt            the lead model — persona, conversation, and the decision to draw
  lead_reflection.txt what the lead model is asked when it looks at its own picture
  drawing.txt         the drawing model — turns a brief into NovelAI tags
  patrol.txt          the patrol model — rewrites an entry's tag lines from its text
  tag_lines.txt       how a tag line is written: one file, read by the lead and the
                      patrol both, so there is one copy of the rules. lead.txt takes
                      it where the line {{tag_lines.txt}} stands.

Files are read once per process and cached; edit one and restart the backend
(in development the autoreloader does that for you).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from core.prompts import wording

PROMPT_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=None)
def load(name: str) -> str:
    """Return the text of `<name>.txt`, stripped of a trailing newline."""
    path = PROMPT_DIR / f"{name}.txt"
    return path.read_text(encoding="utf-8").rstrip("\n")


#: the line in lead.txt that tag_lines.txt is dropped in place of
TAG_LINES_MARK = "{{tag_lines.txt}}"

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


def lead() -> str:
    text = _with_usage(load("lead"), _usage("LEAD_USAGE_SCENARIO"), "lead.txt")
    assert TAG_LINES_MARK in text, "lead.txt has lost its tag_lines.txt line"
    return text.replace(TAG_LINES_MARK, load("tag_lines").rstrip("\n"))


def tag_lines() -> str:
    return load("tag_lines")


def drawing() -> str:
    return _with_usage(load("drawing"), _usage("DRAW_USAGE_SCENARIO"), "drawing.txt")


def patrol() -> str:
    """The patrol model's system prompt: patrol.txt, then the same tag-line
    rules the lead reads. A rule the patrol followed and the lead did not (or
    the other way round) would be the two of them writing two kinds of line
    for the same entry."""
    return load("patrol") + "\n\n" + load("tag_lines").rstrip("\n")


def drawing_edit() -> str:
    """
    What is appended to drawing.txt on an edit turn: this is not a new
    picture, here is the one being changed, return a change list.

    Text rather than a file because it is a few sentences that name the
    shape of the turn, and the shape is the app's — the rules it points at
    are in drawing.txt, which is the user's.
    """
    return wording.DRAWING_EDIT_INTRO


def reflection(*, round: int, max_rounds: int) -> str:
    """
    The reflection turn's user message.

    Reflection reuses lead.txt as its system prompt — it is the same model
    doing the same job, looking at what came back — so the only new text is
    this, and it takes the two numbers that make a round make sense.
    """
    return load("lead_reflection").format(round=round, max_rounds=max_rounds)

