"""
NovelAI image-generation client (V5, text-to-image only — never img2img).

The API returns a ZIP whose first entry is the PNG. Requests are sent one
image at a time (n_samples=1) so Opus-tier free generation applies and so a
cancel between images takes effect quickly.
"""

from __future__ import annotations

import io
import logging
import os
import secrets
import time
import zipfile

import httpx
from django.conf import settings

from .spend import record_nai_images

log = logging.getLogger(__name__)

API_URL = "https://image.novelai.net/ai/generate-image"

#: The most pixels Opus will draw without charging Anlas: one at a time, no
#: base image, steps <= 28, and at most 1024x1024 worth. The cap is on the
#: pixel count rather than either side, so 832x1216 is inside it and 1024x1536
#: is half again over.
OPUS_FREE_PIXELS = 1024 * 1024

#: Sizes inside the free cap, and the larger set that is not.
NORMAL_SIZES = {
    "portrait": (832, 1216),
    "landscape": (1216, 832),
    "square": (1024, 1024),
}
LARGE_SIZES = {
    "portrait": (1024, 1536),
    "landscape": (1536, 1024),
    "square": (1472, 1472),
}

#: What each quality tier costs, measured on a live Opus account 2026-09-15:
#:
#:   low   v4.5, normal size   Anlas unchanged, V5 allowance unchanged
#:   mid   v5,   normal size   Anlas unchanged, V5 allowance down one point
#:   high  v5,   large size    45 Anlas per picture
#:
#: The tier picks a model and a size band; which way round the picture goes is
#: still the drawing model's call, so the two are separate choices.
TIERS: dict[str, dict] = {
    "low": {"model": "nai-diffusion-4-5-full", "sizes": NORMAL_SIZES},
    "mid": {"model": "nai-diffusion-5-full", "sizes": NORMAL_SIZES},
    "high": {"model": "nai-diffusion-5-full", "sizes": LARGE_SIZES},
}
DEFAULT_TIER = "mid"

#: Kept so anything still reading it gets the free sizes.
SIZES = NORMAL_SIZES

#: The quality words, always appended. NovelAI's own client adds these in the
#: browser as its "standard" preset — they are not sent as a flag, and the
#: `qualityToggle` field in the payload does nothing on the server (measured:
#: true and false draw the same image, pixel for pixel). So the only way to get
#: them is to write them into the prompt, which until now only happened when
#: the drawing model felt like it: of twelve measured calls it wrote them seven
#: times and left them out five. They are worth being consistent about — adding
#: them moves 97.5% of the pixels.
#:
#: This string is the preset for nai-diffusion-4-5-full and nai-diffusion-5-full,
#: which are the two models the tiers use. The `-curated` variants have a
#: different one; if a tier ever points at those, this has to grow a table.
QUALITY_WORDS = "very aesthetic, masterpiece"
#: Sent with the quality words when the picture renders no text. Sent beside a
#: text it is read as part of the text: a sign once came out reading
#: 「Midnight Pages, no text」.
NO_TEXT_WORD = "no text"

#: Words NovelAI understands that are NOT tags on Danbooru. Kept so that
#: checking a prompt against the tag index does not report the app's own
#: standard vocabulary as "no such tag" on every single picture.
#: Measured against the bundled index: of the words below, none are on the site.
CONVENTION_WORDS = frozenset({
    "very aesthetic", "masterpiece", "no text", "best quality", "amazing quality",
    "worst quality", "bad quality", "very displeasing", "screentone",
})

# Standard quality negative, always appended (the drawing model only supplies
# content-specific negatives on top of this).
DEFAULT_NEGATIVE = (
    "lowres, artistic error, film grain, scan artifacts, worst quality, "
    "bad quality, jpeg artifacts, very displeasing, chromatic aberration, "
    "dithering, halftone, screentone, multiple views, logo, too many "
    "watermarks, negative space, blank page"
)


class NovelAIError(Exception):
    """
    Any failure talking to NovelAI.

    `status` is the HTTP status when there was one, and None when nothing came
    back at all. Whoever decides whether to try again needs it: 429 means wait
    a moment, and 402 means the account is out of Anlas, and those two look
    identical from behind a message string.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class GenerationCancelled(Exception):
    pass


def tier_of(name) -> dict:
    """The model and size band for a tier name, falling back to the default."""
    return TIERS.get(str(name or ""), TIERS[DEFAULT_TIER])


def tags_to_check(prompt: str) -> list[str]:
    """
    The pieces of a prompt that it makes sense to look up on Danbooru.

    A prompt is not only tags, so three kinds of thing are left out rather than
    reported as unknown tags:

      * NovelAI's own vocabulary (QUALITY_WORDS and friends) — real words that
        the model understands and the site has never heard of;
      * a clause of plain English, which drawing.txt allows for the relations
        tags cannot hold. Five words is the line: no Danbooru tag is longer,
        and a sentence is always longer;
      * the emphasis syntax — `1.2::tag::` is that tag, weighted.

    Returns them in the order they appear, lower-cased, duplicates and all,
    because "which of these does the site not have" is the only question asked
    of the result.
    """
    out: list[str] = []
    for piece in (prompt or "").split(","):
        piece = " ".join(piece.strip().lower().split())
        # 1.2::tag:: and -0.8::tag:: — the weight is not part of the tag
        if "::" in piece:
            parts = [p for p in piece.split("::") if p.strip()]
            piece = parts[-1].strip() if parts else ""
            # a bare number is the weight with its tag in the next piece
            if piece.replace(".", "").replace("-", "").isdigit():
                continue
        # {tag} and [tag] are emphasis wrappers; parentheses are NOT, they are
        # part of the name on a great many tags (`oil painting (medium)`,
        # `hatsune_miku_(vocaloid)`), and stripping them turned real tags into
        # unknown ones.
        while len(piece) > 1 and piece[0] in "{[" and piece[-1] in "}]":
            piece = piece[1:-1].strip()
        if not piece or len(piece.split()) > 5 or piece in CONVENTION_WORDS:
            continue
        out.append(piece)
    return out


def tag_key(piece: str) -> str:
    """
    One tag, as the app tells two apart: lower-cased, underscores read as
    spaces, NovelAI's emphasis taken off -- `{{rim light}}`, `[rim light]`,
    `1.2::rim light::` and `rim_light` are all the tag `rim light`.

    This is the key everything dedupes on: prompt.join writes a tag once
    whatever its dressing, prompt.lint reports a repeat by the same rule, and
    merge_negative keeps the default negative behind the picture's own by it.
    Three places with three ideas of "the same tag" would send a tag twice,
    which is how NovelAI is told to weight it. Empty for a piece that is only
    a weight (`1.5::`), which is not a tag and is left as it is.
    """
    piece = " ".join((piece or "").replace("_", " ").strip().lower().split())
    if "::" in piece:
        parts = [p.strip() for p in piece.split("::") if p.strip()]
        piece = parts[-1] if parts else ""
        if piece.replace(".", "").replace("-", "").isdigit():
            return ""
    while len(piece) > 1 and piece[0] in "{[" and piece[-1] in "}]":
        piece = piece[1:-1].strip()
    return piece


def quality_words(has_text: bool) -> str:
    """The words NovelAI's own client adds to every prompt: the quality words,
    and `no text` only when the picture is not asked to render any text."""
    return QUALITY_WORDS if has_text else f"{QUALITY_WORDS}, {NO_TEXT_WORD}"


def with_quality(base_prompt: str) -> str:
    """
    The prompt with the quality words, for a row that does not carry them.

    prompt.render puts them in their place (the end of the tag list, before
    the sentences and the text) when it finishes a prompt for the app, so for
    today's rows this adds nothing. A row from before that -- written by an
    earlier version, by MCP -- gets the missing ones on the end, and never
    `no text`: on the end means after a text, where NovelAI reads it as part
    of the text.
    """
    have = {t for t in tags_to_check(base_prompt)}
    said = (base_prompt or "").strip().rstrip(",")
    missing = [
        w for w in (w.strip() for w in QUALITY_WORDS.split(","))
        if w and w.lower() not in have and w.lower() not in said.lower()
    ]
    if not missing:
        return said
    return f"{said}, {', '.join(missing)}" if said else ", ".join(missing)


def without_default_negative(negative_prompt: str) -> str:
    """The picture's own negative words: what the model wrote, with the
    app's default negative taken back out (for showing the model its own
    work, not the boilerplate)."""
    default = {t.strip().lower() for t in DEFAULT_NEGATIVE.split(",")}
    return ", ".join(t.strip() for t in (negative_prompt or "").split(",")
                     if t.strip() and t.strip().lower() not in default)


def merge_negative(negative_prompt: str) -> str:
    """The negative with the default negative behind it, each word once."""
    out: list[str] = []
    seen: set[str] = set()
    for word in [t.strip() for t in (negative_prompt or "").split(",")] + \
            [t.strip() for t in DEFAULT_NEGATIVE.split(",")]:
        key = tag_key(word)
        if word and key not in seen:
            seen.add(key)
            out.append(word)
    return ", ".join(out)


def _build_payload(
    base_prompt: str,
    character_prompts: list[dict],
    negative_prompt: str,
    size: str,
    seed: int,
    tier: str = DEFAULT_TIER,
) -> dict:
    band = tier_of(tier)
    sizes = band["sizes"]
    width, height = sizes.get(size, sizes["portrait"])
    base_prompt = with_quality(base_prompt)
    negative = merge_negative(negative_prompt)

    char_captions = []
    use_coords = False
    for item in character_prompts:
        cx, cy = item.get("center_x"), item.get("center_y")
        if cx is not None and cy is not None:
            use_coords = True
            centers = [{"x": cx, "y": cy}]
        else:
            centers = [{"x": 0.5, "y": 0.5}]
        char_captions.append({"char_caption": item["prompt"], "centers": centers})

    return {
        "input": base_prompt,
        "model": band["model"],
        "action": "generate",
        "parameters": {
            "params_version": 3,
            "width": width,
            "height": height,
            "scale": 5.0,
            "sampler": "k_euler_ancestral",
            "steps": 28,
            "seed": seed,
            "n_samples": 1,
            "ucPreset": 0,
            "qualityToggle": True,
            "negative_prompt": negative,
            "v4_prompt": {
                "caption": {
                    "base_caption": base_prompt,
                    "char_captions": char_captions,
                },
                "use_coords": use_coords,
                "use_order": True,
            },
            "v4_negative_prompt": {
                "caption": {
                    "base_caption": negative,
                    "char_captions": [
                        {"char_caption": "", "centers": c["centers"]}
                        for c in char_captions
                    ],
                },
                "legacy_uc": False,
            },
        },
    }


def _generate_one(payload: dict) -> bytes:
    last_status = None
    for attempt in range(3):
        try:
            with httpx.Client(
                timeout=httpx.Timeout(settings.NAI_TIMEOUT_SECONDS, connect=15.0)
            ) as client:
                resp = client.post(
                    API_URL,
                    headers={
                        "Authorization": f"Bearer {settings.NOVELAI_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except httpx.HTTPError as exc:
            if attempt < 2:
                log.warning("novelai network error, retrying: %s", exc)
                time.sleep(5 * (attempt + 1))
                continue
            raise NovelAIError(f"novelai network error: {exc}") from exc

        last_status = resp.status_code
        if resp.status_code == 200:
            try:
                with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                    names = zf.namelist()
                    if not names:
                        raise NovelAIError("novelai returned an empty zip")
                    return zf.read(names[0])
            except zipfile.BadZipFile as exc:
                raise NovelAIError("novelai returned invalid zip data") from exc
        if resp.status_code in (429, 500, 502, 503, 520) and attempt < 2:
            log.warning("novelai HTTP %s, retrying (%d)", resp.status_code, attempt + 1)
            time.sleep(6 * (attempt + 1))
            continue
        raise NovelAIError(
            f"novelai HTTP {resp.status_code}: {resp.text[:300]}",
            status=resp.status_code,
        )
    raise NovelAIError(f"novelai failed after retries (last status {last_status})")


def _placeholder_png(size: str, seed: int, tier: str = DEFAULT_TIER) -> bytes:
    """
    A picture-shaped file, drawn here. Only for the e2e stack — see
    settings.FAKE_NOVELAI.

    It is the right pixel size for the size it was asked for, and its colour
    follows the seed, because the suites check both: that a portrait is a
    portrait, and that a redraw produced a different image rather than the
    same one twice.
    """
    from PIL import Image, ImageDraw

    # painting is a phase the UI names while it happens; finishing between two
    # polls would make it untestable — see providers/fake._pause
    try:
        ms = int(os.environ.get("FAKE_LATENCY_MS", "600"))
    except ValueError:
        ms = 600
    if ms > 0:
        time.sleep(ms / 1000.0)

    sizes = tier_of(tier)["sizes"]
    width, height = sizes.get(size, sizes["portrait"])
    hue = (seed % 360) / 360.0
    shade = tuple(int(90 + 120 * abs(((hue * 3 + i / 3.0) % 1.0) - 0.5)) for i in range(3))
    img = Image.new("RGB", (width, height), shade)
    draw = ImageDraw.Draw(img)
    draw.rectangle([width // 6, height // 6, width * 5 // 6, height * 5 // 6],
                   outline=(245, 240, 230), width=max(2, width // 120))
    draw.text((width // 6 + 12, height // 6 + 12), f"fake {seed}", fill=(245, 240, 230))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def generate_one(
    base_prompt: str,
    character_prompts: list[dict] | None,
    negative_prompt: str,
    size: str,
    seed: int | None = None,
    *,
    tier: str = DEFAULT_TIER,
) -> tuple[bytes, int]:
    """
    Generate a single image. Returns (png_bytes, seed).

    `tier` is keyword-only: it arrived after `seed` in a call that passes its
    arguments positionally, so for one build the tier was being handed over as
    the seed. Every picture still came back, at the default tier, with a seed
    of "high" — which is the kind of wrong that looks like nothing at all.
    """
    if seed is None:
        seed = secrets.randbelow(2**32 - 1)
    if settings.FAKE_NOVELAI:
        return _placeholder_png(size, seed, tier), seed
    payload = _build_payload(
        base_prompt, character_prompts or [], negative_prompt, size, seed, tier
    )
    png = _generate_one(payload)
    record_nai_images(1)
    return png, seed
