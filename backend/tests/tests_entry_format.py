"""
How the lead writes entries in an answer, as the app reads them.

A costume is written as an entry of its own among the characters, keyed
`character/costume` -- the key it has in the library listing and in
`expose` -- and the app files it under its character. Written that way, a
costume alone leaves its character as it is. And a key names one entry in
its layer whatever list it is written in: a setting written among the
characters is that setting. (Report 44, test 8: with costumes nested under
their character the lead left the costume out 6 times in 32 creation turns,
and once a reflection round wrote a change to 「人物比例」 among the
characters, which made a character of it.)
"""
from __future__ import annotations

import json

from django.test import SimpleTestCase, TestCase

from core.models import Character, CharacterVariant, Conversation, Folder, Message, Setting
from core.services import library, llm, pipeline


def _item(key, **kw):
    return {"key": key, "note": kw.get("note", ""), "positive": kw.get("positive", ""),
            "negative": kw.get("negative", ""), "prose": kw.get("prose", ""), **(
                {"category": kw["category"]} if "category" in kw else {})}


class CostumesWrittenAsEntriesTests(SimpleTestCase):
    def _characters(self, items):
        return llm._normalize_lead({"reply": "好", "characters": items})["characters"]

    def test_a_costume_goes_under_its_character_keyed_by_its_title(self):
        out = self._characters([_item("林晚", positive="1girl"), _item("林晚/日常装", positive="white shirt")])
        self.assertEqual([c["key"] for c in out], ["林晚"])
        self.assertEqual([v["key"] for v in out[0]["variants"]], ["日常装"])
        self.assertEqual(out[0]["variants"][0]["positive"], ["white shirt"])
        self.assertNotIn("carrier", out[0])

    def test_a_costume_written_before_its_character_still_joins_it(self):
        out = self._characters([_item("林晚/日常装", positive="white shirt"), _item("林晚", positive="1girl")])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["positive"], ["1girl"])
        self.assertNotIn("carrier", out[0])
        self.assertEqual([v["key"] for v in out[0]["variants"]], ["日常装"])

    def test_a_costume_alone_rides_on_a_carrier_that_does_not_write_the_character(self):
        out = self._characters([_item("林晚/运动装", positive="tank top")])
        self.assertEqual(out[0]["key"], "林晚")
        self.assertTrue(out[0]["carrier"])
        self.assertNotIn("positive", out[0])
        self.assertEqual([v["key"] for v in out[0]["variants"]], ["运动装"])

    def test_a_project_costume_keeps_the_prefix_on_its_carrier(self):
        out = llm._normalize_lead({"reply": "好", "edits": {"characters": [_item("@林晚/运动装")]}})["edits"]
        self.assertEqual(out["characters"][0]["key"], "@林晚")

    def test_costumes_nested_the_old_way_are_still_read(self):
        out = self._characters([{**_item("林晚"), "variants": [_item("日常装", positive="white shirt")]}])
        self.assertEqual([v["key"] for v in out[0]["variants"]], ["日常装"])

    def test_the_lead_is_shown_its_answers_the_way_it_writes_them(self):
        own = {"settings": [{"key": "画风", "category": "画风", "positive": ["film grain"]}],
               "characters": [{"key": "林晚", "positive": ["1girl"], "variants": [{"key": "日常装", "positive": ["white shirt"]}]},
                              {"key": "陈禾", "carrier": True, "variants": [{"key": "运动装", "positive": ["tank top"]}]}]}
        said = llm.as_said(own)
        assert said is not None
        self.assertEqual([c["key"] for c in said["characters"]], ["林晚", "林晚/日常装", "陈禾/运动装"])
        self.assertEqual(said["characters"][1]["positive"], "white shirt")
        self.assertTrue(all("variants" not in c and "carrier" not in c for c in said["characters"]))
        self.assertEqual(said["settings"][0]["positive"], "film grain")


class CostumeAloneLeavesItsCharacterTests(TestCase):
    def setUp(self):
        self.conv = Conversation.objects.create(title="c")
        library.write_conversation_entries(self.conv, 1, [], [
            {**_item("林晚", note="深棕卷发", positive="1girl, curly hair", prose="Her curls reach her waist."),
             "variants": [_item("日常装", positive="white shirt")]}])

    def _write(self, characters, seq=3):
        staged = library.Staged(seq=seq, in_project=False)
        staged.add(llm._normalize_lead({"reply": "", "characters": characters}), self.conv)
        _, chars = staged.conversation_lists()
        return library.write_conversation_entries(self.conv, seq, [], chars)

    def test_writing_only_a_costume_adds_it_and_keeps_the_character(self):
        self._write([_item("林晚/运动装", positive="tank top")])
        lin = Character.objects.get(conversation=self.conv, name="林晚")
        self.assertEqual(lin.positive, ["1girl", "curly hair"])
        self.assertEqual(lin.prose, "Her curls reach her waist.")
        self.assertEqual(lin.origin_seq, 1)
        self.assertEqual(sorted(lin.variants.values_list("title", flat=True)), ["日常装", "运动装"])

    def test_the_turn_draws_the_character_as_the_library_has_it(self):
        staged = library.Staged(seq=3, in_project=False)
        staged.add(llm._normalize_lead({"reply": "", "characters": [_item("林晚/运动装", positive="tank top")]}), self.conv)
        hit = staged.entries_for(self.conv, "林晚/运动装")
        assert hit is not None
        entries, missing = hit
        self.assertFalse(missing)
        self.assertEqual(entries[0]["positive"], ["1girl", "curly hair"])
        self.assertEqual(entries[1]["positive"], ["tank top"])

    def test_a_costume_for_a_character_nobody_has_is_sent_back(self):
        answer = llm._normalize_lead({"reply": "", "characters": [_item("周野/日常装", positive="hoodie")],
                                      "draw": {"description": "x", "expose": ["周野/日常装"]}})
        self.assertEqual(library.unwritten_exposure(self.conv, answer), ["周野/日常装"])


class CostumeForAProjectCharacterTests(TestCase):
    def test_the_conversation_gets_a_copy_of_the_character_to_hang_it_on(self):
        folder = Folder.objects.create(name="项目")
        conv = Conversation.objects.create(title="c", folder=folder)
        lin = Character.objects.create(folder=folder, name="林晚", description="深棕卷发", positive=["1girl", "curly hair"],
                                       position=1)
        daily = CharacterVariant.objects.create(character=lin, title="日常装", positive=["white shirt"], position=1)
        conv.selected_variants.add(daily)
        staged = library.Staged(seq=3, in_project=True)
        staged.add(llm._normalize_lead({"reply": "", "characters": [_item("林晚/运动装", positive="tank top")]}), conv)
        _, chars = staged.conversation_lists()
        library.write_conversation_entries(conv, 3, [], chars)
        own = Character.objects.get(conversation=conv, name="林晚")
        self.assertEqual(own.positive, ["1girl", "curly hair"])
        self.assertEqual(own.description, "深棕卷发")
        self.assertEqual(list(own.variants.values_list("title", flat=True)), ["运动装"])
        lin.refresh_from_db()
        self.assertEqual(list(lin.variants.values_list("title", flat=True)), ["日常装"])


class AKeyNamesOneEntryWhateverListTests(TestCase):
    def setUp(self):
        self.conv = Conversation.objects.create(title="c")
        library.write_conversation_entries(
            self.conv, 1, [_item("人物比例", category="人物比例", positive="young adult", negative="child")],
            [{**_item("林晚", positive="1girl"), "variants": [_item("日常装", positive="white shirt")]}])

    def _staged(self, answer):
        staged = library.Staged(seq=5, in_project=False)
        staged.add(llm._normalize_lead({"reply": "", **answer}), self.conv)
        return staged

    def test_a_setting_written_among_the_characters_is_that_setting(self):
        staged = self._staged({"characters": [_item("人物比例", positive="young adult", negative="child, bad hands")]})
        settings, chars = staged.conversation_lists()
        self.assertEqual(chars, [])
        self.assertEqual(settings[0]["key"], "人物比例")
        self.assertEqual(settings[0]["category"], "人物比例")
        library.write_conversation_entries(self.conv, 5, settings, chars)
        self.assertFalse(Character.objects.filter(conversation=self.conv, name="人物比例").exists())
        self.assertEqual(Setting.objects.get(conversation=self.conv, title="人物比例").negative, ["child", "bad hands"])

    def test_a_character_written_among_the_settings_is_that_character(self):
        staged = self._staged({"settings": [_item("林晚", positive="1girl, freckles", category="其他")]})
        settings, chars = staged.conversation_lists()
        self.assertEqual(settings, [])
        self.assertEqual(chars[0]["key"], "林晚")
        self.assertNotIn("category", chars[0])

    def test_a_new_key_stays_where_it_was_written(self):
        staged = self._staged({"characters": [_item("周野", positive="1boy")], "settings": [_item("海边", category="场景")]})
        settings, chars = staged.conversation_lists()
        self.assertEqual([s["key"] for s in settings], ["海边"])
        self.assertEqual([c["key"] for c in chars], ["周野"])

    def test_the_check_sees_the_setting_where_it_belongs(self):
        answer = llm._normalize_lead({"reply": "", "characters": [_item("人物比例", positive="young adult")],
                                      "draw": {"description": "x", "expose": ["人物比例", "林晚/日常装"]}})
        self.assertEqual(library.unwritten_exposure(self.conv, answer), [])


class HistoryShowsTheWayTheLeadWritesTests(TestCase):
    def test_an_earlier_answer_comes_back_with_each_costume_its_own_entry(self):
        conv = Conversation.objects.create(title="c")
        m = Message.objects.create(conversation=conv, role="assistant", seq=2, content="好", model_raw={
            "settings": [], "characters": [{"key": "林晚", "positive": ["1girl"],
                                            "variants": [{"key": "日常装", "positive": ["white shirt"]}]}]})
        said = json.loads(pipeline.assistant_answer(m))
        self.assertEqual([c["key"] for c in said["characters"]], ["林晚", "林晚/日常装"])
        self.assertEqual(said["characters"][1]["positive"], "white shirt")
