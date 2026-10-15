"""
The Danbooru tag list, on disk, searchable — and the lookup that searches it.

NovelAI is trained on Danbooru, so its prompts are Danbooru tags: a tag that
exists on the site means something to the model, and a tag that does not is
noise dressed up as a tag. The drawing model writes those tags from memory,
which is where this comes in. It can now look one up instead.

Three decisions worth knowing before reading the code:

**The list is a file, not an API.** `core/data/danbooru-tags.tsv.gz` ships with
the app and is rebuilt by hand with `manage.py fetch_danbooru_tags`. A turn
that has to reach the internet to write tags is a turn that fails when the
internet does, and the tag list changes by a few hundred entries a week — there
is nothing here worth a network call, let alone one per search.

**Nothing hands the model the whole list.** It is ~71,000 tags; even trimmed to
names alone it would be most of a context window, and it would be sent on every
single drawing call to answer questions the model mostly does not have. So the
model searches, and pays only for what it asked about.

**One call, many queries.** `queries` is a list because the alternative is a
round trip per word, and a round trip is a whole model response — seconds each,
twenty of them for one picture. Searching is local and takes about a
millisecond a word, so the only cost that matters is the number of calls, and
batching is what makes that one.
"""

from __future__ import annotations

import gzip
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from core.prompts import wording


log = logging.getLogger(__name__)

#: Rebuilt by core/management/commands/fetch_danbooru_tags.py, which also
#: documents the format and what it leaves out.
DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "danbooru-tags.tsv.gz"

#: Danbooru's own names for its tag categories, by its own numbers. Kept
#: verbatim rather than translated: they are the site's vocabulary, and the
#: model is writing that site's tags. An unknown number prints as itself.
CATEGORIES = {0: "general", 1: "artist", 3: "copyright", 4: "character", 5: "meta"}

#: How many queries one call may carry, and how many tags come back for each.
#:
#: The per-query cap is what keeps a wide word like "hair" from answering with
#: four hundred tags; the hits are sorted best-first, so a cap takes the tail.
#: The two together bound what one call can cost the context: at most
#: MAX_QUERIES * PER_QUERY lines of about thirty characters.
MAX_QUERIES = 40
PER_QUERY = 8

#: Words that are not worth matching a tag on by themselves.
#:
#: Only grade 3 below looks at single words of a longer query, and that is where
#: these do damage: "on" is inside `on_bed`, `on_back`, `sitting_on_lap` and two
#: hundred others, so a search for "sitting on chair" came back led by the
#: biggest tags containing "on" and pushed `chair` off the end of the list.
#:
#: They are dropped as SEARCH TERMS only. Tags whose names contain them are
#: still found by every other grade, which is what "sitting on lap" should find.
STOPWORDS = frozenset(
    "a an and at by for from in of on or the to with".split()
)


@dataclass(frozen=True)
class Hit:
    """One tag that matched, as the model is shown it."""

    name: str
    count: int
    category: str
    #: matched only part of what was asked for — see grade 3 in `search`. Shown
    #: to the model as such: a near miss presented as a hit is how a prompt ends
    #: up carrying a tag that answers a different question.
    partial: bool = False


@dataclass(frozen=True)
class Index:
    """
    The tag list, arranged for the one question asked of it.

    Parallel lists rather than a list of objects, and one pre-built string per
    tag rather than a structure to walk: a search looks at every tag, so the
    inner loop is the whole cost. `blobs[i]` is tag i's name and every alias of
    it, underscores turned into spaces and the whole thing padded, so that a
    whole-word test is one substring check.

    Rows are in the file's order, which is by post count descending — so where
    two tags match equally well, the earlier row is the better known one.
    """

    names: list[str]
    counts: list[int]
    categories: list[int]
    blobs: list[str]
    #: a name OR an alias, spaced, to its row — "the model typed this exactly"
    exact: dict[str, int]

    def __len__(self) -> int:
        return len(self.names)


def _spaced(text: str) -> str:
    """A tag name as a phrase: underscores are how the site writes spaces."""
    return " ".join(text.replace("_", " ").split())


def normalize(query: str) -> str:
    """
    A query as it will be matched: lower case, one space between words.

    Both spellings arrive — the model writes "cherry blossoms" as readily as
    "cherry_blossoms" — and they are the same question, so they are made into
    the same string here rather than being two cases everywhere below.
    """
    return _spaced(str(query or "").strip().lower())


@lru_cache(maxsize=1)
def index() -> Index:
    """
    The tag list, read once per process.

    A missing or unreadable file is an empty index, not an exception: the only
    thing that depends on this is a lookup a model may ask for, and a drawing turn
    that fails outright because a lookup table is missing is a far worse outcome
    than one written from memory, which is what every turn did before this
    existed.
    """
    names: list[str] = []
    counts: list[int] = []
    categories: list[int] = []
    blobs: list[str] = []
    exact: dict[str, int] = {}
    try:
        with gzip.open(DATA_FILE, "rt", encoding="utf-8") as fh:
            for line in fh:
                # A comment, not a tag. The space is load-bearing: two real
                # tags begin with a hash (`#compass`, `#unicus_(idolmaster)`)
                # and a bare `startswith("#")` quietly ate both. No tag name on
                # the site contains a space, so this cannot eat anything.
                if line.startswith("# "):
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 3:
                    continue
                name, category, count = parts[0], parts[1], parts[2]
                aliases = parts[3].split() if len(parts) > 3 and parts[3] else []
                if not name:
                    continue
                row = len(names)
                names.append(name)
                categories.append(int(category) if category.isdigit() else -1)
                counts.append(int(count) if count.isdigit() else 0)
                texts = [_spaced(name)] + [_spaced(a) for a in aliases]
                for text in texts:
                    # first row wins: the file is best-known-first, and an alias
                    # shared by two tags should resolve to the one in use
                    exact.setdefault(text, row)
                blobs.append(" " + "  ".join(texts) + " ")
    except OSError as exc:
        log.warning("no danbooru tag index at %s (%s)", DATA_FILE, exc)
        return Index([], [], [], [], {})
    return Index(names, counts, categories, blobs, exact)


def search(query: str, limit: int = PER_QUERY) -> list[Hit]:
    """
    The tags that answer one query, best first.

    Four grades of match, best first. The grades are the point: a model asking
    for "paper umbrella" wants `oil-paper_umbrella` first and tags that merely
    contain "umbrella" after it, and "sorted by post count" alone cannot tell
    those apart — `umbrella` has six times the pictures.

      0. the query IS this tag, or an alias of it;
      1. every word of the query appears in it as a whole word;
      2. the query appears in it at all;
      3. SOME word of the query does, ignoring STOPWORDS. Only reachable for a
         query of several words, and reported as a partial match, because it is
         one: it answers a narrower question than the one that was asked.

    Within a grade, by how many of the query's words were matched and then by
    how many pictures carry the tag. Both halves matter at grade 3, where a tag
    matching two words out of three belongs above a far bigger tag matching one.
    """
    q = normalize(query)
    if not q:
        return []
    idx = index()
    words = q.split()
    padded = [f" {w} " for w in words]
    # Grade 3 only: see STOPWORDS. When every word is one, there is no grade 3
    # for this query, which is the right answer for a query of pure glue.
    narrowing = [f" {w} " for w in words if w not in STOPWORDS]

    found: list[tuple[int, int, int, int]] = []
    seen: set[int] = set()
    row = idx.exact.get(q)
    if row is not None:
        found.append((0, -len(words), -idx.counts[row], row))
        seen.add(row)

    # A tag containing no word of the query cannot match under any grade below,
    # and almost no tag contains any of them — so one substring pass per WORD
    # cuts seventy thousand tags down to a few hundred, and the grading below
    # only ever looks at those. One pass per word rather than a test per tag
    # because the test per tag runs seventy thousand times and this runs twice:
    # measured at twice the speed for the same answers.
    candidates: set[int] = set()
    for word in words:
        candidates.update(i for i, blob in enumerate(idx.blobs) if word in blob)
    candidates -= seen

    for i in candidates:
        blob = idx.blobs[i]
        whole = sum(1 for w in padded if w in blob)
        if whole == len(words):
            found.append((1, -whole, -idx.counts[i], i))
        elif q in blob:
            found.append((2, -whole, -idx.counts[i], i))
        elif len(words) > 1:
            # Counted over the narrowing words only, which is what decides the
            # order here: "sitting on chair" should put a tag that matched both
            # `sitting` and `chair` above one that matched either, and counting
            # "on" as well would rank every `on_*` tag above plain `chair`.
            narrow = sum(1 for w in narrowing if w in blob)
            if narrow:
                found.append((3, -narrow, -idx.counts[i], i))

    found.sort()
    return [
        Hit(
            idx.names[i],
            idx.counts[i],
            CATEGORIES.get(idx.categories[i], str(idx.categories[i])),
            partial=grade == 3,
        )
        for grade, _, _, i in found[: max(int(limit), 0)]
    ]


def unknown(tags) -> list[str]:
    """
    Which of these are not tags on the site, in the order they were given.

    Aliases count as known: `idx.exact` holds every alias too, so a prompt that
    says `paper umbrella` is not reported just because the site files it under
    `oil-paper_umbrella`.

    An empty index reports nothing rather than everything. A checkout without
    the data file would otherwise flag every tag in every picture, which is a
    worse answer than silence.
    """
    idx = index()
    if not len(idx):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        key = normalize(tag)
        if not key or key in seen:
            continue
        seen.add(key)
        if key not in idx.exact:
            out.append(str(tag).strip())
    return out


def answer(args: dict) -> str:
    """
    The whole lookup, from the queries the model sent to the text it reads.

    Written as lines rather than JSON because every character here is paid for
    twice — once going into the context and once in everything the model reads
    afterwards — and a table says the same thing in a third of the space.

    A query with nothing to show says so. An empty answer under a heading reads
    as a failed lookup; "no such tag" is the useful half of what the answer can
    say, and the only way the model can tell that it is about to invent one.

    Partial matches are fenced off under a line that says they are partial, for
    the same reason: "sitting on chair" finds `sitting` and `chair`, and a model
    shown those two in the same list as a real hit has been told something false
    about its own question.
    """
    raw = args.get("queries")
    if isinstance(raw, str):
        # one word, sent unwrapped. It is unambiguous, so it is answered
        raw = [raw]
    queries = [str(q) for q in raw or [] if str(q).strip()]
    if not queries:
        return wording.TOOL_TAGS_NO_QUERIES

    out: list[str] = [wording.TOOL_TAGS_COLUMNS]
    for query in queries[:MAX_QUERIES]:
        out.append(wording.TOOL_TAGS_QUERY.format(query=query))
        hits = search(query)
        if not hits:
            out.append(wording.TOOL_TAGS_NONE)
            continue
        fenced = False
        for h in hits:
            if h.partial and not fenced:
                out.append(wording.TOOL_TAGS_PARTIAL)
                fenced = True
            out.append(f"{h.name}\t{h.count}\t{h.category}")
    if len(queries) > MAX_QUERIES:
        out.append(wording.TOOL_TAGS_TRIMMED.format(n=MAX_QUERIES))
    return "\n".join(out)
