"""
The compressor: before every paint the assembled prompt is counted
(novelai.prompt_tokens, the T5 tokenizer), and only when NovelAI would not
read it whole is it handed, with what the drawing model was asked, to the
compressor (prompts/compressor.txt, the drawing model's settings); its answer
is what is stored and painted. A prompt within the limit is painted as
assembled and the compressor is never called.
"""

import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase, override_settings

from core import i18n, prompts, ui_text
from core.models import Message
from core.prompts import wording as w
from core.services import assemble, llm, novelai, pipeline
from core.services.providers.fake import FakeProvider
from tests.tests import TEST_CREDS, _png_bytes, _sync_jobs

TAGS = ["red scarf", "blue ribbon", "green hat", "yellow umbrella", "black lantern", "white book", "purple cup",
        "orange flower", "pink bird", "brown kite", "grey balloon", "silver bottle", "gold candle", "teal feather",
        "navy pebble", "beige leaf", "crimson shell", "violet coin", "amber bead", "ivory thread"]
QUALITY = ", very aesthetic, masterpiece, no text"
#: one exposed entry, as the drawing model was shown it
ENTRY = {"id": "x", "kind": "character", "key": "林", "layer": "conversation", "shown_key": "林",
         "note": "银发", "positive": ["1girl", "silver hair"], "negative": [], "prose": "", "origin_seq": None}
#: what the drawing model was asked, as llm.draw hands it on to the compressor
ASKED = llm.format_draw("林站在门口", [ENTRY], size="portrait")


def _raw(msg: Message) -> dict:
    return msg.model_raw or {}


def _prompts(tags: list[str], character: str | None = "silver hair, blue eyes", negative: str = "lowres",
             char_negative: str = "") -> dict:
    """A finished prompt dict as the pipeline holds one: the words, plus
    what rides along (count, size, the character's key and centre, the id)."""
    return {
        "n_images": 1, "size": "portrait", "draw_id": "d-1",
        "base_prompt": "1girl, solo, " + ", ".join(tags) + QUALITY,
        "character_prompts": ([{"key": "林", "prompt": character, "negative": char_negative,
                                "center_x": 0.3, "center_y": 0.6}] if character else []),
        "negative_prompt": negative,
    }


def _answer(tags: list[str], character: str | None = "silver hair, blue eyes", negative: str = "lowres",
            char_negative: str = "") -> dict:
    """The compressor's answer: the words only, characters by name in order."""
    return {
        "base_prompt": "1girl, solo, " + ", ".join(tags) + QUALITY,
        "character_prompts": ([{"name": "林", "prompt": character, "negative": char_negative}]
                              if character is not None else []),
        "negative_prompt": negative,
    }


def _drawn(nai_prompts: dict) -> dict:
    """What llm.draw returns for a picture: the prompt and its record."""
    return {"nai_prompts": nai_prompts, "assembly": [], "answer": assemble.clean_answer({}), "asked": ASKED,
            "reasoning": "", "lookups": [], "usage": {}}


def _compressed(shorter: dict, before: dict, **more) -> dict:
    """What llm.compress returns when it turned `before` into `shorter`."""
    after = novelai.prompt_tokens(shorter)
    over = novelai.over_limit(after)
    return {"nai_prompts": shorter,
            "before": {k: before.get(k) for k in ("base_prompt", "character_prompts", "negative_prompt")},
            "counts": {"before": novelai.prompt_tokens(before), "after": after},
            "changed": not llm.same_words(shorter, before), "fits": not over,
            "tokens": max((n for _, n in over), default=after["positive"]),
            "reasoning": "", "usage": {}, **more}


#: the lead's answer for a turn that draws one picture and writes nothing
LEAD = {"reply": "构图我选了俯视角。", "settings": [], "characters": [], "edits": None,
        "draw": {"description": "一个银发少女站在花园里", "expose": [], "inherits": "", "text": "", "size": "portrait"},
        "raw_answer": None, "reasoning": "", "lookups": [], "usage": {}}


class TokenCountTests(SimpleTestCase):
    def test_the_count_is_the_t5_tokenizers(self):
        self.assertEqual(novelai.count_tokens("1girl, solo, red scarf"), 7)
        self.assertEqual(novelai.count_tokens(""), 0)

    def test_what_is_over_depends_on_the_rule(self):
        counts = novelai.prompt_tokens({
            "base_prompt": "1girl, solo, red scarf",
            "character_prompts": [{"key": "林", "prompt": "silver hair, blue eyes"}],
            "negative_prompt": "lowres",
        })
        self.assertEqual(counts["base"], 7)
        self.assertEqual(counts["characters"][0]["tokens"], 5)
        self.assertEqual(counts["negative"], 3)
        self.assertEqual(counts["positive"], 12)
        with override_settings(NAI_PROMPT_TOKEN_LIMIT=10, NAI_PROMPT_TOKEN_RULE="total"):
            # base and character together are over ten; the negative is not
            self.assertEqual([name for name, _ in novelai.over_limit(counts)], ["positive"])
            self.assertEqual(novelai.excess(counts), 2)
        with override_settings(NAI_PROMPT_TOKEN_LIMIT=10, NAI_PROMPT_TOKEN_RULE="each"):
            self.assertEqual(novelai.over_limit(counts), [])
        with override_settings(NAI_PROMPT_TOKEN_LIMIT=4, NAI_PROMPT_TOKEN_RULE="each"):
            self.assertEqual([name for name, _ in novelai.over_limit(counts)], ["base", "character 林"])

    def test_the_compressors_prompt_is_its_own_and_names_no_other_model(self):
        text = prompts.compressor()
        self.assertTrue(text.strip())
        self.assertNotEqual(text, prompts.drawing())
        for word in ("主 AI", "绘图 AI", "表征表", "格子"):
            self.assertNotIn(word, text)


class CompressTests(TestCase):
    def setUp(self):
        self.enterContext(i18n.use("zh"))

    def test_the_answer_replaces_the_words_and_keeps_what_rides_along(self):
        handed = _prompts(TAGS, char_negative="child, long hair")
        answer = _answer(TAGS[:3], "silver hair", "lowres, bad hands", char_negative="child")
        with override_settings(NAI_PROMPT_TOKEN_LIMIT=30), \
                mock.patch.object(llm, "_ask", return_value=(answer, {"_reasoning": "cut", "_lookups": [], "in": 1})) as ask:
            out = llm.compress(ASKED, handed)
        ask.assert_called_once()
        self.assertEqual(ask.call_args.args[0], prompts.compressor())
        self.assertIs(ask.call_args.args[2], llm.COMPRESS_SCHEMA)
        self.assertEqual(ask.call_args.kwargs["rounds"], 0)
        got = out["nai_prompts"]
        self.assertEqual(got["base_prompt"], answer["base_prompt"])
        self.assertEqual(got["character_prompts"],
                         [{"key": "林", "prompt": "silver hair", "negative": "child", "center_x": 0.3, "center_y": 0.6}])
        self.assertEqual(got["negative_prompt"], "lowres, bad hands")
        self.assertEqual((got["n_images"], got["size"], got["draw_id"]), (1, "portrait", "d-1"))
        self.assertTrue(out["changed"])
        self.assertTrue(out["fits"])
        self.assertEqual(out["before"]["base_prompt"], handed["base_prompt"])
        self.assertEqual(out["before"]["character_prompts"][0]["negative"], "child, long hair")
        self.assertGreater(out["counts"]["before"]["positive"], 30)
        self.assertLessEqual(out["counts"]["after"]["positive"], 30)
        self.assertEqual(out["reasoning"], "cut")
        self.assertEqual(out["usage"], {"in": 1})

    def test_an_answer_that_changes_nothing_says_so_and_one_still_over_says_that(self):
        handed = _prompts(TAGS)
        with override_settings(NAI_PROMPT_TOKEN_LIMIT=30), \
                mock.patch.object(llm, "_ask", return_value=(_answer(TAGS), {})):
            out = llm.compress(ASKED, handed)
        self.assertFalse(out["changed"])
        self.assertFalse(out["fits"])
        self.assertGreater(out["tokens"], 30)
        self.assertEqual(out["nai_prompts"], handed)

    def test_a_wrong_character_count_or_no_base_is_a_model_error_and_an_emptied_character_keeps_its_words(self):
        handed = _prompts(TAGS[:3], char_negative="child")
        with mock.patch.object(llm, "_ask", return_value=(_answer(TAGS[:3], None), {})), \
                self.assertRaises(llm.ModelError):
            llm.compress(ASKED, handed)
        with mock.patch.object(llm, "_ask", return_value=({**_answer(TAGS[:3]), "base_prompt": " "}, {})), \
                self.assertRaises(llm.ModelError):
            llm.compress(ASKED, handed)
        with mock.patch.object(llm, "_ask", return_value=(_answer(TAGS[:3], "", char_negative=""), {})):
            out = llm.compress(ASKED, handed)
        self.assertEqual(out["nai_prompts"]["character_prompts"][0]["prompt"], "silver hair, blue eyes")
        # a negative it emptied is a negative it dropped, which it may
        self.assertEqual(out["nai_prompts"]["character_prompts"][0]["negative"], "")
        self.assertTrue(out["changed"])

    def test_the_message_is_what_the_drawing_model_read_the_counts_and_the_prompt(self):
        handed = _prompts(TAGS[:3], char_negative="child")
        with override_settings(NAI_PROMPT_TOKEN_LIMIT=100, NAI_PROMPT_TOKEN_RULE="total"):
            text = llm.format_compress(ASKED, handed, novelai.prompt_tokens(handed))
        self.assertTrue(text.startswith(ASKED))
        self.assertIn("100", text)
        self.assertIn(w.REVIEW_LIMIT_FITS, text)
        self.assertIn(w.REVIEW_RULE["total"], text)
        self.assertIn(w.REVIEW_COUNT_CHAR.format(key="林", n=5), text)
        self.assertIn(w.REVIEW_PROMPT_BASE.format(text=handed["base_prompt"]), text)
        self.assertIn(w.REVIEW_PROMPT_CHAR.format(key="林", text="silver hair, blue eyes"), text)
        self.assertIn(w.REVIEW_PROMPT_CHAR_NEGATIVE.format(key="林", text="child"), text)
        self.assertIn(w.REVIEW_PROMPT_NEGATIVE.format(text="lowres"), text)
        self.assertEqual(text.count("【"), ASKED.count("【") + 2)
        self.assertLess(text.index(w.DRAW_ENTRIES_HEADING), text.index(w.REVIEW_LIMIT_HEADING))
        self.assertLess(text.index(w.REVIEW_LIMIT_HEADING), text.index(w.REVIEW_PROMPTS_HEADING))
        # a subject with no negative of its own gets no line for one
        without = llm.format_compress(ASKED, _prompts(TAGS[:3]), novelai.prompt_tokens(handed))
        self.assertNotIn(w.REVIEW_PROMPT_CHAR_NEGATIVE.split("{key}")[1].split("{text}")[0], without)

    def test_an_over_long_prompt_is_told_how_far_over(self):
        handed = _prompts(TAGS)
        with override_settings(NAI_PROMPT_TOKEN_LIMIT=20):
            counts = novelai.prompt_tokens(handed)
            text = llm.format_compress(ASKED, handed, counts)
        self.assertIn(w.REVIEW_LIMIT_OVER.format(over=counts["positive"] - 20), text)


@override_settings(**TEST_CREDS, NAI_PROMPT_TOKEN_LIMIT=12, NAI_PROMPT_TOKEN_RULE="total")
class CompressInTheTurnTests(TestCase):
    def setUp(self):
        self.c = Client()
        self.c.post("/api/auth/login", json.dumps({"username": "tester", "password": "test-password-123"}),
                    content_type="application/json")
        self.cid = self.c.post("/api/conversations", json.dumps({"title": "t"}),
                               content_type="application/json").json()["id"]

    def _turn(self, drawn: dict, compressed, text: str = "画一张", *, lead: dict = LEAD,
              ) -> tuple[mock.MagicMock, mock.MagicMock]:
        patches = [
            mock.patch.object(pipeline.llm, "lead_turn", return_value=lead),
            mock.patch.object(pipeline.llm, "draw", return_value=drawn),
            mock.patch.object(pipeline.llm, "make_title", return_value=""),
            mock.patch.object(pipeline.novelai, "generate_one", return_value=(_png_bytes(), 5)),
            mock.patch.object(pipeline.llm, "compress", side_effect=compressed)
            if callable(compressed) or isinstance(compressed, BaseException)
            else mock.patch.object(pipeline.llm, "compress", return_value=compressed),
        ]
        with _sync_jobs():
            started = [p.start() for p in patches]
            try:
                self.c.post(f"/api/conversations/{self.cid}/messages", {"text": text})
            finally:
                for p in patches:
                    p.stop()
        return started[3], started[4]

    def _msg(self) -> Message:
        return Message.objects.get(conversation_id=self.cid, role="assistant")

    def test_the_compressed_prompt_is_what_is_stored_and_painted(self):
        long = _prompts(TAGS, character=None)
        short = {**long, "base_prompt": "1girl, solo, red scarf"}
        compressed = _compressed(short, long, reasoning="cut the colours")
        gen, comp = self._turn(_drawn(long), compressed, text="一个戴红围巾的女孩")
        comp.assert_called_once()
        # the compressor is handed what the drawing model was asked, and the assembled prompt
        self.assertEqual(comp.call_args.args[0], ASKED)
        self.assertEqual(comp.call_args.args[1]["base_prompt"], long["base_prompt"])
        self.assertEqual(gen.call_args.args[0], "1girl, solo, red scarf")
        msg = self._msg()
        self.assertEqual(msg.status, "done", msg.error)
        self.assertEqual((msg.nai_prompts or {})["base_prompt"], "1girl, solo, red scarf")
        raw = _raw(msg)
        self.assertTrue(raw["compress"]["changed"])
        self.assertTrue(raw["compress"]["fits"])
        self.assertEqual(raw["compress"]["reasoning"], "cut the colours")
        self.assertEqual(raw["compress"]["before"]["base_prompt"], long["base_prompt"])
        self.assertNotIn("draw_notes", raw)
        shown = self.c.get(f"/api/messages/{msg.id}/raw").json()
        self.assertEqual(shown["compress"]["counts"]["after"]["positive"], 7)
        self.assertGreater(shown["compress"]["counts"]["before"]["positive"], 12)

    def test_the_turn_says_it_is_compressing_while_it_does(self):
        long = _prompts(TAGS, character=None)
        phases: list[str] = []

        def compress(asked, nai_prompts, **kw):
            phases.append(self._msg().phase)
            return _compressed({**long, "base_prompt": "1girl, solo"}, long)

        self._turn(_drawn(long), compress)
        self.assertEqual(phases, ["compressing"])
        self.assertEqual(self._msg().phase, "")

    def test_a_prompt_still_over_the_limit_is_painted_with_a_note(self):
        long = _prompts(TAGS, character=None)
        still_long = {**long, "base_prompt": "1girl, solo, " + ", ".join(TAGS[:10])}
        compressed = _compressed(still_long, long)
        gen, _ = self._turn(_drawn(long), compressed)
        self.assertEqual(gen.call_args.args[0], still_long["base_prompt"])
        msg = self._msg()
        self.assertFalse(_raw(msg)["compress"]["fits"])
        with i18n.use("zh"):
            self.assertIn(ui_text.PROMPT_OVER_LIMIT.format(tokens=compressed["tokens"], limit=12),
                          _raw(msg)["draw_notes"])

    def test_a_prompt_within_the_limit_is_painted_as_assembled_and_the_compressor_is_never_called(self):
        short = _prompts(TAGS[:2], character=None)
        with override_settings(NAI_PROMPT_TOKEN_LIMIT=1000):
            gen, comp = self._turn(_drawn(short), None)
        comp.assert_not_called()
        self.assertEqual(gen.call_args.args[0], short["base_prompt"])
        msg = self._msg()
        self.assertEqual(msg.status, "done", msg.error)
        self.assertNotIn("compress", _raw(msg))
        self.assertNotIn("draw_notes", _raw(msg))
        self.assertIsNone(self.c.get(f"/api/messages/{msg.id}/raw").json()["compress"])

    def test_a_compressor_that_fails_fails_the_turn_the_ordinary_way(self):
        long = _prompts(TAGS, character=None)
        gen, comp = self._turn(_drawn(long), llm.ModelError("compressor returned 0 character prompts for 1"))
        self.assertGreater(comp.call_count, 1, "a free retry or two, like every step")
        gen.assert_not_called()
        msg = self._msg()
        self.assertEqual(msg.status, "error")
        self.assertIn("compressor", msg.error)


@override_settings(LLM_PROVIDER="fake", FAKE_NOVELAI=True, APP_USERNAME="w", APP_PASSWORD="pw")
class CompressOnTheStandInTests(TestCase):
    """The stand-in provider answers the compressor with the prompt it was
    handed, so a browser suite that goes over the limit paints the stub's
    words; and the pipeline only asks it when the prompt is over."""

    def setUp(self):
        self.enterContext(i18n.use("zh"))
        self.enterContext(mock.patch.dict("os.environ", {"FAKE_LATENCY_MS": "0", "FAKE_NOVELAI": "1"}))
        get_user_model().objects.create_user(username="w", password="pw")
        self.c = Client()
        self.c.force_login(get_user_model().objects.get(username="w"))

    def test_the_stand_in_hands_the_prompt_back_as_it_came_subject_negatives_included(self):
        handed = _prompts(TAGS[:3], char_negative="child")
        message = llm.format_compress(ASKED, handed, novelai.prompt_tokens(handed))
        got = FakeProvider().complete(system="", messages=[{"role": "user", "content": message}],
                                      schema=llm.COMPRESS_SCHEMA, timeout=1).parsed
        self.assertEqual(got, {"base_prompt": handed["base_prompt"],
                               "character_prompts": [{"name": "林", "prompt": "silver hair, blue eyes", "negative": "child"}],
                               "negative_prompt": "lowres"})
        out = llm.compress(ASKED, handed)
        self.assertFalse(out["changed"])
        self.assertEqual(out["nai_prompts"]["character_prompts"][0]["negative"], "child")

    def _turn(self, text: str) -> Message:
        conv = self.c.post("/api/conversations", json.dumps({"title": "t"}), content_type="application/json").json()
        with _sync_jobs(), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            r = self.c.post(f"/api/conversations/{conv['id']}/messages",
                            {"text": text, "n_images": "1", "reflection_rounds": "0"})
        self.assertEqual(r.status_code, 201, r.content[:200])
        msg = Message.objects.get(conversation_id=conv["id"], role="assistant")
        self.assertEqual(msg.status, "done", msg.error)
        return msg

    def test_the_pipeline_compresses_only_a_prompt_that_is_over(self):
        with override_settings(NAI_PROMPT_TOKEN_LIMIT=5):
            over = self._turn("画一张")
        raw = _raw(over)
        self.assertIn("compress", raw)
        self.assertFalse(raw["compress"]["changed"])
        self.assertFalse(raw["compress"]["fits"])
        self.assertGreater(raw["compress"]["counts"]["before"]["positive"], 5)
        self.assertTrue(any(ui_text.PROMPT_OVER_LIMIT.split("{")[0] in n for n in raw["draw_notes"]), raw["draw_notes"])
        self.assertEqual(over.attachments.filter(kind="generated").count(), 1, "painted all the same")
        fits = self._turn("画一张")
        self.assertNotIn("compress", _raw(fits))
        self.assertNotIn("draw_notes", _raw(fits))
