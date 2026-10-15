"""
Rebuild the bundled Danbooru tag index from danbooru.donmai.us.

Run by hand, months apart; nothing in the app calls it. The file it writes is
what core/services/danbooru.py searches, and it is committed, because a drawing
turn may not depend on the internet being up or on a site's rate limit.

    python manage.py fetch_danbooru_tags

The format is one tag per line, tab separated, best known first:

    name <TAB> category <TAB> post_count <TAB> alias alias alias

`category` is Danbooru's own number (0 general, 1 artist, 3 copyright,
4 character, 5 meta). Aliases are space separated, which is safe because no tag
name on the site contains whitespace — verified over the whole list, and the
reason aliases can share a column at all. Lines starting with # are a header
that records when the file was made and from how many tags.

**What it leaves out, and why.** Danbooru has about 1.3 million tags, and most
of them are noise: a tag on twenty pictures was never learned by a model trained
on this site, so suggesting it to the drawing model would be worse than
suggesting nothing. Each category gets the cut-off that fits what it is for —
see MIN_POSTS. Deprecated tags go too: the site says not to use them.
"""

from __future__ import annotations

import gzip
import time
from datetime import date
from pathlib import Path

import httpx
from django.core.management.base import BaseCommand

from core.services import danbooru

API = "https://danbooru.donmai.us"

#: The fewest pictures a tag may have, per Danbooru category, to be worth
#: offering the drawing model. Measured against the live list on 2026-10-06:
#: 71k tags survive this, 1.9 MB of text, 780 KB gzipped.
#:
#:   general (0)    the vocabulary of a prompt — poses, clothes, lighting. The
#:                  loosest cut: a rare general tag still beats an invented one.
#:   artist (1)     a style only exists in a model if it saw a body of work, and
#:                  there are 97k artist tags under 200 pictures — alone they
#:                  would be more than half the file.
#:   copyright (3)  a series, and
#:   character (4)  a character, both of which a model knows by having seen them
#:                  many times over.
#:   meta (5)       almost all of it is about the FILE (resolution, scan source)
#:                  rather than the picture; only the best known are any use.
MIN_POSTS = {0: 50, 1: 200, 3: 100, 4: 100, 5: 200}
#: What a category nobody listed above has to clear.
MIN_POSTS_OTHER = 100

PER_PAGE = 1000
#: Danbooru asks for a real user agent and no hammering. One page a quarter
#: second is well inside what it allows, and the whole run takes four minutes.
PAUSE = 0.25
HEADERS = {"User-Agent": "vallotton-tagindex (private app, run by hand)"}


class Command(BaseCommand):
    help = "Rebuild core/data/danbooru-tags.tsv.gz from the Danbooru API."

    def add_arguments(self, parser):
        parser.add_argument(
            "--out",
            default=str(danbooru.DATA_FILE),
            help="where to write the index (default: the bundled file)",
        )

    def handle(self, *args, **options):
        out = Path(options["out"])
        floor = min(MIN_POSTS.values())
        with httpx.Client(timeout=60.0, headers=HEADERS) as client:
            tags = self._tags(client, floor)
            self.stdout.write(f"{len(tags)} tags clear the per-category minimum")
            aliases = self._aliases(client, {t["name"] for t in tags})
            self.stdout.write(f"{sum(len(v) for v in aliases.values())} aliases kept")

        out.parent.mkdir(parents=True, exist_ok=True)
        # written beside the real file and moved into place, so a run that dies
        # halfway through leaves the working index alone
        tmp = out.with_suffix(out.suffix + ".part")
        with gzip.open(tmp, "wt", encoding="utf-8", newline="\n") as fh:
            fh.write(f"# danbooru tags, fetched {date.today().isoformat()}, "
                     f"{len(tags)} tags\n")
            fh.write("# name\tcategory\tpost_count\taliases\n")
            for t in tags:
                names = " ".join(sorted(aliases.get(t["name"], ())))
                fh.write(f"{t['name']}\t{t['category']}\t{t['count']}\t{names}\n")
        tmp.replace(out)
        self.stdout.write(f"wrote {out} ({out.stat().st_size / 1e6:.2f} MB)")

    # ------------------------------------------------------------------ pages

    def _get(self, client: httpx.Client, path: str, params: dict) -> list[dict]:
        """One page, retried a few times. A failure here is worth stopping for."""
        last = ""
        for attempt in range(4):
            try:
                resp = client.get(API + path, params=params)
            except httpx.HTTPError as exc:
                last = str(exc)[:200]
            else:
                if resp.status_code == 200:
                    got = resp.json()
                    return got if isinstance(got, list) else []
                last = f"HTTP {resp.status_code} {resp.text[:200]}"
            self.stderr.write(f"  {path} {params.get('page')}: {last}")
            time.sleep(3 * (attempt + 1))
        raise SystemExit(f"gave up on {path} page {params.get('page')}: {last}")

    def _tags(self, client: httpx.Client, floor: int) -> list[dict]:
        """
        Every tag worth keeping, best known first.

        Paged in post-count order, which is also the order the file wants, so
        nothing has to be sorted afterwards. The walk stops at the loosest of the
        per-category minimums — past that point no category keeps anything.
        """
        kept: list[dict] = []
        for page in range(1, 1001):
            rows = self._get(
                client,
                "/tags.json",
                {
                    "limit": PER_PAGE,
                    "page": page,
                    "search[order]": "count",
                    "search[post_count]": f">={floor}",
                },
            )
            if not rows:
                break
            for row in rows:
                count = int(row.get("post_count") or 0)
                category = int(row.get("category") or 0)
                if row.get("is_deprecated"):
                    continue
                if count < MIN_POSTS.get(category, MIN_POSTS_OTHER):
                    continue
                kept.append(
                    {"name": row["name"], "category": category, "count": count}
                )
            if page % 20 == 0:
                self.stdout.write(
                    f"  page {page}: {len(kept)} kept, down to {rows[-1]['post_count']}"
                )
            time.sleep(PAUSE)
        return kept

    def _aliases(self, client: httpx.Client, names: set[str]) -> dict[str, list[str]]:
        """
        Active aliases that land on a tag we kept, grouped by that tag.

        An alias is how the model's own phrasing finds the canonical tag — it
        asks for "paper umbrella" and the site calls it `oil-paper_umbrella`.
        Aliases pointing at tags that did not survive the cut are dropped: there
        is nothing in the file for them to resolve to.
        """
        out: dict[str, list[str]] = {}
        for page in range(1, 1001):
            rows = self._get(
                client,
                "/tag_aliases.json",
                {"limit": PER_PAGE, "page": page, "search[status]": "active"},
            )
            if not rows:
                break
            for row in rows:
                target, alias = row.get("consequent_name"), row.get("antecedent_name")
                if not target or not alias or target not in names:
                    continue
                if alias == target or any(ch.isspace() for ch in alias):
                    continue
                out.setdefault(target, []).append(alias)
            time.sleep(PAUSE)
        return out
