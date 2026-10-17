"""
The patrol: an entry whose note was edited by hand gets its tags and prose
rewritten from the note, off the request, on the sweeper's clock; an entry
from before the patrol existed gets them the same way; and the browser can
see which entries are waiting. Driven on the stand-in provider, whose hair
tag follows one word of the note, so a sweep that wrote FROM the note is told
apart from one that did not.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from core.models import Character, CharacterVariant, Conversation, Setting, Folder
from core.prompts import wording
from core.services import library, library_lines
from core.services.providers import fake

BROWN = library.split_tags(fake.STUB_FILL_BROWN)
BLACK = library.split_tags(fake.STUB_FILL_BLACK)


def _fill(positive, negative="", prose=""):
    """What a mocked llm.fill_entry hands back: the filled entry and its usage."""
    return ({"positive": library.split_tags(positive), "negative": library.split_tags(negative),
             "prose": prose}, {})


def _lead_setting(key: str, note: str, positive: str = "") -> dict:
    return {"key": key, "category": "", "note": note, "positive": positive, "negative": "", "prose": ""}


def _lead_character(key: str, note: str, positive: str = "", variants=()) -> dict:
    return {"key": key, "note": note, "positive": positive, "negative": "", "prose": "",
            "variants": [{"key": t, "note": n, "positive": p, "negative": "", "prose": ""} for t, n, p in variants]}


@override_settings(LLM_PROVIDER="fake", APP_USERNAME="w", APP_PASSWORD="pw")
class PatrolTests(TestCase):
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

    def test_editing_the_note_marks_the_entry_stale_and_a_sweep_rewrites_it_from_the_note(self):
        s = Setting.objects.create(folder=self.folder, title="林", content="黑发", positive=["black hair"])
        got = self._patch(f"/api/settings/{s.id}", {"content": "棕发"})
        self.assertTrue(got["lines_stale"])
        s.refresh_from_db()
        self.assertTrue(s.lines_stale)
        # the old tags stand until the patrol has been
        self.assertEqual(s.positive, ["black hair"])

        self.assertEqual(library_lines.sweep(), 1)
        s.refresh_from_db()
        self.assertFalse(s.lines_stale)
        self.assertEqual(s.positive, BROWN, "written from the new note, not from the old tags")
        self.assertEqual(library_lines.sweep(), 0)
        self.assertFalse(self.c.get(f"/api/folders/{self.folder.id}/library").json()["settings"][0]["lines_stale"])

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
        self.assertEqual(c.positive, BROWN)
        self.assertEqual(v.positive, BLACK)

    def test_the_patrol_fills_all_three_fields_from_the_note(self):
        s = Setting.objects.create(folder=self.folder, title="图书馆", content="旧图书馆", category="场景",
                                   text_edited_at=timezone.now())
        with mock.patch.object(library_lines.llm, "fill_entry", return_value=_fill(
                "dark wood bookshelves, old books", "modern furniture",
                "Warm sunlight comes through the tall window on the left.")) as fill:
            self.assertEqual(library_lines.sweep(), 1)
        s.refresh_from_db()
        self.assertEqual((s.positive, s.negative, s.prose),
                         (["dark wood bookshelves", "old books"], ["modern furniture"],
                          "Warm sunlight comes through the tall window on the left."))
        self.assertFalse(s.lines_stale)
        # handed what it needs: which entry, its note, and what it has now
        self.assertEqual(fill.call_args.args[0], library_lines.brief(s).replace(
            f"{wording.ENTRY_POSITIVE_LABEL}：dark wood bookshelves, old books",
            f"{wording.ENTRY_POSITIVE_LABEL}：{wording.FILL_NONE_YET}").replace(
            f"{wording.ENTRY_NEGATIVE_LABEL}：modern furniture",
            f"{wording.ENTRY_NEGATIVE_LABEL}：{wording.FILL_NONE_YET}").replace(
            f"{wording.ENTRY_PROSE_LABEL}：Warm sunlight comes through the tall window on the left.",
            f"{wording.ENTRY_PROSE_LABEL}：{wording.FILL_NONE_YET}"))

    def test_the_brief_names_the_entry_its_note_and_what_it_has_now(self):
        s = Setting.objects.create(folder=self.folder, title="画风", content="厚涂", positive=["painterly"])
        self.assertEqual(library_lines.brief(s), "\n".join([
            wording.FILL_FOR_SETTING.format(title="画风", category=wording.FILL_CATEGORY_OTHER),
            f"{wording.ENTRY_NOTE_LABEL}：厚涂",
            "",
            wording.FILL_CURRENT,
            f"{wording.ENTRY_POSITIVE_LABEL}：painterly",
            f"{wording.ENTRY_NEGATIVE_LABEL}：{wording.FILL_NONE_YET}",
            f"{wording.ENTRY_PROSE_LABEL}：{wording.FILL_NONE_YET}",
        ]))
        c = Character.objects.create(folder=self.folder, name="林", description="黑发")
        v = CharacterVariant.objects.create(character=c, title="校服", content="白衬衫", prose="A white shirt.")
        self.assertTrue(library_lines.brief(c).startswith(wording.FILL_FOR_CHARACTER.format(name="林")))
        self.assertTrue(library_lines.brief(v).startswith(wording.FILL_FOR_VARIANT.format(name="林", title="校服")))
        self.assertIn(f"{wording.ENTRY_PROSE_LABEL}：A white shirt.", library_lines.brief(v))

    def test_tags_given_by_hand_are_the_persons_and_the_entry_still_gets_a_look(self):
        # a person's tags do not stand in for a model's look: the patrol is
        # still due, sees the tags, and may keep them
        s = Setting.objects.create(folder=self.folder, title="画风", content="厚涂",
                                   text_edited_at=timezone.now())
        self._patch(f"/api/settings/{s.id}", {"positive": "painterly"})
        s.refresh_from_db()
        self.assertTrue(s.lines_stale)
        self.assertEqual(s.positive, ["painterly"])
        self.assertIn(f"{wording.ENTRY_POSITIVE_LABEL}：painterly", library_lines.brief(s))
        with mock.patch.object(library_lines.llm, "fill_entry",
                               return_value=_fill("painterly, visible brushstrokes")):
            self.assertEqual(library_lines.sweep(), 1)
        s.refresh_from_db()
        self.assertFalse(s.lines_stale)
        self.assertEqual(s.positive, ["painterly", "visible brushstrokes"])
        self._patch(f"/api/settings/{s.id}", {"title": "画风二"})
        s.refresh_from_db()
        self.assertFalse(s.lines_stale, "a title is not text the tags are written from")

    def test_a_look_counts_whether_or_not_it_changed_a_word(self):
        s = Setting.objects.create(folder=self.folder, title="林", content="黑发", positive=["black hair"],
                                   text_edited_at=timezone.now())
        self.assertTrue(s.lines_stale)
        with mock.patch.object(library_lines.llm, "fill_entry", return_value=_fill("black hair")):
            self.assertEqual(library_lines.sweep(), 1)
        s.refresh_from_db()
        self.assertFalse(s.lines_stale)
        self.assertEqual(library_lines.sweep(), 0)

    def test_tags_and_prose_changed_by_hand_are_stored_as_given_and_owe_no_look(self):
        c = Character.objects.create(folder=self.folder, name="林", description="黑发",
                                     positive=["1girl", "black hair"])
        self._patch(f"/api/characters/{c.id}", {"positive": "1girl, black hair, low ponytail",
                                                "negative": "child", "prose": "Her hair is tied low."})
        c.refresh_from_db()
        self.assertEqual((c.positive, c.negative, c.prose),
                         (["1girl", "black hair", "low ponytail"], ["child"], "Her hair is tied low."))
        # the note did not change, so no look is due for a hand edit alone
        self.assertFalse(c.lines_stale)
        self.assertEqual(library_lines.sweep(), 0)

    def test_a_note_sent_back_unchanged_is_not_an_edit(self):
        # the sidebar's edit form sends title and note together whether or
        # not the note was touched. That has not changed what the tags are
        # written from, so it may not send the patrol over tags someone wrote.
        s = Setting.objects.create(folder=self.folder, title="林", content="黑发", positive=["black hair"])
        self._patch(f"/api/settings/{s.id}", {"title": "林二", "content": "黑发"})
        s.refresh_from_db()
        self.assertFalse(s.lines_stale)
        c = Character.objects.create(folder=self.folder, name="小满", description="黑发", positive=["black hair"])
        v = CharacterVariant.objects.create(character=c, title="雨衣", content="黄雨衣", positive=["yellow raincoat"])
        self._patch(f"/api/characters/{c.id}", {"description": "黑发"})
        self._patch(f"/api/variants/{v.id}", {"content": "黄雨衣"})
        c.refresh_from_db()
        v.refresh_from_db()
        self.assertFalse(c.lines_stale)
        self.assertFalse(v.lines_stale)
        self._patch(f"/api/variants/{v.id}", {"content": "蓝雨衣"})
        v.refresh_from_db()
        self.assertTrue(v.lines_stale, "a note that did change still waits for the patrol")

    def test_an_entry_no_model_has_looked_at_is_waiting_unless_it_has_no_note(self):
        c = Character.objects.create(folder=self.folder, name="老角色", description="黑发",
                                     text_edited_at=timezone.now())
        self.assertTrue(c.lines_stale)
        self.assertFalse(Character.objects.create(
            folder=self.folder, name="空的", description="", text_edited_at=timezone.now()).lines_stale)
        self.assertEqual(library_lines.sweep(), 1)
        c.refresh_from_db()
        self.assertEqual(c.positive, BLACK)
        self.assertFalse(c.lines_stale)

    def test_a_failing_patrol_leaves_the_entry_stale_and_rests_it(self):
        s = Setting.objects.create(folder=self.folder, title="林", content="黑发",
                                   text_edited_at=timezone.now())
        with mock.patch.object(library_lines.llm, "fill_entry", side_effect=RuntimeError("down")) as w:
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
        s.refresh_from_db()
        self.assertEqual(s.positive, BLACK)

    def test_the_patrol_answering_with_nothing_leaves_the_entry_alone(self):
        c = Character.objects.create(folder=self.folder, name="林", description="黑发女孩",
                                     positive=["1girl", "black hair"], prose="Her hair is short.",
                                     text_edited_at=timezone.now())
        with mock.patch.object(library_lines.llm, "fill_entry", return_value=_fill("")):
            self.assertFalse(library_lines.write(c))
        c.refresh_from_db()
        self.assertEqual((c.positive, c.prose), (["1girl", "black hair"], "Her hair is short."))
        self.assertTrue(c.lines_stale, "the entry waits for a real look")
        self.assertIsNotNone(c.lines_tried_at)

    def test_a_note_with_nothing_to_paint_is_looked_at_once_and_left_be(self):
        """A memo entry (a list of names) has nothing to paint: the patrol's
        empty answer is its answer, not a failure to retry every ten minutes."""
        s = Setting.objects.create(folder=self.folder, title="人名登记", content="女主林晚，闺蜜苏棠，房东姓周",
                                   position=1, text_edited_at=timezone.now())
        self.assertTrue(s.lines_stale)
        with mock.patch.object(library_lines.llm, "fill_entry", return_value=_fill("")):
            self.assertTrue(library_lines.write(s))
        s.refresh_from_db()
        self.assertEqual((s.positive, s.negative, s.prose), ([], [], ""))
        self.assertFalse(s.lines_stale)
        self.assertNotIn(s.id, [o.id for o in library_lines.stale(limit=10)])

    def test_what_the_lead_writes_is_never_waiting_tags_or_no_tags(self):
        # the lead answers for its entries and is not told a patrol exists:
        # an entry it made without tags stays without tags
        conv = Conversation.objects.create(title="t", folder=self.folder)
        library.write_conversation_entries(conv, 1, [
            _lead_setting("画风", "厚涂", "painterly"),
            _lead_setting("无tag", "有注释没有tag"),
        ], [])
        made = Setting.objects.get(title="画风", conversation=conv)
        self.assertFalse(made.lines_stale)
        self.assertEqual(made.positive, ["painterly"])
        self.assertFalse(Setting.objects.get(title="无tag", conversation=conv).lines_stale)
        self.assertEqual(library_lines.sweep(), 0)
        # the lead writes the same key again: the whole entry, note and tags, as it chose
        library.write_conversation_entries(conv, 2, [_lead_setting("画风", "水彩", "watercolor")], [])
        made.refresh_from_db()
        self.assertEqual((made.content, made.positive, made.origin_seq), ("水彩", ["watercolor"], 2))
        self.assertFalse(made.lines_stale, "the lead changed the note and wrote the tags with it")
        self.assertEqual(library_lines.sweep(), 0)
        # a person's edit of the note, on the other hand, is a model's to look at
        self._patch(f"/api/settings/{made.id}", {"content": "水彩，淡的"})
        made.refresh_from_db()
        self.assertTrue(made.lines_stale)
        self.assertEqual(library_lines.sweep(), 1)

    def test_the_lead_repeating_a_character_to_add_a_costume_leaves_her_fresh(self):
        conv = Conversation.objects.create(title="t", folder=self.folder)
        library.write_conversation_entries(conv, 1, [], [
            _lead_character("小满", "黑发的女孩", "1girl, black hair", [("雨衣", "黄雨衣", "yellow raincoat")])])
        c = Character.objects.get(name="小满", conversation=conv)
        self.assertFalse(c.lines_stale)
        library.write_conversation_entries(conv, 2, [], [
            _lead_character("小满", "黑发的女孩", "1girl, black hair", [("夏日", "白裙", "white dress")])])
        c.refresh_from_db()
        self.assertFalse(c.lines_stale)
        self.assertEqual(c.positive, ["1girl", "black hair"])
        self.assertEqual(sorted((v.title, v.positive[0], v.lines_stale) for v in c.variants.all()),
                         [("夏日", "white dress", False), ("雨衣", "yellow raincoat", False)])
        self.assertEqual(library_lines.sweep(), 0)

    def test_the_sweep_takes_a_few_at_a_time_oldest_first(self):
        for i in range(5):
            Setting.objects.create(folder=self.folder, title=f"s{i}", content="黑发",
                                   text_edited_at=timezone.now())
        self.assertEqual(library_lines.sweep(limit=2), 2)
        self.assertEqual(sum(1 for s in Setting.objects.all() if s.lines_stale), 3)
        self.assertEqual([s.title for s in Setting.objects.all() if not s.lines_stale], ["s0", "s1"])

    # ------------------------------------------------------------ the prompt

    def test_the_lead_and_the_patrol_read_the_same_entry_rules(self):
        from core import prompts
        rules = prompts.entries().strip()
        self.assertTrue(rules)
        self.assertIn(rules, prompts.lead())
        self.assertIn(rules, prompts.patrol())
        self.assertNotIn(prompts.ENTRIES_MARK, prompts.lead())

    def test_the_stand_in_writes_from_the_note(self):
        self.assertEqual(fake._fill("棕色头发")["positive"], fake.STUB_FILL_BROWN)
        self.assertEqual(fake._fill("黑色头发")["positive"], fake.STUB_FILL_BLACK)
        # and what it writes is tags the app can read as tags
        self.assertEqual(library.split_tags(fake._fill("x")["positive"]), BLACK)
        self.assertEqual(set(fake._fill("x")), {"positive", "negative", "prose"})
