"""
How one drawing is introduced wherever the history mentions it.

There are two places that show a model a picture it made — the ordinary
history in `pipeline.build_history`, and the round-by-round block reflection
builds for itself — and they are not going to become one place: they are shown
at different moments, to answer different questions.

What they must not do is each decide separately how a drawing is described.
They did, and it went exactly the way that always goes: the drawing's id was
added to one of them and the other kept quiet about it, so the turn that is
literally looking at a picture was the one turn that could not say which
picture. Before that, the tags had already been duplicated and then patched
with a function-body import to dodge the import cycle — the shape of the
problem was visible, and got worked around instead of moved.

So it lives here. Both callers ask this module what to say, and the next thing
that belongs in that description is added once.

This module imports nothing from either caller, which is what keeps it usable
by both.
"""

from __future__ import annotations

from core.prompts import wording
from core.services import novelai


def describe_prompts(nai_prompts: dict) -> str:
    """The tags, rendered the way history shows them."""
    chars = nai_prompts.get("character_prompts") or []
    lines = [f"base_prompt: {nai_prompts.get('base_prompt', '')}"]
    for i, c in enumerate(chars):
        pos = ""
        if c.get("center_x") is not None:
            pos = f" @({c['center_x']},{c['center_y']})"
        lines.append(f"character[{i}]{pos}: {c.get('prompt', '')}")
    own_negative = novelai.without_default_negative(nai_prompts.get("negative_prompt") or "")
    if own_negative:
        lines.append(f"negative_prompt: {own_negative}")
    lines.append(
        f"size: {nai_prompts.get('size')}, n_images: {nai_prompts.get('n_images')}"
    )
    return "\n".join(lines)


def lines(
    nai_prompts: dict | None,
    *,
    with_tags: bool = True,
    with_images: bool = False,
) -> list[str]:
    """
    The whole note about one drawing, in order: who is speaking and which
    drawing this is, then the tags, then the pictures.

    Callers say only what they have to hand — the ordinary history rations how
    far back tags travel, and only some turns carry pictures — and never how
    any of it is worded. That is the point of this module: the same note used
    to be assembled in two places, in two different spellings, and they drifted.
    """
    draw_id = (nai_prompts or {}).get("draw_id")
    out = [
        wording.SYSTEM_NOTE_DRAW.format(id=draw_id)
        if draw_id
        else wording.SYSTEM_NOTE_PLAIN
    ]
    if with_tags and nai_prompts and nai_prompts.get("base_prompt"):
        out.append(wording.APP_NOTE_PROMPTS + "\n" + describe_prompts(nai_prompts))
    if with_images:
        out.append(wording.APP_NOTE_IMAGES)
    return out
