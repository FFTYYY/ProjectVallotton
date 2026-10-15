"""
The line writer: an entry edited by hand gets its tag lines rewritten from
its text, off the request, on the sweeper's clock; an entry from before lines
existed gets them the same way; and the browser can see which entries are
waiting. Driven on the stand-in provider, whose line for hair follows one
word of the text, so a sweep that wrote FROM the text is told apart from one
that did not.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from core.models import Character, CharacterVariant, Conversation, Folder, Message, Setting
from core.prompts import wording
from core.services import library, library_lines, proposals
from core.services.providers import fake


@override_settings(LLM_PROVIDER="fake", APP_USERNAME="w", APP_PASSWORD="pw")
class LineWriterTests(TestCase):
    def setUp(self):
        get_user_model().objects.create_user(username="w", password="pw")
        self.c = Client()
        self.c.force_login(get_user_model().objects.get(username="w"))
        self.env = mock.patch.dict("os.environ", {"FAKE_LATENCY_MS": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.folder = Folder.objects.create(name="项目")

    def _patch(self, path: str, body: dict):
        r = self.c.patch(path, json.dumps(body), content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content[:200])
        return r.json()

    # ------------------------------------------------------------ the protocol

    def test_editing_the_text_marks_the_lines_stale_and_a_sweep_rewrites_them(self):
        s = Setting.objects.create(
            folder=self.folder, title="林", content="黑发",
            tags=[{"id": "s1", "part": "头发", "tags": "black hair"}])
        got = self._patch(f"/api/settings/{s.id}", {"content": "棕发"})
        self.assertTrue(got["lines_stale"])
        s.refresh_from_db()
        self.assertTrue(s.lines_stale)
        # the old line is still there until the writer has been
        self.assertEqual(s.tags[0]["tags"], "black hair")

        self.assertEqual(library_lines.sweep(), 1)
        s.refresh_from_db()
        self.assertFalse(s.lines_stale)
        hair = next(ln for ln in s.tags if ln["part"] == "头发")
        # rewritten from the new text, and the line kept its id: pictures that
        # used s1 still point at the hair
        self.assertEqual((hair["id"], hair["tags"]), ("s1", "brown hair"))
        self.assertEqual(library_lines.sweep(), 0)

    def test_a_character_and_its_costume_are_each_written_on_their_own(self):
        c = Character.objects.create(folder=self.folder, name="林", description="黑发")
        v = CharacterVariant.objects.create(character=c, title="校服", content="白衬衫")
        self._patch(f"/api/characters/{c.id}", {"description": "棕发"})
        self._patch(f"/api/variants/{v.id}", {"content": "蓝衬衫"})
        self.assertEqual(library_lines.sweep(), 2)
        c.refresh_from_db()
        v.refresh_from_db()
        self.assertFalse(c.lines_stale)
        self.assertFalse(v.lines_stale)
        self.assertEqual([ln["id"][0] for ln in c.tags], ["k", "k"])
        self.assertEqual([ln["id"][0] for ln in v.tags], ["v", "v"])

    def test_lines_given_by_hand_are_the_persons_and_still_get_a_look(self):
        # a person's lines do not stand in for a model's look: the writer is
        # still due, sees the line marked as the person's, and keeps it
        s = Setting.objects.create(folder=self.folder, title="画风", content="厚涂",
                                   text_edited_at=timezone.now())
        self._patch(f"/api/settings/{s.id}", {"tags": [{"part": "画风", "tags": "painterly"}]})
        s.refresh_from_db()
        self.assertTrue(s.lines_stale)
        self.assertEqual(s.tags[0]["by"], "user")
        self.assertIn(f"s1  画风：painterly　{wording.LINE_BY_USER}", library_lines.brief(s))
        with mock.patch.object(library_lines.llm, "write_lines", return_value=(
                [{"part": "画风", "tags": "painterly"}, {"part": "笔触", "tags": "visible brushstrokes"}], {})):
            self.assertEqual(library_lines.sweep(), 1)
        s.refresh_from_db()
        self.assertFalse(s.lines_stale)
        # copied out verbatim, the line is still the person's; the new one is the model's
        self.assertEqual([(ln["tags"], ln.get("by")) for ln in s.tags],
                         [("painterly", "user"), ("visible brushstrokes", None)])
        self._patch(f"/api/settings/{s.id}", {"title": "画风二"})
        s.refresh_from_db()
        self.assertFalse(s.lines_stale, "a title is not text the lines are written from")

    def test_a_look_counts_whether_or_not_it_changed_a_word(self):
        s = Setting.objects.create(folder=self.folder, title="林", content="黑发",
                                   tags=[{"id": "s1", "part": "头发", "tags": "black hair"}],
                                   text_edited_at=timezone.now())
        self.assertTrue(s.lines_stale)
        with mock.patch.object(library_lines.llm, "write_lines",
                               return_value=([{"part": "头发", "tags": "black hair"}], {})):
            self.assertEqual(library_lines.sweep(), 1)
        s.refresh_from_db()
        self.assertFalse(s.lines_stale)
        self.assertEqual(library_lines.sweep(), 0)

    def test_a_line_changed_by_hand_is_the_persons_and_the_rest_stay_the_models(self):
        c = Character.objects.create(
            folder=self.folder, name="林", description="黑发",
            tags=[{"id": "k1", "part": "身份", "tags": "1girl"}, {"id": "k2", "part": "头发", "tags": "black hair"}])
        self._patch(f"/api/characters/{c.id}", {"tags": [
            {"id": "k1", "part": "身份", "tags": "1girl"},
            {"id": "k2", "part": "头发", "tags": "black hair, low ponytail"},
            {"part": "痣", "tags": "mole under eye"}]})
        c.refresh_from_db()
        self.assertEqual([(ln["id"], ln.get("by")) for ln in c.tags],
                         [("k1", None), ("k2", "user"), ("k3", "user")])
        # the text did not change, so no look is due for a hand edit alone
        self.assertFalse(c.lines_stale)

    def test_text_sent_back_unchanged_is_not_an_edit(self):
        # the sidebar's edit form sends title and text together whether or
        # not the text was touched. That has not changed what the lines are
        # written from, so it may not send the writer over lines someone wrote.
        s = Setting.objects.create(
            folder=self.folder, title="林", content="黑发",
            tags=[{"id": "s1", "part": "头发", "tags": "black hair"}])
        self._patch(f"/api/settings/{s.id}", {"title": "林二", "content": "黑发"})
        s.refresh_from_db()
        self.assertFalse(s.lines_stale)
        c = Character.objects.create(
            folder=self.folder, name="小满", description="黑发",
            tags=[{"id": "k1", "part": "头发", "tags": "black hair"}])
        v = CharacterVariant.objects.create(
            character=c, title="雨衣", content="黄雨衣",
            tags=[{"id": "v1", "part": "雨衣", "tags": "yellow raincoat"}])
        self._patch(f"/api/characters/{c.id}", {"description": "黑发"})
        self._patch(f"/api/variants/{v.id}", {"content": "黄雨衣"})
        c.refresh_from_db()
        v.refresh_from_db()
        self.assertFalse(c.lines_stale)
        self.assertFalse(v.lines_stale)
        self._patch(f"/api/variants/{v.id}", {"content": "蓝雨衣"})
        v.refresh_from_db()
        self.assertTrue(v.lines_stale, "text that did change still waits for the writer")

    def test_an_entry_no_model_has_looked_at_is_waiting_unless_it_has_no_text(self):
        c = Character.objects.create(folder=self.folder, name="老角色", description="黑发",
                                     text_edited_at=timezone.now())
        self.assertTrue(c.lines_stale)
        self.assertFalse(Character.objects.create(
            folder=self.folder, name="空的", description="", text_edited_at=timezone.now()).lines_stale)
        self.assertEqual(library_lines.sweep(), 1)
        c.refresh_from_db()
        self.assertEqual([ln["part"] for ln in c.tags], ["身份", "头发"])
        self.assertFalse(c.lines_stale)

    def test_a_failing_writer_leaves_the_entry_stale_and_rests_it(self):
        s = Setting.objects.create(folder=self.folder, title="林", content="黑发",
                                   text_edited_at=timezone.now())
        with mock.patch.object(library_lines.llm, "write_lines", side_effect=RuntimeError("down")) as w:
            self.assertEqual(library_lines.sweep(), 0)
            self.assertEqual(w.call_count, 1)
            s.refresh_from_db()
            self.assertTrue(s.lines_stale)
            self.assertIsNotNone(s.lines_tried_at)
            # not again this minute
            self.assertEqual(library_lines.sweep(), 0)
            self.assertEqual(w.call_count, 1)
        # after the rest, it is tried again, and this time it works
        Setting.objects.filter(id=s.id).update(
            lines_tried_at=timezone.now() - library_lines.RETRY_AFTER - timedelta(seconds=1))
        self.assertEqual(library_lines.sweep(), 1)

    def test_what_the_lead_writes_is_never_waiting_lines_or_no_lines(self):
        # the lead answers for its entries and is not told a writer exists:
        # an entry it made without lines stays without lines
        conv = Conversation.objects.create(title="t", folder=self.folder)
        msg = Message.objects.create(conversation=conv, seq=1, role="assistant", content="")
        proposals.record(conv, msg, {"settings": [
            {"title": "画风", "content": "厚涂", "scope": "conversation",
             "tags": [{"part": "画风", "tags": "painterly"}]},
            {"title": "无行", "content": "有正文没有行", "scope": "conversation"}], "characters": []})
        made = Setting.objects.get(title="画风", conversation=conv)
        self.assertFalse(made.lines_stale)
        self.assertFalse(Setting.objects.get(title="无行", conversation=conv).lines_stale)
        self.assertEqual(library_lines.sweep(), 0)
        proposals.record(conv, msg, {"settings": [
            {"title": "画风", "content": "水彩", "scope": "conversation"}], "characters": []})
        made.refresh_from_db()
        self.assertEqual(made.content, "水彩")
        self.assertFalse(made.lines_stale, "the lead changed the text and chose to keep the lines")
        # a person's edit of the text, on the other hand, is a model's to look at
        self._patch(f"/api/settings/{made.id}", {"content": "水彩，淡的"})
        made.refresh_from_db()
        self.assertTrue(made.lines_stale)

    def test_the_lead_repeating_a_character_to_add_a_costume_leaves_her_lines_fresh(self):
        conv = Conversation.objects.create(title="t", folder=self.folder)
        msg = Message.objects.create(conversation=conv, seq=1, role="assistant", content="")
        proposals.record(conv, msg, {"settings": [], "characters": [
            {"name": "小满", "description": "黑发的女孩", "scope": "conversation",
             "tags": [{"part": "身份", "tags": "1girl"}, {"part": "头发", "tags": "black hair"}],
             "variants": [{"title": "雨衣", "content": "黄雨衣",
                           "tags": [{"part": "雨衣", "tags": "yellow raincoat"}]}]}]})
        c = Character.objects.get(name="小满", conversation=conv)
        self.assertFalse(c.lines_stale)
        proposals.record(conv, msg, {"settings": [], "characters": [
            {"name": "小满", "description": "黑发的女孩", "scope": "conversation", "tags": [],
             "variants": [{"title": "夏日", "content": "白裙",
                           "tags": [{"part": "裙子", "tags": "white dress"}]}]}]})
        c.refresh_from_db()
        self.assertFalse(c.lines_stale)
        self.assertEqual([ln["tags"] for ln in c.tags], ["1girl", "black hair"])
        self.assertEqual(sorted(v.title for v in c.variants.all()), ["夏日", "雨衣"])

    def test_the_sweep_takes_a_few_at_a_time_oldest_first(self):
        for i in range(5):
            Setting.objects.create(folder=self.folder, title=f"s{i}", content="黑发",
                                   text_edited_at=timezone.now())
        self.assertEqual(library_lines.sweep(limit=2), 2)
        self.assertEqual(sum(1 for s in Setting.objects.all() if s.lines_stale), 3)

    # ------------------------------------------------------------ ids across costumes

    def test_two_costumes_of_one_character_never_share_a_line_id(self):
        conv = Conversation.objects.create(title="t", folder=self.folder)
        msg = Message.objects.create(conversation=conv, seq=1, role="assistant", content="")
        proposals.record(conv, msg, {"settings": [], "characters": [{
            "name": "林", "scope": "conversation",
            "tags": [{"part": "身份", "tags": "1girl"}],
            "variants": [
                {"title": "校服", "tags": [{"part": "衬衫", "tags": "white shirt"}]},
                {"title": "便服", "tags": [{"part": "毛衣", "tags": "sweater"}]},
            ],
        }]})
        c = Character.objects.get(name="林", conversation=conv)
        ids = [ln["id"] for v in c.variants.order_by("position") for ln in v.tags]
        self.assertEqual(len(ids), len(set(ids)), ids)
        # and an edit by hand of one costume keeps clear of the other
        second = c.variants.get(title="便服")
        self._patch(f"/api/variants/{second.id}",
                    {"tags": [{"part": "毛衣", "tags": "sweater"}, {"part": "裤子", "tags": "jeans"}]})
        ids = [ln["id"] for v in c.variants.all() for ln in v.tags]
        self.assertEqual(len(ids), len(set(ids)), ids)

    # ------------------------------------------------------------ the prompt

    def test_the_lead_and_the_patrol_read_the_same_tag_line_rules(self):
        from core import prompts
        rules = prompts.tag_lines().strip()
        self.assertTrue(rules)
        self.assertIn(rules, prompts.lead())
        self.assertIn(rules, prompts.patrol())
        self.assertNotIn(prompts.TAG_LINES_MARK, prompts.lead())

    def test_the_stand_in_writes_from_the_text(self):
        self.assertEqual(fake._lines_for("棕色头发")[1]["tags"], "brown hair")
        self.assertEqual(fake._lines_for("黑色头发")[1]["tags"], "black hair")
        # and nothing in it is a word the writer could not have been sent
        self.assertTrue(all(ln["part"] and ln["tags"] for ln in fake._lines_for("x")))
        self.assertEqual(library.lines_of(fake._lines_for("x"))[0]["id"], "")
