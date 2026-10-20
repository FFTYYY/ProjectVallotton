"""
Chinese in the drawing model's answer goes to NovelAI as it is.

NovelAI V5 reads natural language, Chinese included. An answer with Chinese
in its tags or sentences used to fail the turn ("drawing model wrote prose
NovelAI cannot read as tags"), and for a day was sent back to be rewritten in
English. Neither: the app does not check the script.
"""
from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase

from core.services import assemble, llm


class ChineseGoesThroughTests(SimpleTestCase):
    def test_an_answer_in_chinese_is_used_as_it_is(self):
        answer = ({"positive": "1girl, 招牌", "prose": "书店门口挂着写有夜航的招牌。"}, {})
        with mock.patch.object(llm, "_run", return_value=answer) as run:
            out = llm.draw("书店门口的少女，招牌写着夜航", [])
        self.assertEqual(run.call_count, 1)
        base = out["nai_prompts"]["base_prompt"]
        self.assertIn("招牌", base)
        self.assertIn("书店门口挂着写有夜航的招牌。", base)
        self.assertEqual(assemble.lint(out["nai_prompts"]), [])


class ChineseSentencesTests(SimpleTestCase):
    """The prose is cut into sentences to dedupe them and to take out the ones
    a picture drops. Read as Latin letters only, a Chinese sentence was empty
    and vanished from the prompt; a Chinese stop was not a stop, and the next
    piece was glued on after a comma."""

    def test_a_chinese_sentence_is_kept_once(self):
        self.assertEqual(assemble.sentences(["The shop glows. 门口挂着灯笼。猫在睡觉。", "门口挂着灯笼。"]),
                         "The shop glows. 门口挂着灯笼。 猫在睡觉。")

    def test_a_chinese_stop_ends_a_piece_like_an_english_one(self):
        self.assertEqual(assemble.glue(["1girl, sign", "门口挂着灯笼。", "Text: 夜航"]),
                         "1girl, sign, 门口挂着灯笼。 Text: 夜航")

    def test_a_picture_can_drop_a_chinese_sentence_of_an_entry(self):
        shop = {"id": "1", "kind": "setting", "key": "书店", "layer": "conversation", "shown_key": "书店",
                "note": "", "positive": ["bookstore"], "negative": [], "prose": "The window glows. 门口挂着灯笼。",
                "origin_seq": None, "category": "场景"}
        answer = ({"entries": [{"key": "书店", "remove_prose": "门口挂着灯笼。"}], "positive": "1girl"}, {})
        with mock.patch.object(llm, "_run", return_value=answer):
            base = llm.draw("书店门口的少女", [shop])["nai_prompts"]["base_prompt"]
        self.assertIn("The window glows.", base)
        self.assertNotIn("灯笼", base)
