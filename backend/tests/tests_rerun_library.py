"""
重跑 takes back what the replaced turn wrote into the conversation's own
entries.

A turn records the layer as it found it (library_before) and as it left it
(library_snapshot). A rerun undoes the difference: what the turn made goes,
what it rewrote says again what it said, a switch it flipped flips back. What
the user changed by hand since -- the text of an entry, or its switch -- is
theirs and stays, so the turn asked again reads the library as it is now. The
project's entries are not touched: an edit to them went through a proposal.
"""
from __future__ import annotations

import json
from unittest import mock

from django.test import Client, TestCase, override_settings

from core.models import Character, Conversation, Message, Setting
from core.services import jobs, library

from .tests import TEST_CREDS, _login


def _item(key, **kw):
    return {"key": key, "note": kw.get("note", ""), "positive": kw.get("positive", ""),
            "negative": kw.get("negative", ""), "prose": kw.get("prose", "")}


class TakeBackTests(TestCase):
    def setUp(self):
        self.conv = Conversation.objects.create(title="c")
        # what the user had before the turn: a setting (off), a character
        # with its stand-in outfit (on)
        library.write_conversation_entries(self.conv, 1, [_item("天台", note="旧天台", positive="rooftop")], [
            _item("林晚", note="深棕卷发", positive="1girl, curly hair")])
        self.rooftop = Setting.objects.get(conversation=self.conv, title="天台")
        self.conv.selected_settings.remove(self.rooftop)
        self.lin = Character.objects.get(conversation=self.conv, name="林晚")
        self.stub = self.lin.variants.get()
        self.before = library.own_layer(self.conv)
        # the turn: rewrites 天台 (and so switches it on), makes 雨夜, gives
        # 林晚 a real outfit (the stand-in goes), makes 周野 with one
        library.write_conversation_entries(self.conv, 3, [
            _item("天台", note="新天台", positive="rooftop, rain"), _item("雨夜", note="下雨", positive="rain")], [
            {**_item("林晚"), "carrier": True, "variants": [_item("雨衣", positive="raincoat")]},
            {**_item("周野", note="男生", positive="1boy"), "variants": [_item("校服", positive="school uniform")]}])
        self.after = library.own_layer(self.conv)

    def _take_back(self):
        return library.take_back(self.conv, self.before, self.after)

    def test_an_untouched_turn_is_taken_back_whole(self):
        self._take_back()
        self.assertEqual(sorted(Setting.objects.filter(conversation=self.conv).values_list("title", flat=True)), ["天台"])
        self.rooftop.refresh_from_db()
        self.assertEqual((self.rooftop.content, self.rooftop.positive, self.rooftop.origin_seq), ("旧天台", ["rooftop"], 1))
        self.assertNotIn(self.rooftop, self.conv.selected_settings.all())
        self.assertEqual(list(Character.objects.filter(conversation=self.conv).values_list("name", flat=True)), ["林晚"])
        outfits = list(self.lin.variants.all())
        self.assertEqual([v.title for v in outfits], [self.stub.title])
        self.assertIn(outfits[0], self.conv.selected_variants.all())
        self.assertEqual(library.own_layer(self.conv)["settings"][0]["note"], self.before["settings"][0]["note"])

    def test_what_the_user_changed_since_stays(self):
        Setting.objects.filter(conversation=self.conv, title="雨夜").update(content="下雨，我改过")
        self.rooftop.refresh_from_db()
        self.rooftop.positive = ["rooftop", "rain", "night"]
        self.rooftop.save()
        zhou = Character.objects.get(conversation=self.conv, name="周野")
        zhou.variants.create(title="便服", position=9)
        self._take_back()
        self.assertEqual(Setting.objects.get(conversation=self.conv, title="雨夜").content, "下雨，我改过")
        self.rooftop.refresh_from_db()
        self.assertEqual((self.rooftop.content, self.rooftop.positive), ("新天台", ["rooftop", "rain", "night"]))
        # 周野 stays for the outfit added by hand; the turn's own outfit goes
        self.assertEqual(list(Character.objects.get(conversation=self.conv, name="周野")
                              .variants.values_list("title", flat=True)), ["便服"])

    def test_a_switch_the_user_flipped_since_stays(self):
        rain = Setting.objects.get(conversation=self.conv, title="雨夜")
        self.conv.selected_settings.remove(self.rooftop)  # off by hand after the turn switched it on
        rain.content = "下雨，我改过"
        rain.save()  # kept, so its switch can be looked at
        self.conv.selected_settings.remove(rain)
        self._take_back()
        self.assertNotIn(self.rooftop, self.conv.selected_settings.all())
        self.assertNotIn(rain, self.conv.selected_settings.all())

    def test_a_turn_that_wrote_nothing_changes_nothing(self):
        layer = library.own_layer(self.conv)
        self.assertEqual(library.take_back(self.conv, layer, layer), [])
        self.assertEqual(library.own_layer(self.conv), layer)


@override_settings(**TEST_CREDS)
class RerunTakesBackTests(TestCase):
    def setUp(self):
        self.c = Client()
        _login(self.c)

    def test_a_rerun_takes_back_the_replaced_turn_and_keeps_hand_edits(self):
        cid = self.c.post("/api/conversations", "{}", content_type="application/json").json()["id"]
        conv = Conversation.objects.get(id=cid)
        Message.objects.create(conversation=conv, seq=1, role="user", content="画", status="done")
        before = library.own_layer(conv)
        library.write_conversation_entries(conv, 2, [_item("雨夜", note="下雨"), _item("天台", note="天台")], [])
        Message.objects.create(conversation=conv, seq=2, role="assistant", content="好", status="done",
                               library_before=before, library_snapshot=library.own_layer(conv))
        Setting.objects.filter(conversation=conv, title="天台").update(content="天台，我改过")
        with mock.patch.object(jobs, "start_generation"):
            resp = self.c.post(f"/api/conversations/{cid}/rerun", json.dumps({}), content_type="application/json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(list(Setting.objects.filter(conversation=conv).values_list("title", "content")),
                         [("天台", "天台，我改过")])
