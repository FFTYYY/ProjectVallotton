"""
The reworked workflow, driven end to end through the real endpoints on the
stand-in provider: entries with tag lines in two layers, a picture made from
those lines, a change to it that keeps what it did not mention and follows the
main picture's seed, and a main picture the user can move.

Nothing here reads a prompt file. What the stand-in decides, it decides from
the message it was sent (see providers/fake.py), so these pass and fail on the
app's behaviour alone.
"""

from __future__ import annotations

import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from core.models import (
    Character, CharacterVariant, Conversation, Folder, LibraryProposal, Message, Setting,
)
from core import ui_text
from core.prompts import wording
from core.services import library, llm, pipeline, prompt as prompt_slots, proposals
from core.services.providers.fake import EDIT_MARK, LOOKUP_MARK, NAMELESS_EDIT_MARK

from .tests import _sync_jobs


@override_settings(LLM_PROVIDER="fake", FAKE_NOVELAI=True, APP_USERNAME="w", APP_PASSWORD="pw")
class WorkflowTests(TestCase):
    def setUp(self):
        get_user_model().objects.create_user(username="w", password="pw")
        self.c = Client()
        self.c.force_login(get_user_model().objects.get(username="w"))
        self.env = mock.patch.dict("os.environ", {"FAKE_LATENCY_MS": "0", "FAKE_NOVELAI": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _conv(self, folder: Folder | None = None) -> Conversation:
        conv = Conversation.objects.create(title="t", folder=folder)
        return conv

    def _send(self, conv: Conversation, text: str) -> Message:
        with _sync_jobs(), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            r = self.c.post(f"/api/conversations/{conv.id}/messages",
                            {"text": text, "n_images": "2", "reflection_rounds": "0"})
        self.assertEqual(r.status_code, 201, r.content[:200])
        msg = conv.messages.filter(role="assistant").order_by("-seq").first()
        assert msg is not None
        msg.refresh_from_db()
        self.assertEqual(msg.status, "done", msg.error)
        return msg

    # ------------------------------------------------------------ pictures

    def test_a_picture_is_made_in_slots_and_the_row_keeps_them(self):
        conv = self._conv()
        msg = self._send(conv, "画一张")
        assert msg.prompt is not None
        self.assertIn("slots", msg.prompt)
        self.assertEqual(msg.attachments.filter(kind="generated").count(), 2)
        self.assertEqual(msg.main_index, 0)
        assert msg.nai_prompts is not None
        self.assertTrue(msg.nai_prompts["draw_id"])

    def test_an_edit_continues_the_drawing_and_follows_its_main_seed(self):
        conv = self._conv()
        first = self._send(conv, "画一张")
        pictures = list(first.attachments.filter(kind="generated").order_by("position"))
        self.assertEqual(len(pictures), 2)

        # the second picture becomes the main one, named by its position
        r = self.c.post(f"/api/messages/{first.id}/main",
                        json.dumps({"index": pictures[1].position}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["messages"][0]["main_index"], pictures[1].position)

        second = self._send(conv, f"{EDIT_MARK}：再亮一点")
        assert second.draw_brief is not None
        self.assertEqual(second.draw_brief["continues"], (first.nai_prompts or {})["draw_id"])
        self.assertEqual(second.draw_brief["change"], f"{EDIT_MARK}：再亮一点")
        # the first picture of the edit follows the main picture's seed; the
        # second rolls its own
        seeds = [a.seed for a in second.attachments.filter(kind="generated").order_by("position")]
        self.assertEqual(seeds[0], pictures[1].seed)
        self.assertNotEqual(seeds[1], pictures[1].seed)
        # and the change list is on the row for 「检查」
        raw = (second.model_raw or {})
        self.assertEqual(raw.get("draw_patch"), {"other": {"prose": {"set": "redrawn by the stand-in"}}})
        # what was not mentioned stayed byte for byte
        assert first.prompt and second.prompt
        self.assertEqual(first.prompt["slots"]["count"], second.prompt["slots"]["count"])
        self.assertEqual(second.prompt["slots"]["other"]["prose"], "redrawn by the stand-in")
        # and the sentence went out after the tags
        self.assertTrue((second.nai_prompts or {})["base_prompt"].endswith("redrawn by the stand-in."))

    def test_a_change_that_names_no_drawing_changes_the_latest_one(self):
        """The real lead now and then writes `change` and leaves `continues`
        empty; read as a new picture, that throws away everything the change
        did not mention."""
        conv = self._conv()
        first = self._send(conv, "画一张")
        second = self._send(conv, f"{NAMELESS_EDIT_MARK}：再亮一点")
        assert second.draw_brief is not None
        self.assertEqual(second.draw_brief["continues"], (first.nai_prompts or {})["draw_id"])
        self.assertEqual((second.model_raw or {}).get("draw_patch"),
                         {"other": {"prose": {"set": "redrawn by the stand-in"}}})

    def test_a_new_picture_follows_the_latest_main_seed_too(self):
        conv = self._conv()
        first = self._send(conv, "画一张")
        main = first.attachments.filter(kind="generated").order_by("position").first()
        assert main is not None
        second = self._send(conv, "再画一张")
        seeds = [a.seed for a in second.attachments.filter(kind="generated").order_by("position")]
        self.assertEqual(seeds[0], main.seed)

    def test_setting_a_picture_that_does_not_exist_is_refused(self):
        conv = self._conv()
        msg = self._send(conv, "画一张")
        r = self.c.post(f"/api/messages/{msg.id}/main", json.dumps({"index": 7}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_the_main_picture_survives_by_position_when_earlier_ones_are_gone(self):
        """A reflection round discards the pictures it replaced; positions keep counting."""
        conv = self._conv()
        msg = self._send(conv, "画一张")
        pictures = list(msg.attachments.filter(kind="generated").order_by("position"))
        pictures[0].delete()
        self.assertEqual(pipeline.main_seed(Message.objects.get(id=msg.id)), pictures[1].seed)

    def test_a_lookup_is_answered_and_kept_on_the_row(self):
        conv = self._conv()
        msg = self._send(conv, f"{LOOKUP_MARK}，画一张")
        raw = msg.model_raw or {}
        self.assertEqual(len(raw.get("lookups") or []), 1)
        self.assertIn("1girl", raw["lookups"][0]["result"])
        r = self.c.get(f"/api/messages/{msg.id}/raw").json()
        self.assertEqual(len(r["lookups"]["lead"]), 1)

    # ------------------------------------------------------------ the library

    def test_a_note_for_this_conversation_is_written_at_once(self):
        """No project: everything the model records lives in this conversation."""
        conv = self._conv()
        self._send(conv, "把「黄昏」记下来")
        made = Setting.objects.get(title="黄昏")
        self.assertEqual(made.conversation_id, conv.id)
        self.assertIsNone(made.folder_id)
        self.assertEqual(LibraryProposal.objects.get(conversation=conv).status, "applied")
        # switched on, and read by both models next turn
        self.assertIn(made.id, set(conv.selected_settings.values_list("id", flat=True)))
        sel = library.selection_for(conv)
        self.assertIn("黄昏", library.as_prompt_text(sel))

    def test_in_a_project_a_project_scoped_note_waits_for_approval(self):
        folder = Folder.objects.create(name="项目")
        conv = self._conv(folder)
        self._send(conv, "把「黄昏」记下来")
        self.assertFalse(Setting.objects.filter(title="黄昏").exists())
        self.assertEqual(LibraryProposal.objects.get(conversation=conv).status, "pending")

    def test_in_a_project_an_unscoped_note_is_the_conversations_own(self):
        """The project is the layer the user asks for by name; a note the
        model forgot to scope stays in this conversation."""
        folder = Folder.objects.create(name="项目")
        conv = self._conv(folder)
        self._send(conv, "把「黄昏」记下来，不说层")
        made = Setting.objects.get(title="黄昏")
        self.assertEqual(made.conversation_id, conv.id)
        self.assertEqual(LibraryProposal.objects.get(conversation=conv).status, "applied")

    def test_the_receipt_names_the_entries_the_turn_itself_wrote(self):
        """A turn that records a conversation-layer entry and draws from it
        in the same breath: the receipt (selection_snapshot) has to carry that
        entry, since the picture was drawn from its lines."""
        conv = self._conv()
        msg = self._send(conv, "把「黄昏」记下来，本对话")
        self.assertTrue((msg.nai_prompts or {}).get("draw_id"))
        snap = msg.selection_snapshot or {}
        self.assertEqual([s["title"] for s in snap.get("settings") or []], ["黄昏"])

    def test_a_branch_rebuilds_the_conversation_layer_as_it_was_at_that_message(self):
        """A branch at an earlier message starts with the entries that point
        in the story had -- not the ones added later, and not none. (That
        point is the moment the next message was sent: see the test below
        for what a hand edit in between does.)"""
        conv = self._conv()
        first = self._send(conv, "把「黄昏」记下来")
        second = self._send(conv, "把「夜景」记下来")
        r = self.c.post(f"/api/messages/{first.id}/branch")
        self.assertEqual(r.status_code, 201, r.content[:200])
        early = Conversation.objects.get(id=r.json()["id"])
        self.assertEqual(list(early.own_settings.values_list("title", flat=True)), ["黄昏"])
        self.assertEqual(list(early.selected_settings.values_list("title", flat=True)), ["黄昏"])
        # a copy with the lines' ids kept, and the source untouched
        src = Setting.objects.get(conversation=conv, title="黄昏")
        self.assertEqual([ln["id"] for ln in early.own_settings.get().tags],
                         [ln["id"] for ln in src.tags])
        self.assertEqual(set(conv.own_settings.values_list("title", flat=True)), {"黄昏", "夜景"})
        r = self.c.post(f"/api/messages/{second.id}/branch")
        late = Conversation.objects.get(id=r.json()["id"])
        self.assertEqual(set(late.own_settings.values_list("title", flat=True)), {"黄昏", "夜景"})

    def test_a_branch_at_an_earlier_message_undoes_later_edits_and_deletions_of_the_layer(self):
        """The conversation layer at that message, not as it reads now: an
        entry edited later comes back as it was, an entry deleted later
        comes back at all, and what was switched on then is switched on."""
        conv = self._conv()
        first = self._send(conv, "把「黄昏」记下来")
        dusk = Setting.objects.get(conversation=conv, title="黄昏")
        then_content, then_lines = dusk.content, [ln["tags"] for ln in dusk.tags]
        # later: the user rewrites it by hand, the lead adds another, and the user deletes the first
        r = self.c.patch(f"/api/settings/{dusk.id}", json.dumps({"content": "改成深夜", "tags": [
            {"part": "天色", "tags": "midnight"}]}), content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content[:200])
        second = self._send(conv, "把「夜景」记下来")
        self.assertEqual(Setting.objects.get(id=dusk.id).content, "改成深夜")
        r = self.c.delete(f"/api/settings/{dusk.id}")
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertFalse(Setting.objects.filter(id=dusk.id).exists())
        # A branch at the first message starts where the second message was
        # sent from: 黄昏 as the user had rewritten it by then (the hand edit
        # came before the second message), still existing, still on; 夜景 not
        # yet. The deletion came after the second message, so it is undone.
        r = self.c.post(f"/api/messages/{first.id}/branch")
        early = Conversation.objects.get(id=r.json()["id"])
        got = early.own_settings.get()
        self.assertEqual(got.title, "黄昏")
        self.assertEqual(got.content, "改成深夜")
        self.assertEqual([ln["tags"] for ln in got.tags], ["midnight"])
        self.assertNotEqual((then_content, then_lines), (got.content, [ln["tags"] for ln in got.tags]))
        self.assertEqual(list(early.selected_settings.values_list("title", flat=True)), ["黄昏"])
        # a branch at the second (latest) message starts from now: 黄昏 is gone, 夜景 is there
        r = self.c.post(f"/api/messages/{second.id}/branch")
        late = Conversation.objects.get(id=r.json()["id"])
        self.assertEqual(set(late.own_settings.values_list("title", flat=True)), {"夜景"})
        # the source conversation itself is untouched by either branch
        self.assertEqual(set(conv.own_settings.values_list("title", flat=True)), {"夜景"})

    def test_a_branch_of_a_history_without_snapshots_copies_the_layer_as_it_is_now(self):
        conv = self._conv()
        msg = self._send(conv, "把「黄昏」记下来")
        Message.objects.filter(conversation=conv).update(library_snapshot=None)
        r = self.c.post(f"/api/messages/{msg.id}/branch")
        branch = Conversation.objects.get(id=r.json()["id"])
        self.assertEqual(list(branch.own_settings.values_list("title", flat=True)), ["黄昏"])

    def test_a_new_conversation_in_a_project_starts_with_the_projects_settings_on(self):
        folder = Folder.objects.create(name="项目")
        style = Setting.objects.create(folder=folder, title="画风", content="厚涂",
                                       tags=[{"id": "s1", "part": "画风", "tags": "painterly"}])
        r = self.c.post("/api/conversations", json.dumps({"folder_id": str(folder.id)}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content[:200])
        conv = Conversation.objects.get(id=r.json()["id"])
        self.assertEqual([s.id for s in conv.selected_settings.all()], [style.id])
        # and so does one moved into the project later
        other = Conversation.objects.create(title="t")
        r = self.c.post(f"/api/conversations/{other.id}/folder", json.dumps({"folder_id": str(folder.id)}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual([s.id for s in other.selected_settings.all()], [style.id])

    def test_the_lead_sees_the_rest_of_the_project_and_switches_a_character_on_by_naming_her(self):
        """A project character that this conversation has not switched on is
        listed to the lead by name; drawing her by name switches her on,
        costume and all, instead of the lead making her up again."""
        folder = Folder.objects.create(name="项目")
        conv = self._conv(folder)
        her = Character.objects.create(
            folder=folder, name="瓦洛东小姐", description="银发的画家",
            tags=[{"id": "k1", "part": "身份", "tags": "1girl"}, {"id": "k2", "part": "头发", "tags": "silver hair"}])
        CharacterVariant.objects.create(character=her, title="便服", position=1,
                                        tags=[{"id": "v1", "part": "毛衣", "tags": "sweater"}])
        CharacterVariant.objects.create(character=her, title="礼服", position=2,
                                        tags=[{"id": "v2", "part": "裙子", "tags": "dress"}])
        text = library.as_prompt_text(library.selection_for(conv))
        self.assertIn(wording.LIBRARY_OFF_HEADING, text)
        self.assertIn(wording.LIBRARY_OFF_CHARACTER.format(name="瓦洛东小姐", text="银发的画家"), text)
        self.assertIn(wording.LIBRARY_OFF_VARIANTS.format(titles="便服、礼服"), text)
        self.assertNotIn("k2  头发", text, "an entry that is off is named, not spelled out")

        # the stand-in lead draws 瓦洛东小姐 with no outfit named
        msg = self._send(conv, "画她")
        on = list(conv.selected_variants.all())
        self.assertEqual([(v.character.name, v.title) for v in on], [("瓦洛东小姐", "便服")],
                         "named with no outfit: her first costume")
        snap = msg.selection_snapshot or {}
        self.assertEqual([c["name"] for c in snap.get("characters") or []], ["瓦洛东小姐"])
        text = library.as_prompt_text(library.selection_for(conv))
        self.assertIn("k2  头发：silver hair", text)
        self.assertNotIn(wording.LIBRARY_OFF_HEADING, text)

    def test_a_conversation_edit_of_a_project_character_shadows_it_with_a_seeded_copy(self):
        """What is said about a character in a conversation changes this
        conversation's copy; the project entry changes only when asked."""
        folder = Folder.objects.create(name="项目")
        conv = self._conv(folder)
        project = Character.objects.create(
            folder=folder, name="林", description="黑发",
            tags=[{"id": "k1", "part": "身份", "tags": "1girl"},
                  {"id": "k2", "part": "头发", "tags": "black hair"}])
        CharacterVariant.objects.create(
            character=project, title="校服",
            tags=[{"id": "v1", "part": "衬衫", "tags": "white shirt"}])
        asked = Message.objects.create(conversation=conv, seq=1, role="user", content="加眼镜")
        proposals.record(conv, asked, {"settings": [], "characters": [{
            "name": "林", "scope": "conversation",
            "tags": [{"part": "身份", "tags": "1girl"}, {"part": "头发", "tags": "black hair"},
                     {"part": "眼镜", "tags": "glasses"}],
            "variants": [],
        }]})
        project.refresh_from_db()
        self.assertEqual([ln["id"] for ln in project.tags], ["k1", "k2"])
        own = Character.objects.get(name="林", conversation=conv)
        self.assertEqual([(ln["id"], ln["part"]) for ln in own.tags],
                         [("k1", "身份"), ("k2", "头发"), ("k3", "眼镜")])
        self.assertEqual(own.description, "黑发")
        self.assertEqual([(v.title, v.tags[0]["id"]) for v in own.variants.all()], [("校服", "v1")])
        sel = library.selection_for(conv)
        self.assertIn("k3", library.as_lines(sel))

    def test_only_a_settings_style_lines_fill_the_style_slot_the_rest_are_listed_to_pick(self):
        conv = self._conv()
        r = self.c.post(f"/api/conversations/{conv.id}/settings",
                        json.dumps({"title": "图书馆", "content": "旧图书馆",
                                    "tags": [{"part": "画风", "tags": "painterly"},
                                             {"part": "书架", "tags": "dark wood bookshelves"}]}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content[:200])
        sel = library.selection_for(conv)
        text = library.as_lines(sel)
        self.assertIn("s2  书架：dark wood bookshelves", text)
        self.assertIn("s1  画风：painterly" + wording.LIBRARY_STYLE_LINE_MARK, text)
        self.assertEqual(library.setting_lines_map(sel),
                         {"图书馆": {"s1": "painterly", "s2": "dark wood bookshelves"}})
        self.assertEqual(library.style_lines_map(sel), {"图书馆": {"s1": "painterly"}})
        self.assertEqual(library.setting_truth(sel),
                         {"图书馆": [("s1", "画风", "painterly", True), ("s2", "书架", "dark wood bookshelves", False)]})

    def test_approving_switches_the_entry_on_where_it_was_proposed(self):
        """The lead wrote it down to draw from it here; an approved entry left
        off was read by the drawing model as no entry at all."""
        folder = Folder.objects.create(name="项目")
        conv = self._conv(folder)
        self._send(conv, "把「黄昏」记下来")
        p = LibraryProposal.objects.get(conversation=conv)
        r = self.c.post(f"/api/proposals/{p.id}/resolve", json.dumps({"approve": True}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content[:200])
        made = Setting.objects.get(title="黄昏")
        self.assertEqual(made.folder_id, folder.id)
        self.assertIn(made.id, set(conv.selected_settings.values_list("id", flat=True)))
        self.assertIn("黄昏", library.as_prompt_text(library.selection_for(conv)))

    def test_a_conversation_entry_can_be_promoted_and_shadows_until_then(self):
        folder = Folder.objects.create(name="项目")
        conv = self._conv(folder)
        project = Setting.objects.create(folder=folder, title="画风", content="厚涂")
        own = Setting.objects.create(folder=folder, conversation=conv, title="画风", content="水彩")
        conv.selected_settings.add(project, own)
        sel = library.selection_for(conv)
        # the nearer one wins, and only one of them is sent
        self.assertEqual([s.content for s in sel.settings], ["水彩"])

        r = self.c.post(f"/api/settings/{own.id}/promote")
        self.assertEqual(r.status_code, 400, "the project already has that title")
        project.delete()
        r = self.c.post(f"/api/settings/{own.id}/promote")
        self.assertEqual(r.status_code, 200)
        own.refresh_from_db()
        self.assertIsNone(own.conversation_id)
        self.assertEqual(own.folder_id, folder.id)

    def test_the_selection_lists_both_layers(self):
        folder = Folder.objects.create(name="项目")
        conv = self._conv(folder)
        Character.objects.create(folder=folder, name="林")
        r = self.c.post(f"/api/conversations/{conv.id}/characters",
                        json.dumps({"name": "猫", "tags": [{"part": "身份", "tags": "cat"}]}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content[:200])
        self.assertEqual(r.json()["tags"][0]["id"], "k1")
        payload = self.c.get(f"/api/conversations/{conv.id}/selection").json()
        names = {(c["name"], c["conversation_id"] is not None) for c in payload["characters"]}
        self.assertEqual(names, {("林", False), ("猫", True)})

    # ------------------------------------------------------------ lines

    def test_line_ids_survive_an_edit_of_the_entry(self):
        old = [{"id": "k1", "part": "身份", "tags": "1girl"}, {"id": "k2", "part": "头发", "tags": "black hair"}]
        new = [{"part": "头发", "tags": "black hair, long hair"}, {"part": "身份", "tags": "1girl"},
               {"part": "眼睛", "tags": "brown eyes"}]
        out = library.assign_ids(old, new, "k")
        self.assertEqual([(ln["part"], ln["id"]) for ln in out], [("头发", "k2"), ("身份", "k1"), ("眼睛", "k3")])

    def test_a_renamed_line_keeps_its_id_when_it_says_the_same_thing(self):
        old = [{"id": "k7", "part": "发", "tags": "black hair, long hair"}]
        out = library.assign_ids(old, [{"part": "头发", "tags": "black hair, long hair, bangs"}], "k")
        self.assertEqual(out[0]["id"], "k7")

    def test_words_are_copied_from_the_picture_not_from_memory(self):
        lines, dropped = library.copied_lines(
            "1girl, kaname raana, moss green robe",
            [{"part": "身份", "tags": "1girl, kaname_raana"}, {"part": "长袍", "tags": "moss green robe, hood"}],
        )
        self.assertEqual([ln["tags"] for ln in lines], ["1girl, kaname raana", "moss green robe"])
        self.assertEqual(dropped, ["hood"])

    def test_an_edit_can_add_a_subject_and_drop_one(self):
        picture = prompt_slots.upgrade({"size": "portrait", "slots": {
            "count": {"tags": "1girl, solo", "prose": ""}, "scene": {"tags": "library", "prose": ""}, "text": ""},
            "characters": [{"key": "林", "from": "林", "use": ["k1"], "patches": [],
                            "look": {"tags": "", "prose": ""}, "moment": {"tags": "standing", "prose": ""},
                            "center_x": None, "center_y": None}], "negative": ""})
        out = prompt_slots.apply(picture, {
            "count": {"tags": {"set": "1girl, 1other"}},
            "characters": [{"from": "", "look": {"tags": {"set": "white cat"}},
                            "moment": {"tags": {"set": "sitting on shelf, looking at viewer"}}}],
        })
        self.assertEqual([c["key"] for c in out["characters"]], ["林", "主体2"])
        cat = out["characters"][1]
        self.assertEqual(cat["from"], "")
        self.assertEqual(cat["look"]["tags"], "white cat")
        self.assertEqual(cat["moment"]["tags"], "sitting on shelf, looking at viewer")
        self.assertEqual(out["characters"][0], picture["characters"][0])
        rendered = prompt_slots.render(out, {"林": {"k1": "1girl, black hair"}})
        self.assertEqual([c["prompt"] for c in rendered["character_prompts"]],
                         ["1girl, black hair, standing", "white cat, sitting on shelf, looking at viewer"])
        # a library subject joins under its entry name
        out2 = prompt_slots.apply(out, {"characters": [{"from": "猫", "use": {"set": "k1, k2"}}]})
        self.assertEqual(out2["characters"][2]["key"], "猫")
        self.assertEqual(out2["characters"][2]["use"], ["k1", "k2"])
        # and leaves again
        out3 = prompt_slots.apply(out2, {"characters": [{"from": "主体2", "drop": True}]})
        self.assertEqual([c["key"] for c in out3["characters"]], ["林", "猫"])
        with self.assertRaises(prompt_slots.PatchRejected):
            prompt_slots.apply(out3, {"characters": [{"from": "主体2", "drop": True}]})
        # an item that names no subject and brings nothing is still rejected
        with self.assertRaises(prompt_slots.PatchRejected):
            prompt_slots.apply(out3, {"characters": [{"from": "谁"}]})

    def test_an_edit_can_bind_a_nameless_subject_to_an_entry(self):
        picture = prompt_slots.upgrade({"size": "portrait", "slots": {
            "count": {"tags": "1girl", "prose": ""}, "text": ""},
            "characters": [{"key": "主体1", "from": "", "use": [], "patches": [],
                            "look": {"tags": "wakaba mutsumi", "prose": ""},
                            "moment": {"tags": "reclining", "prose": ""},
                            "center_x": None, "center_y": None}], "negative": ""})
        lines = {"若叶睦": {"k1": "1girl, wakaba_mutsumi", "v1": "tsukinomori_school_uniform"}}
        # before the binding the ids name lines in no book: nothing reaches NovelAI
        unbound = prompt_slots.apply(picture, {"characters": [{"from": "主体1", "use": {"set": "k1, v1"}}]})
        self.assertEqual(prompt_slots.render(unbound, lines)["character_prompts"][0]["prompt"],
                         "wakaba mutsumi, reclining")
        self.assertTrue(any("没有对应条目的主体" in n for n in prompt_slots.check(unbound, lines)))
        bound = prompt_slots.apply(picture, {"characters": [
            {"from": "主体1", "entry": "若叶睦", "use": {"set": "k1, v1"}, "look": {"tags": {"set": ""}}}]})
        self.assertEqual(bound["characters"][0]["from"], "若叶睦")
        self.assertEqual(bound["characters"][0]["key"], "主体1")
        self.assertEqual(prompt_slots.render(bound, lines)["character_prompts"][0]["prompt"],
                         "1girl, wakaba_mutsumi, tsukinomori_school_uniform, reclining")
        self.assertEqual(prompt_slots.check(bound, lines), [])

    def test_a_sentence_in_a_tags_half_is_noted(self):
        picture = prompt_slots.upgrade({"size": "portrait", "slots": {
            "count": {"tags": "1girl", "prose": ""},
            "scene": {"tags": "street, The sign hangs over the door.", "prose": ""}, "text": ""},
            "characters": [{"key": "主体1", "from": "", "use": [], "patches": [],
                            "look": {"tags": "black hair", "prose": ""},
                            "moment": {"tags": "standing, She looks up.", "prose": ""},
                            "center_x": None, "center_y": None}], "negative": ""})
        notes = prompt_slots.check(picture, {})
        self.assertTrue(any("scene的tags半边里写了句子" in n for n in notes), notes)
        self.assertTrue(any("主体1的moment的tags半边里写了句子" in n for n in notes), notes)
        # and the prompt still goes out as a tag list without the stop
        out = prompt_slots.render(picture, {})
        self.assertEqual(prompt_slots.lint(out, picture), [])
        self.assertIn("street, The sign hangs over the door, ", out["base_prompt"] + ", ")

    def test_the_line_writer_answering_with_no_lines_leaves_the_entry_alone(self):
        from core.services import library_lines
        conv = self._conv()
        r = self.c.post(f"/api/conversations/{conv.id}/characters",
                        json.dumps({"name": "林", "description": "黑发女孩",
                                    "tags": [{"part": "身份", "tags": "1girl, black hair"}]}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content[:200])
        ch = Character.objects.get(name="林", conversation=conv)
        before = ch.tags
        with mock.patch.object(llm, "write_lines", return_value=([], {})):
            self.assertFalse(library_lines.write(ch))
        ch.refresh_from_db()
        self.assertEqual(ch.tags, before)

    def test_a_shadow_copy_switches_on_only_the_costumes_that_were_on(self):
        folder = Folder.objects.create(name="项目")
        conv = self._conv(folder)
        project = Character.objects.create(
            folder=folder, name="林", description="黑发",
            tags=[{"id": "k1", "part": "身份", "tags": "1girl"}])
        a = CharacterVariant.objects.create(character=project, title="校服", position=1,
                                            tags=[{"id": "v1", "part": "衬衫", "tags": "white shirt"}])
        CharacterVariant.objects.create(character=project, title="便装", position=2,
                                        tags=[{"id": "v2", "part": "T恤", "tags": "t-shirt"}])
        conv.selected_variants.add(a)
        asked = Message.objects.create(conversation=conv, seq=1, role="user", content="加眼镜")
        proposals.record(conv, asked, {"settings": [], "characters": [{
            "name": "林", "scope": "conversation",
            "tags": [{"part": "身份", "tags": "1girl"}, {"part": "眼镜", "tags": "glasses"}],
            "variants": [],
        }]})
        own = Character.objects.get(name="林", conversation=conv)
        self.assertEqual([v.title for v in own.variants.order_by("position")], ["校服", "便装"])
        on = {v.title for v in conv.selected_variants.filter(character=own)}
        self.assertEqual(on, {"校服"})

    def test_a_costumes_identity_lines_all_move_to_the_character(self):
        conv = self._conv()
        r = self.c.post(f"/api/conversations/{conv.id}/characters",
                        json.dumps({"name": "林", "tags": [{"part": "身份", "tags": "1girl"}]}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content[:200])
        asked = Message.objects.create(conversation=conv, seq=1, role="user", content="记")
        proposals.record(conv, asked, {"settings": [], "characters": [{
            "name": "林", "scope": "conversation", "tags": [],
            "variants": [{"title": "校服", "tags": [
                {"part": "身份", "tags": "wakaba_mutsumi"}, {"part": "身份", "tags": "1girl"},
                {"part": "衬衫", "tags": "white shirt"}]}],
        }]})
        own = Character.objects.get(name="林", conversation=conv)
        parts = [(ln["part"], ln["tags"]) for ln in own.tags]
        self.assertIn(("身份", "wakaba_mutsumi"), parts)
        self.assertEqual(parts.count(("身份", "1girl")), 1)
        costume = own.variants.get(title="校服")
        self.assertEqual([ln["part"] for ln in costume.tags], ["衬衫"])

    def test_copy_from_reads_the_lines_the_picture_was_drawn_with(self):
        conv = self._conv()
        r = self.c.post(f"/api/conversations/{conv.id}/characters",
                        json.dumps({"name": "林", "tags": [{"part": "身份", "tags": "1girl, black hair"}]}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content[:200])
        snap = {"settings": [], "characters": [{
            "id": "x", "name": "林", "layer": "conversation",
            "tags": [{"id": "k1", "part": "身份", "tags": "1girl, red hair"}], "variants": []}]}
        Message.objects.create(
            conversation=conv, seq=1, role="assistant", nai_prompts={"draw_id": "abc123"},
            selection_snapshot=snap,
            prompt={**prompt_slots.empty(), "characters": [
                {"key": "林", "from": "林", "use": ["k1"], "patches": [], "look": "",
                 "moment": "standing", "center_x": None, "center_y": None}]})
        # the entry says black hair now; the picture was drawn with red hair
        self.assertEqual(pipeline._source_tags(conv, 2, {"picture": "abc123", "subject": "林"}),
                         "1girl, red hair")

    def test_copy_from_for_a_setting_reads_the_pictures_style_and_scene(self):
        conv = self._conv()
        snap = {"settings": [{"id": "s", "title": "黄昏", "layer": "conversation",
                              "tags": [{"id": "s1", "part": "画风", "tags": "painterly"},
                                       {"id": "s2", "part": "天色", "tags": "sunset"}]}],
                "characters": []}
        pic = prompt_slots.empty()
        pic["slots"]["style"] = {"tags": "film grain", "prose": ""}
        pic["slots"]["scene"] = {"tags": "street", "prose": "The street is empty."}
        pic["settings"] = [{"from": "黄昏", "use": ["s2"], "patches": []}]
        Message.objects.create(conversation=conv, seq=1, role="assistant",
                               nai_prompts={"draw_id": "abc123"}, selection_snapshot=snap, prompt=pic)
        got = pipeline._source_tags(conv, 2, {"picture": "abc123"}, kind="setting")
        self.assertEqual(got, "sunset, painterly, film grain, street, The street is empty.")

    def test_the_lead_is_not_shown_the_default_negative(self):
        from core.services import drawnote, novelai
        text = drawnote.describe_prompts({"base_prompt": "1girl", "character_prompts": [],
                                          "negative_prompt": novelai.merge_negative("blurry, text")})
        self.assertIn("negative_prompt: blurry, text", text)
        self.assertNotIn("lowres", text)

    def test_a_proposal_says_which_lines_an_update_dropped(self):
        conv = self._conv()
        r = self.c.post(f"/api/conversations/{conv.id}/settings",
                        json.dumps({"title": "黄昏", "tags": [{"part": "天色", "tags": "sunset"},
                                                              {"part": "云", "tags": "cloud"}]}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content[:200])
        asked = Message.objects.create(conversation=conv, seq=1, role="user", content="改")
        p = proposals.record(conv, asked, {"characters": [], "settings": [{
            "title": "黄昏", "scope": "conversation", "tags": [{"part": "天色", "tags": "sunset, dusk"}]}]})
        self.assertIn("「黄昏」少了1行：云", p.applied_summary)

    def test_copy_from_that_points_at_nothing_is_said_on_the_row(self):
        conv = self._conv()
        msg = Message.objects.create(conversation=conv, seq=3, role="assistant", status="pending")
        pipeline._resolve_copies(conv, msg, {"settings": [], "characters": [{
            "name": "林", "scope": "conversation", "copy_from": {"picture": "nope", "subject": "林"},
            "tags": [{"part": "身份", "tags": "1girl"}], "variants": []}]})
        msg.refresh_from_db()
        self.assertTrue(any("nope" in n and "找不到" in n for n in (msg.model_raw or {}).get("edit_notes") or []))

    def test_the_rail_hides_a_project_entry_a_conversation_entry_shadows(self):
        from core.views.projects import selection_payload
        folder = Folder.objects.create(name="项目")
        conv = self._conv(folder)
        Setting.objects.create(folder=folder, title="黄昏", tags=[{"id": "s1", "part": "天色", "tags": "sunset"}])
        Setting.objects.create(folder=folder, conversation=conv, title="黄昏",
                               tags=[{"id": "s1", "part": "天色", "tags": "dusk"}])
        Setting.objects.create(folder=folder, title="雨夜", tags=[{"id": "s1", "part": "天气", "tags": "rain"}])
        titles = [(s["title"], bool(s.get("conversation_id"))) for s in selection_payload(conv)["settings"]]
        self.assertEqual(sorted(titles), [("雨夜", False), ("黄昏", True)])

    def test_same_words_compares_what_novelai_receives_and_nothing_else(self):
        a = {"base_prompt": "1girl, street", "character_prompts": [{"prompt": "black hair"}],
             "negative_prompt": "", "n_images": 1, "draw_id": "aaa", "tier": "mid"}
        b = {"base_prompt": "1girl, street", "character_prompts": [{"prompt": "black hair", "center_x": None}],
             "negative_prompt": "", "n_images": 2, "draw_id": "bbb"}
        self.assertTrue(llm.same_words(a, b))
        self.assertFalse(llm.same_words(a, {**b, "base_prompt": "1girl, street, cloud"}))
        self.assertFalse(llm.same_words(a, {**b, "character_prompts": [{"prompt": "red hair"}]}))
        self.assertFalse(llm.same_words(a, None))

    def test_a_sentence_is_copied_by_what_it_says_not_word_for_word(self):
        source = ("orange cat, white fur, with a small lightning-shaped white patch above its left eye, "
                  "and its tail tip is pure white")
        lines, dropped = library.copied_lines(source, [
            {"part": "毛色", "tags": "orange cat, white fur"},
            {"part": "白斑", "tags": "a small white patch shaped like a lightning bolt above its left eye"},
            {"part": "尾巴", "tags": "the tip of its tail is pure white"},
            {"part": "眼睛", "tags": "amber eyes"},
        ])
        self.assertEqual([ln["tags"] for ln in lines], [
            "orange cat, white fur",
            "with a small lightning-shaped white patch above its left eye",
            "and its tail tip is pure white",
        ])
        self.assertEqual(dropped, ["amber eyes"])

    def test_a_character_recorded_without_a_costume_is_still_drawn_from(self):
        """A cat has no clothes, and the lead recorded none. The selection
        is made of costumes, so she was invisible to both models and never
        drawn from her lines; the write gives her the default costume."""
        conv = self._conv()
        msg = Message.objects.create(conversation=conv, seq=1, role="assistant", content="")
        proposals.record(conv, msg, {"settings": [], "characters": [{
            "name": "橘白猫", "scope": "conversation", "description": "橘白相间的猫",
            "tags": [{"part": "身份", "tags": "cat"}, {"part": "毛色", "tags": "orange cat, white fur"}],
            "variants": [],
        }]})
        cat = Character.objects.get(name="橘白猫", conversation=conv)
        self.assertEqual([v.title for v in cat.variants.all()], [ui_text.DEFAULT_VARIANT_TITLE])
        sel = library.selection_for(conv)
        self.assertIn("k2  毛色：orange cat, white fur", library.as_lines(sel))
        self.assertIn("角色「橘白猫」", library.as_prompt_text(sel))

    def test_an_outfits_lines_sent_as_the_characters_do_not_replace_her_own(self):
        """Told to add a scarf to an outfit, the model sent the outfit's lines
        as the character's tags too; her hair and eyes must survive that."""
        conv = self._conv()
        r = self.c.post(f"/api/conversations/{conv.id}/characters",
                        json.dumps({"name": "夏树", "tags": [{"part": "身份", "tags": "1girl"},
                                                           {"part": "头发", "tags": "black hair, ponytail"}]}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content[:200])
        char = Character.objects.get(name="夏树", conversation=conv)
        outfit = [{"part": "毛衣", "tags": "beige sweater"}, {"part": "围巾", "tags": "red scarf"}]
        proposals._write_character(
            {"name": "夏树", "tags": list(outfit), "variants": [{"title": "便服", "tags": outfit}]},
            folder=None, conversation=conv, existing=char, done=[])
        char.refresh_from_db()
        self.assertEqual([(ln["part"], ln["id"]) for ln in char.tags], [("身份", "k1"), ("头发", "k2")])
        self.assertEqual([ln["part"] for ln in char.variants.get(title="便服").tags], ["毛衣", "围巾"])

    def test_copy_from_takes_a_line_drawn_subjects_lines_but_not_its_look(self):
        """A library character's look is this picture's extras (wet hair), not
        what she looks like; only a subject without lines carries its
        appearance in look."""
        conv = self._conv()
        r = self.c.post(f"/api/conversations/{conv.id}/characters",
                        json.dumps({"name": "林", "tags": [{"part": "身份", "tags": "1girl, black hair"}]}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content[:200])
        Message.objects.create(
            conversation=conv, seq=1, role="assistant", nai_prompts={"draw_id": "abc123"},
            prompt={**prompt_slots.empty(), "characters": [
                {"key": "林", "from": "林", "use": ["k1"], "patches": [], "look": "wet hair",
                 "moment": "standing", "center_x": None, "center_y": None},
                {"key": "路人", "from": "", "use": [], "patches": [], "look": "red hat",
                 "moment": "walking", "center_x": None, "center_y": None},
                {"key": "老人", "from": "", "use": [], "patches": [],
                 "look": {"tags": "grey coat", "prose": "A rope is knotted at his waist."},
                 "moment": "sitting", "center_x": None, "center_y": None},
            ]})
        self.assertEqual(pipeline._source_tags(conv, 2, {"picture": "abc123", "subject": "林"}),
                         "1girl, black hair")
        self.assertEqual(pipeline._source_tags(conv, 2, {"picture": "abc123", "subject": "路人"}),
                         "red hat")
        # a sentence is handed to the lead set apart from the tags, not glued on with a comma
        self.assertEqual(pipeline._source_tags(conv, 2, {"picture": "abc123", "subject": "老人"}),
                         "grey coat, A rope is knotted at his waist.")

    def test_the_render_splices_lines_with_their_patches(self):
        picture = {
            **prompt_slots.empty(),
            "characters": [{
                "key": "林", "from": "林", "use": ["k1", "v1"],
                "patches": [{"line": "v1", "remove": "open cardigan", "add": "unworn cardigan"}],
                "look": "", "moment": "standing", "center_x": None, "center_y": None,
            }],
        }
        lines = {"林": {"k1": "1girl, black hair", "v1": "blue cardigan, open cardigan"}}
        out = prompt_slots.render(picture, lines)
        self.assertEqual(out["character_prompts"][0]["prompt"],
                         "1girl, black hair, blue cardigan, unworn cardigan, standing")

    def test_a_change_list_may_not_add_to_a_fast_changing_slot(self):
        picture = {**prompt_slots.empty(), "characters": []}
        with self.assertRaises(prompt_slots.PatchRejected):
            prompt_slots.apply(picture, {"camera": {"tags": {"add": "from above"}}})
        # a slot's change is written in halves; a bare operation names neither
        with self.assertRaises(prompt_slots.PatchRejected):
            prompt_slots.apply(picture, {"camera": {"set": "from above"}})
        # a sentence is added whole, never nudged a word at a time: `add` on
        # prose appends a sentence (with its stop), and a word is not an op
        added = prompt_slots.apply(picture, {"style": {"prose": {"add": "painted softly"}}})
        self.assertEqual(added["slots"]["style"]["prose"], "painted softly.")
        with self.assertRaises(prompt_slots.PatchRejected):
            prompt_slots.apply(picture, {"style": {"prose": {"nudge": "softly"}}})
        # a subject the picture does not have is added when the item brings
        # one; an item that only names it, or drops it, is refused
        with self.assertRaises(prompt_slots.PatchRejected):
            prompt_slots.apply(picture, {"characters": [{"from": "nobody"}]})
        with self.assertRaises(prompt_slots.PatchRejected):
            prompt_slots.apply(picture, {"characters": [{"from": "nobody", "drop": True}]})
        merged = prompt_slots.apply(picture, {
            "style": {"tags": {"add": "film grain"}, "prose": {"set": "the paint is thick"}},
            "negative": {"add": "text"},
        })
        self.assertEqual(merged["slots"]["style"], {"tags": "film grain", "prose": "the paint is thick"})
        self.assertEqual(merged["negative"], "text")

    def test_sentences_go_out_after_every_slots_tags_and_are_never_cut(self):
        """A sentence is a sentence because the model put it in prose: it goes
        out whole, commas and all, after the tags of every slot."""
        picture = prompt_slots.empty()
        picture["slots"]["camera"] = {"tags": "from above", "prose": ""}
        picture["slots"]["scene"] = {"tags": "library, indoors",
                                     "prose": "the shelves are tall, dusty and full of books"}
        picture["slots"]["light"] = {"tags": "backlighting", "prose": "light from the left"}
        picture["slots"]["text"] = "Text: Closed"
        out = prompt_slots.render(picture)
        self.assertEqual(
            out["base_prompt"],
            "from above, library, indoors, backlighting, "
            "the shelves are tall, dusty and full of books. light from the left. Text: Closed")

    def test_a_picture_from_before_prose_had_its_own_half_is_read_as_is(self):
        """An older row: a string per slot and a note slot. Nothing is split or
        moved -- the string is the tags, the note is other's prose."""
        old = {"size": "portrait",
               "slots": {"count": "1girl", "style": "", "camera": "", "scene": "library, a quiet room",
                         "light": "", "other": "", "note": "She looks at the camera.", "text": ""},
               "characters": [{"key": "林", "from": "林", "use": ["k1"], "patches": [],
                               "look": "wet hair", "moment": "standing", "center_x": None, "center_y": None}],
               "negative": "text"}
        new = prompt_slots.upgrade(old)
        self.assertEqual(new["slots"]["scene"], {"tags": "library, a quiet room", "prose": ""})
        self.assertEqual(new["slots"]["other"], {"tags": "", "prose": "She looks at the camera."})
        self.assertNotIn("note", new["slots"])
        self.assertEqual(new["characters"][0]["look"], {"tags": "wet hair", "prose": ""})
        self.assertEqual(new["settings"], [])
        self.assertEqual(prompt_slots.upgrade(new), new)
        out = prompt_slots.render(old, {"林": {"k1": "1girl, black hair"}})
        self.assertEqual(out["base_prompt"], "1girl, library, a quiet room, She looks at the camera.")
        self.assertEqual(out["character_prompts"][0]["prompt"], "1girl, black hair, wet hair, standing")

    def test_the_unknown_tag_check_skips_the_pieces_of_a_sentence(self):
        """A sentence with a comma in it is not several tags: with the picture
        at hand the check leaves its pieces alone and reports only real tags."""
        from core.services import llm
        picture = prompt_slots.empty()
        picture["slots"]["light"] = {"tags": "backlighting",
                                     "prose": "warm light falls on her cheek, her right hand, and the book"}
        rendered = prompt_slots.render(picture)
        with mock.patch.object(llm.danbooru, "unknown", side_effect=lambda tags: list(tags)):
            self.assertEqual(llm.unknown_tags(rendered, picture), ["backlighting"])
            # without the picture the short pieces of the sentence are taken for tags
            self.assertIn("her right hand", llm.unknown_tags(rendered))

    def test_a_picture_may_patch_a_style_line_for_itself_only(self):
        """The style slot is filled from the entry every time; a picture's
        patches on the style line change that picture alone, and an edit of a
        spliced picture is spliced again from the entry, so a remove is not
        undone by the words the previous picture carried."""
        style_lines = {"画风": {"s1": "painterly, muted colors, film grain"}}
        setting_lines = {"画风": {"s1": "painterly, muted colors, film grain"}}
        base = lambda pic: prompt_slots.render(pic, None, setting_lines, style_lines)["base_prompt"]  # noqa: E731
        first = prompt_slots.empty()
        first["slots"]["style"] = {"tags": "sketch lines", "prose": ""}
        self.assertEqual(base(first), "painterly, muted colors, film grain, sketch lines")
        # the picture keeps only the model's own words; the entry's go in at render
        self.assertEqual(first["slots"]["style"]["tags"], "sketch lines")
        # an edit of it: patch the style line, keep the model's own word
        second = prompt_slots.apply(first, {"settings": {"set": [
            {"from": "画风", "use": "", "patches": [{"line": "s1", "remove": "muted colors", "add": "vivid colors"}]}]}})
        self.assertEqual(base(second), "painterly, film grain, vivid colors, sketch lines")
        # a whole other style for one picture: set
        third = prompt_slots.apply(second, {"settings": {"set": [
            {"from": "画风", "use": "", "patches": [{"line": "s1", "set": "pencil sketch, monochrome"}]}]}})
        self.assertEqual(base(third), "pencil sketch, monochrome, sketch lines")
        # the patches dropped: back to the entry's line
        fourth = prompt_slots.apply(third, {"settings": {"set": []}})
        self.assertEqual(base(fourth), "painterly, muted colors, film grain, sketch lines")
        # a picture stored with the lines already in its style slot (the shape
        # before this) renders the same, and a remove on it still takes
        legacy = prompt_slots.empty()
        legacy["slots"]["style"] = {"tags": "painterly, muted colors, film grain, sketch lines", "prose": ""}
        self.assertEqual(base(legacy), "painterly, muted colors, film grain, sketch lines")
        self.assertEqual(base(prompt_slots.apply(legacy, {"settings": {"set": [
            {"from": "画风", "use": "", "patches": [{"line": "s1", "remove": "muted colors"}]}]}})),
            "painterly, film grain, sketch lines")
        # a patch on a line the setting does not have is reported
        notes = prompt_slots.check(third, None, None, {"画风": {"s2": "x"}})
        self.assertTrue(any("s1" in n for n in notes), notes)

    def test_a_settings_picked_lines_are_spliced_and_its_style_lines_come_by_themselves(self):
        picture = prompt_slots.empty()
        picture["slots"]["scene"] = {"tags": "indoors", "prose": ""}
        picture["settings"] = [{"from": "图书馆", "use": ["s2"]}]
        setting_lines = {"图书馆": {"s1": "painterly", "s2": "dark wood bookshelves, old books"}}
        out = prompt_slots.render(picture, None, setting_lines)
        self.assertEqual(out["base_prompt"], "dark wood bookshelves, old books, indoors")
        notes = prompt_slots.check(picture, None, None, {"图书馆": {"s1": "painterly"}})
        self.assertTrue(any("s2" in n for n in notes), notes)
        merged = prompt_slots.apply(picture, {"settings": {"set": [{"from": "图书馆", "use": "s1, s2"}]}})
        self.assertEqual(merged["settings"], [{"from": "图书馆", "use": ["s1", "s2"], "patches": []}])
        with self.assertRaises(prompt_slots.PatchRejected):
            prompt_slots.apply(picture, {"settings": {"add": "s1"}})

    def test_use_is_rewritten_whole_never_nudged(self):
        picture = {
            **prompt_slots.empty(),
            "characters": [{"key": "林", "from": "林", "use": ["k1", "v5"], "patches": [],
                            "look": "", "moment": "", "center_x": None, "center_y": None}],
        }
        with self.assertRaises(prompt_slots.PatchRejected):
            prompt_slots.apply(picture, {"characters": [{"from": "林", "use": {"remove": "v5"}}]})
        merged = prompt_slots.apply(picture, {"characters": [{"from": "林", "use": {"set": "k1"}}]})
        self.assertEqual(merged["characters"][0]["use"], ["k1"])

    def test_the_check_names_a_missing_identity_line(self):
        picture = {
            **prompt_slots.empty(),
            "characters": [{"key": "林", "from": "林", "use": ["k2"], "patches": [],
                            "look": "", "moment": "", "center_x": None, "center_y": None}],
        }
        notes = prompt_slots.check(picture, {"林": {"k1": "1girl", "k2": "black hair"}},
                                   {"林": {"k1": "身份", "k2": "头发"}})
        self.assertTrue(any("身份" in n for n in notes), notes)
