"""
The drawing model looking a tag up before it writes one.

Three separable things, tested separately, because they fail separately:

  * the tag list and the search over it (core/services/danbooru.py) — does a
    query find what it should, in the order it should, and does it say so when
    it finds nothing;
  * the loop that lets a model call a tool at all (providers/base.py) — does it
    run what was asked, feed the answer back, stop when it is out of rounds or
    out of time, and survive a tool that throws;
  * the wiring (llm.py, the providers, the pipeline, the inspect endpoint) —
    does the drawing call get the tool, does nothing else, and does what the
    model looked up end up somewhere a person can read it.
"""

from __future__ import annotations

import gzip
import json
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from core.models import Conversation, Message
from core.prompts import wording
from core.services import danbooru, llm, providers
from core.services.providers import base

#: name, category, post count, aliases — the shape of one line of the real file.
FIXTURE = [
    ("sitting", 0, 1_000_000, ""),
    ("chair", 0, 500_000, ""),
    ("on_chair", 0, 40_000, "sitting_on_chair"),
    ("on_floor", 0, 40_000, "sitting_on_floor"),
    ("black_cat", 0, 400_000, ""),
    ("cat", 0, 300_000, ""),
    ("cathedral", 0, 5_000, ""),
    ("umbrella", 0, 120_000, ""),
    ("oil-paper_umbrella", 0, 19_000, "paper_umbrella karakasa"),
    ("cherry_blossoms", 0, 73_000, ""),
    ("hair", 0, 900_000, ""),
    ("red_hair", 0, 500_000, "crimson_hair"),
    ("long_hair", 0, 800_000, ""),
    ("bed", 0, 200_000, ""),
    ("girl", 0, 100, ""),
    ("hatsune_miku", 4, 147_000, "miku"),
    ("wlop", 1, 400, ""),
    # a real tag that starts the way a comment does; the site has two
    ("#compass", 3, 463, ""),
]


def write_index(rows=FIXTURE) -> Path:
    """A tag file of our own, in the real format, header line and all."""
    path = Path(tempfile.mkdtemp()) / "tags.tsv.gz"
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as fh:
        fh.write("# a test index\n")
        fh.write("# name\tcategory\tpost_count\taliases\n")
        for name, category, count, aliases in rows:
            fh.write(f"{name}\t{category}\t{count}\t{aliases}\n")
    return path


class SmallIndexTestCase(TestCase):
    """Everything below searches the fixture above, not the shipped file."""

    rows = FIXTURE

    def setUp(self):
        path = write_index(self.rows)
        patch = mock.patch.object(danbooru, "DATA_FILE", path)
        patch.start()
        self.addCleanup(patch.stop)
        danbooru.index.cache_clear()
        self.addCleanup(danbooru.index.cache_clear)

    @staticmethod
    def names(query, **kw) -> list[str]:
        return [h.name for h in danbooru.search(query, **kw)]


class WhatASearchFindsTests(SmallIndexTestCase):
    def test_the_header_lines_are_not_tags(self):
        self.assertEqual(len(danbooru.index()), len(FIXTURE))
        self.assertEqual(self.names("a test index"), [])

    def test_a_tag_that_begins_like_a_comment_is_still_a_tag(self):
        """
        `#compass` and `#unicus_(idolmaster)` are real tags, and a comment rule
        of "starts with #" ate both of them out of the shipped file.
        """
        self.assertEqual(self.names("#compass"), ["#compass"])

    def test_the_tag_asked_for_beats_a_bigger_tag_that_merely_contains_it(self):
        """
        Grade before size. `black_cat` has a hundred thousand more pictures than
        `cat`, so sorting by post count alone answers "cat" with `black_cat` —
        which is a different thing in the picture.
        """
        self.assertEqual(self.names("cat")[0], "cat")
        hits = self.names("cat")
        self.assertLess(hits.index("cat"), hits.index("black_cat"))
        self.assertIn("cathedral", hits, "a substring match still belongs, later")
        self.assertLess(hits.index("black_cat"), hits.index("cathedral"))

    def test_an_alias_finds_the_tag_it_points_at(self):
        self.assertEqual(self.names("miku")[0], "hatsune_miku")
        self.assertEqual(self.names("crimson hair")[0], "red_hair")

    def test_underscores_and_spaces_are_the_same_question(self):
        self.assertEqual(self.names("cherry blossoms"), self.names("cherry_blossoms"))
        self.assertEqual(self.names("CHERRY Blossoms")[0], "cherry_blossoms")

    def test_the_model_s_own_phrasing_resolves_to_the_site_s_name_for_it(self):
        """What this is for: it asks for "paper umbrella" and the site calls it
        something else entirely."""
        self.assertEqual(self.names("paper umbrella")[0], "oil-paper_umbrella")

    def test_a_tag_holding_every_word_of_the_query_comes_first(self):
        hits = self.names("long hair")
        self.assertEqual(hits[0], "long_hair")
        self.assertLess(hits.index("long_hair"), hits.index("hair"))

    def test_a_tag_holding_only_some_of_the_words_is_marked_as_partial(self):
        hits = danbooru.search("paper umbrella")
        self.assertFalse(hits[0].partial, "oil-paper_umbrella answers the question")
        partial = [h.name for h in hits if h.partial]
        self.assertIn("umbrella", partial)

    def test_a_single_word_query_is_never_a_partial_match(self):
        """
        There is no part of one word to match. Said out loud because the grade
        exists to flag "I answered something narrower than you asked", and one
        word cannot be narrowed.
        """
        self.assertEqual([h for h in danbooru.search("hair") if h.partial], [])

    def test_glue_words_do_not_drag_in_every_tag_that_contains_them(self):
        """
        "on" sits inside `on_chair`, `on_floor` and two hundred others on the
        real site, and they are not what "girl on bed" is asking about.
        """
        self.assertEqual(set(self.names("girl on bed")), {"bed", "girl"})
        # and the same words, when they really are what is being asked about:
        self.assertIn("on_chair", self.names("sitting on chair"))

    def test_the_tag_that_matched_more_of_the_query_outranks_the_bigger_one(self):
        hits = self.names("sitting on chair")
        self.assertEqual(hits[0], "on_chair", "it is an alias of sitting_on_chair")
        self.assertLess(hits.index("chair"), hits.index("on_floor"))

    def test_nothing_found_is_an_answer(self):
        self.assertEqual(self.names("absolutely no such thing"), [])
        said = danbooru.answer({"queries": ["absolutely no such thing"]})
        self.assertIn(wording.TOOL_TAGS_NONE, said)

    def test_the_cap_takes_the_tail(self):
        self.assertEqual(self.names("hair", limit=2), self.names("hair")[:2])
        self.assertEqual(len(self.names("hair", limit=2)), 2)


class WhatTheModelReadsBackTests(SmallIndexTestCase):
    def test_every_query_gets_its_own_heading_whether_or_not_it_matched(self):
        said = danbooru.answer({"queries": ["cat", "no such tag at all"]})
        self.assertIn(wording.TOOL_TAGS_QUERY.format(query="cat"), said)
        self.assertIn(wording.TOOL_TAGS_QUERY.format(query="no such tag at all"), said)
        self.assertIn(wording.TOOL_TAGS_COLUMNS, said)

    def test_a_tag_arrives_with_its_size_and_its_kind(self):
        said = danbooru.answer({"queries": ["miku"]})
        self.assertIn("hatsune_miku\t147000\tcharacter", said)
        self.assertIn("wlop\t400\tartist", danbooru.answer({"queries": ["wlop"]}))

    def test_partial_matches_are_fenced_off_under_a_line_that_says_so(self):
        said = danbooru.answer({"queries": ["paper umbrella"]})
        fence = said.index(wording.TOOL_TAGS_PARTIAL)
        self.assertLess(said.index("oil-paper_umbrella"), fence)
        self.assertLess(fence, said.index("umbrella\t120000"))

    def test_an_answer_with_no_partial_matches_has_no_fence(self):
        self.assertNotIn(wording.TOOL_TAGS_PARTIAL, danbooru.answer({"queries": ["cat"]}))

    def test_more_queries_than_one_call_carries_are_said_to_be_left_out(self):
        said = danbooru.answer({"queries": ["cat"] * (danbooru.MAX_QUERIES + 3)})
        self.assertIn(wording.TOOL_TAGS_TRIMMED.format(n=danbooru.MAX_QUERIES), said)
        self.assertEqual(
            said.count(wording.TOOL_TAGS_QUERY.format(query="cat")),
            danbooru.MAX_QUERIES,
        )

    def test_a_call_with_nothing_to_look_up_is_answered_rather_than_raising(self):
        for args in ({}, {"queries": []}, {"queries": ["", "  "]}, {"queries": None}):
            self.assertEqual(danbooru.answer(args), wording.TOOL_TAGS_NO_QUERIES)

    def test_one_word_sent_unwrapped_is_still_answered(self):
        self.assertIn("cat\t300000", danbooru.answer({"queries": "cat"}))

class CheckingAFinishedPromptTests(SmallIndexTestCase):
    """
    Every tag that actually went out, looked up. Reported, never enforced: the
    index is trimmed to tags with enough pictures behind them and is weeks old
    besides, so a word missing from it is a reason to look, not to refuse.
    """

    def test_it_names_the_tags_the_site_does_not_have(self):
        self.assertEqual(danbooru.unknown(["cat", "no such tag", "miku"]),
                         ["no such tag"])

    def test_an_alias_counts_as_known(self):
        """`paper umbrella` is not missing just because the site files it
        under `oil-paper_umbrella`."""
        self.assertEqual(danbooru.unknown(["paper umbrella"]), [])

    def test_spelling_does_not_matter(self):
        self.assertEqual(danbooru.unknown(["Cherry Blossoms", "cherry_blossoms"]), [])

    def test_the_same_word_twice_is_reported_once(self):
        self.assertEqual(danbooru.unknown(["nope", "nope"]), ["nope"])

    def test_a_whole_prompt_is_walked_base_characters_and_negative(self):
        from core.services import llm

        found = llm.unknown_tags({
            "base_prompt": "cat, invented one, very aesthetic",
            "character_prompts": [{"prompt": "miku, invented two"}],
            "negative_prompt": "invented three",
        })
        self.assertEqual(found, ["invented one", "invented two", "invented three"])

    def test_novelais_own_words_are_not_reported(self):
        """Otherwise every single picture would come back with a complaint."""
        from core.services import llm

        self.assertEqual(
            llm.unknown_tags({"base_prompt": "cat, very aesthetic, masterpiece",
                              "negative_prompt": "worst quality, bad quality"}),
            [],
        )

    def test_a_row_with_no_prompts_says_nothing(self):
        from core.services import llm

        self.assertEqual(llm.unknown_tags(None), [])
        self.assertEqual(llm.unknown_tags({}), [])


class WithNoIndexNothingIsReportedTests(TestCase):
    """
    A checkout without the data file must not flag every tag in every picture.
    Silence is the right answer when there is nothing to check against.
    """

    def setUp(self):
        patch = mock.patch.object(
            danbooru, "DATA_FILE", Path(tempfile.mkdtemp()) / "nope.tsv.gz"
        )
        patch.start()
        self.addCleanup(patch.stop)
        danbooru.index.cache_clear()
        self.addCleanup(danbooru.index.cache_clear)

    def test_nothing_is_unknown_when_nothing_is_known(self):
        self.assertEqual(danbooru.unknown(["anything at all"]), [])


class NoTagListTests(TestCase):
    """
    A checkout or an image built before the file existed. The turn still draws.
    """

    def setUp(self):
        patch = mock.patch.object(
            danbooru, "DATA_FILE", Path(tempfile.mkdtemp()) / "nope.tsv.gz"
        )
        patch.start()
        self.addCleanup(patch.stop)
        danbooru.index.cache_clear()
        self.addCleanup(danbooru.index.cache_clear)

    def test_a_missing_file_is_an_empty_index_and_not_an_exception(self):
        self.assertEqual(len(danbooru.index()), 0)
        self.assertEqual(danbooru.search("cat"), [])

class TheBundledIndexTests(TestCase):
    """
    The file that actually ships. Everything above would pass with no file at
    all, and the tool is worth nothing without one.
    """

    def test_it_is_there_and_it_is_the_whole_site(self):
        self.assertTrue(danbooru.DATA_FILE.exists(), "run manage.py fetch_danbooru_tags")
        self.assertGreater(len(danbooru.index()), 50_000)

    def test_the_tags_every_prompt_starts_with_are_in_it(self):
        for tag in ("1girl", "solo", "looking_at_viewer", "cherry_blossoms"):
            self.assertEqual(danbooru.search(tag)[0].name, tag)

    def test_counts_and_categories_came_through(self):
        miku = danbooru.search("hatsune_miku")[0]
        self.assertEqual(miku.category, "character")
        self.assertGreater(miku.count, 1000)


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------


@override_settings(LLM_PROVIDER="deepseek", DEEPSEEK_API_KEY="k", OPENROUTER_API_KEY="")
class BothModelsLookTagsUpInTheAnswerTests(TestCase):
    """
    A lookup is an answer with the `lookup` field filled in. The app runs it,
    hands the result back as the next message, and asks again — the same
    shape, the same parser, no vendor tool mechanism. The last allowed round
    offers no such field, so a model that would rather keep asking has to
    answer.
    """

    def _calls(self, call, *answers):
        sent = []

        def fake_run(system, messages, schema, effort, on_retry=None, temperature=None):
            sent.append((json.loads(json.dumps(messages)), schema))
            return answers[min(len(sent) - 1, len(answers) - 1)], {"prompt_tokens": 1}

        with mock.patch.object(llm, "_run", side_effect=fake_run):
            out = call()
        return out, sent

    def test_the_drawing_call_may_ask_and_is_answered(self):
        out, sent = self._calls(
            lambda: llm.draw_turn({"composition": "一只猫", "size": "portrait"}),
            {"lookup": ["cat"]},
            {"scene": "cat"},
        )
        self.assertEqual(len(sent), 2)
        # the second request carries the question and the app's answer
        roles = [m["role"] for m in sent[1][0]]
        self.assertEqual(roles, ["user", "assistant", "user"])
        self.assertIn("cat", sent[1][0][-1]["content"])
        self.assertTrue(sent[1][0][-1]["content"].startswith(wording.LOOKUP_HEADING))
        # and the lookup comes back with the turn, which is how it reaches the row
        self.assertEqual(out["lookups"][0]["queries"], ["cat"])
        self.assertIn("cat", out["lookups"][0]["result"])

    def test_the_conversation_call_may_too(self):
        out, sent = self._calls(
            lambda: llm.lead_turn([{"role": "user", "content": "你好"}]),
            {"lookup": ["hair"]},
            {"reply": "你好", "draw": None, "edits": None},
        )
        self.assertEqual(len(sent), 2)
        self.assertEqual(out["lookups"][0]["queries"], ["hair"])

    def test_the_last_allowed_round_offers_no_lookup(self):
        with override_settings(DRAW_TOOL_ROUNDS=1):
            _, sent = self._calls(
                lambda: llm.draw_turn({"composition": "一只猫", "size": "portrait"}),
                {"lookup": ["cat"]},
                {"scene": "cat"},
            )
        self.assertIn("lookup", sent[0][1]["schema"]["properties"])
        self.assertNotIn("lookup", sent[1][1]["schema"]["properties"])
        # and the model is told so in the message that carries the answer
        self.assertIn(wording.LOOKUP_LAST, sent[1][0][-1]["content"])

    @override_settings(DRAW_TOOL_ROUNDS=0)
    def test_the_kill_switch_leaves_one_request_with_no_lookup(self):
        _, sent = self._calls(
            lambda: llm.draw_turn({"composition": "一只猫", "size": "portrait"}),
            {"scene": "cat"},
        )
        self.assertEqual(len(sent), 1)
        self.assertNotIn("lookup", sent[0][1]["schema"]["properties"])

    def test_an_answer_that_asks_for_nothing_is_the_answer(self):
        out, sent = self._calls(
            lambda: llm.draw_turn({"composition": "一只猫", "size": "portrait"}),
            {"lookup": [], "scene": "cat"},
        )
        self.assertEqual(len(sent), 1)
        self.assertEqual(out["lookups"], [])
        self.assertNotIn("lookup", out)

    def test_a_turn_that_looked_nothing_up_carries_an_empty_list(self):
        out, _ = self._calls(
            lambda: llm.draw_turn({"composition": "一只猫", "size": "portrait"}),
            {"scene": "cat"},
        )
        self.assertEqual(out["lookups"], [])


@override_settings(LLM_PROVIDER="fake")
class TheStandInLooksUpTooTests(TestCase):
    """
    The e2e stack runs on the fake provider, so if the stub never asks, the
    loop is never driven by a browser run at all. Asked to, it asks once, is
    answered from the real index, and then answers.
    """

    def test_a_drawing_call_on_the_stub_really_looks_up(self):
        with mock.patch.dict("os.environ", {"FAKE_LATENCY_MS": "0"}):
            out = llm.draw_turn({"composition": "查标签：一个人物站在窗前", "size": "portrait"})
        self.assertEqual(len(out["lookups"]), 1)
        self.assertIn("1girl", out["lookups"][0]["result"])
        # and the answer still arrives, which is the half a stub can get wrong
        self.assertTrue(out["base_prompt"])

    def test_the_stub_answers_at_once_when_not_asked_to_look(self):
        with mock.patch.dict("os.environ", {"FAKE_LATENCY_MS": "0"}):
            out = llm.draw_turn({"composition": "一个人物站在窗前", "size": "portrait"})
        self.assertEqual(out["lookups"], [])


class WhatGetsStoredTests(TestCase):
    """
    Where the lookups end up: beside the drawing model's working, never inside
    the prompts, because 重绘 replays those verbatim.
    """

    def test_the_prompts_are_the_prompt_fields_and_nothing_else(self):
        drawn = {
            "n_images": 1, "size": "portrait", "base_prompt": "cat",
            "character_prompts": [], "negative_prompt": "",
            "usage": {"prompt_tokens": 1}, "reasoning": "想了想",
            "lookups": [{"queries": ["cat"], "result": "x"}],
        }
        self.assertEqual(
            set(llm.prompts_from(drawn)),
            {"n_images", "size", "base_prompt", "character_prompts", "negative_prompt"},
        )

    def test_a_field_added_to_the_result_does_not_leak_into_the_prompts(self):
        """
        The list says what belongs rather than what does not, so the next field
        anyone adds to a draw result is excluded by default.
        """
        self.assertEqual(llm.prompts_from({"base_prompt": "cat", "whatever": 1}),
                         {"base_prompt": "cat"})


@override_settings(APP_USERNAME="yyy", APP_PASSWORD="pw")
class TheInspectDialogCanSeeThemTests(TestCase):
    """
    「检查」 answers "why these tags" with the model's working. "what did it
    check" is the same question from the other side, and it was on the row
    already — it just was not being handed over.
    """

    def setUp(self):
        get_user_model().objects.create_user(username="yyy", password="pw")
        self.c = Client()
        self.c.force_login(get_user_model().objects.get(username="yyy"))
        self.conv = Conversation.objects.create(title="c")

    def _raw(self, **model_raw):
        msg = Message.objects.create(
            conversation=self.conv, seq=1, role="assistant", status="done",
            content="好", kind="image", model_raw=model_raw or None,
        )
        return self.c.get(f"/api/messages/{msg.id}/raw").json()

    def test_what_was_looked_up_comes_back_with_the_turn(self):
        runs = [{"queries": ["cat"], "result": "cat\t300000\tgeneral"}]
        raw = self._raw(draw_lookups=runs, lookups=[{"queries": ["hair"], "result": "hair"}])
        self.assertEqual(raw["lookups"]["draw"], runs)
        self.assertEqual(raw["lookups"]["lead"][0]["queries"], ["hair"])

    def test_a_turn_that_looked_nothing_up_says_so_with_an_empty_list(self):
        raw = self._raw(draw_reasoning="想了想")
        self.assertEqual(raw["lookups"]["draw"], [])
        self.assertEqual(raw["lookups"]["lead"], [])
