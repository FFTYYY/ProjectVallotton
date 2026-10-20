"""
The entry-based workflow, driven end to end through the real endpoints on the
stand-in provider: entries the lead writes into two layers, a picture
assembled from the entries it exposes, a change to it that inherits that
exposure and follows the main picture's seed, and a main picture the user
can move.

Nothing here reads a prompt file. What the stand-in decides, it decides from
the message it was sent (see providers/fake.py), so these pass and fail on the
app's behaviour alone.
"""

from __future__ import annotations

import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase, override_settings

from core.models import (
    Character, CharacterVariant, Conversation, Folder, LibraryProposal, Message, Setting,
)
from core import ui_text
from core.prompts import wording
from core.services import assemble, drawnote, library, llm, novelai, pipeline
from core.services.providers.fake import (
    EDIT_MARK, LOOKUP_MARK, PROJECT_MARK, STUB_CHARACTER_TAGS, STUB_PICTURE_TAGS,
    STUB_SETTING_TAGS, STUB_VARIANT_TAGS,
)

from .tests import _sync_jobs

#: the project layer's prefix, as the lead reads and writes it
P = library.PROJECT_PREFIX


def _tags(raw: str) -> list[str]:
    return library.split_tags(raw)


def _lead_setting(key: str, positive: str = "", **more) -> dict:
    """A setting as the lead writes one (llm._clean_entry's shape)."""
    return {"key": key, "category": "", "note": "", "positive": positive, "negative": "", "prose": "", **more}


def _lead_character(key: str, positive: str = "", variants: list[tuple[str, str]] | None = None, **more) -> dict:
    """A character as the lead writes one; `variants` are (title, positive) pairs."""
    return {
        "key": key, "note": "", "positive": positive, "negative": "", "prose": "",
        "variants": [{"key": t, "note": "", "positive": p, "negative": "", "prose": ""} for t, p in (variants or [])],
        **more,
    }


def _exposed(key: str, kind: str = "setting", **more) -> dict:
    """An exposed entry as library.entry_dict renders one, for the assembler."""
    e: dict = {"id": key, "kind": kind, "key": key, "layer": "conversation", "shown_key": key,
               "note": "", "positive": [], "negative": [], "prose": "", "origin_seq": None}
    if kind == "setting":
        e["category"] = ""
    if kind == "variant":
        e["character"] = key.split(library.VARIANT_SEP, 1)[0]
    e.update(more)
    return e


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
        return Conversation.objects.create(title="t", folder=folder)

    def _turn(self, conv: Conversation, text: str, n: int = 1) -> Message:
        """One turn through the real endpoint, however it ends."""
        with _sync_jobs(), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            r = self.c.post(f"/api/conversations/{conv.id}/messages",
                            {"text": text, "n_images": str(n), "reflection_rounds": "0"})
        self.assertEqual(r.status_code, 201, r.content[:200])
        msg = conv.messages.filter(role="assistant").order_by("-seq").first()
        assert msg is not None
        msg.refresh_from_db()
        return msg

    def _send(self, conv: Conversation, text: str, n: int = 1) -> Message:
        msg = self._turn(conv, text, n)
        self.assertEqual(msg.status, "done", msg.error)
        return msg

    def _row(self, conv: Conversation, msg: Message) -> dict:
        rows = self.c.get(f"/api/conversations/{conv.id}/messages").json()["messages"]
        return next(m for m in rows if m["id"] == str(msg.id))

    # ------------------------------------------------------------ pictures

    def test_a_picture_is_made_and_the_row_keeps_how_it_was_made(self):
        conv = self._conv()
        msg = self._send(conv, "画一张", n=2)
        self.assertEqual(msg.attachments.filter(kind="generated").count(), 2)
        self.assertEqual(msg.main_index, 0)
        assert msg.nai_prompts is not None
        self.assertTrue(msg.nai_prompts["draw_id"])
        # this picture's own tags, then the quality words; no entry, so no more
        self.assertEqual(msg.nai_prompts["base_prompt"],
                         ", ".join([*_tags(STUB_PICTURE_TAGS), novelai.QUALITY_WORDS, novelai.NO_TEXT_WORD]))
        self.assertEqual(msg.nai_prompts["character_prompts"], [])
        self.assertEqual(msg.nai_prompts["negative_prompt"], novelai.merge_negative(""))
        # what it was made from and made with, for the next picture and the inspector
        assert msg.prompt is not None
        self.assertEqual(set(msg.prompt),
                         {"description", "text", "size", "inherits", "exposed_keys", "exposed", "answer", "assembly"})
        self.assertEqual(msg.prompt["description"], "画一张")
        self.assertEqual((msg.prompt["inherits"], msg.prompt["exposed_keys"], msg.prompt["exposed"]), ("", [], []))
        self.assertEqual(msg.prompt["answer"]["positive"], _tags(STUB_PICTURE_TAGS))
        self.assertEqual(msg.prompt["assembly"], [])
        self.assertIsNone(msg.selection_snapshot, "nothing exposed, nothing to receipt")
        assert msg.draw_brief is not None
        self.assertEqual(msg.draw_brief["inherits"], "")
        raw = msg.model_raw or {}
        self.assertNotIn("compress", raw, "a prompt within the limit is painted as assembled")
        shown = self.c.get(f"/api/messages/{msg.id}/raw").json()
        self.assertEqual(shown["picture"], msg.prompt)
        self.assertEqual(shown["drawing"], msg.nai_prompts)
        self.assertIsNone(shown["compress"])
        self.assertEqual(self._row(conv, msg)["used_entries"], [])

    def test_an_entry_the_lead_writes_reaches_the_prompt_and_the_row_says_so(self):
        conv = self._conv()
        msg = self._send(conv, "把「黄昏」记下来，画一张")
        made = Setting.objects.get(title="黄昏")
        self.assertEqual((made.conversation_id, made.folder_id, made.origin_seq), (conv.id, None, msg.seq))
        self.assertEqual(made.positive, _tags(STUB_SETTING_TAGS))
        self.assertIn(made.id, set(conv.selected_settings.values_list("id", flat=True)))
        self.assertFalse(LibraryProposal.objects.filter(conversation=conv).exists(),
                         "the conversation's own entries wait for nobody")
        self.assertEqual((msg.model_raw or {}).get("edit_notes"), [ui_text.LIBRARY_WRITTEN.format(what="+ 黄昏")])
        # the entry's tags are in the prompt, after this picture's own and before the quality words
        base = (msg.nai_prompts or {})["base_prompt"]
        self.assertIn(STUB_SETTING_TAGS, base)
        self.assertLess(base.index(STUB_PICTURE_TAGS), base.index(STUB_SETTING_TAGS))
        self.assertLess(base.index(STUB_SETTING_TAGS), base.index(novelai.QUALITY_WORDS))
        # the row says what the picture was drawn from
        picture: dict = msg.prompt or {}
        self.assertEqual(picture["exposed_keys"], ["黄昏"])
        self.assertEqual([(e["key"], e["kind"], e["layer"], e["positive"]) for e in picture["exposed"]],
                         [("黄昏", "setting", "conversation", _tags(STUB_SETTING_TAGS))])
        came_to = {"key": "黄昏", "kind": "setting", "used": True,
                   "positive": _tags(STUB_SETTING_TAGS), "negative": [], "prose": ""}
        self.assertEqual(picture["assembly"], [came_to])
        snap = msg.selection_snapshot or {}
        self.assertEqual(set(snap), {"entries", "keys"})
        self.assertEqual(snap["keys"], ["黄昏"])
        self.assertEqual([e["key"] for e in snap["entries"]], ["黄昏"])
        row = self._row(conv, msg)
        self.assertEqual(row["used_entries"], [came_to])
        self.assertEqual(row["selection_snapshot"], snap)
        # and the lead reads it in full next turn, with where it came from: the
        # id of the drawing of the turn that wrote it, as the conversation shows it
        text = library.as_prompt_text(library.selection_for(conv))
        self.assertIn(STUB_SETTING_TAGS, text)
        self.assertIn(wording.ENTRY_FROM_LEAD.format(id=(msg.nai_prompts or {})["draw_id"]), text)

    def test_a_character_the_lead_writes_is_drawn_through_her_costume(self):
        conv = self._conv()
        msg = self._send(conv, "角色「林晚」站在窗前")
        her = Character.objects.get(name="林晚", conversation=conv)
        self.assertEqual((her.positive, her.origin_seq), (_tags(STUB_CHARACTER_TAGS), msg.seq))
        self.assertEqual([(v.title, v.positive) for v in her.variants.all()], [("日常装", _tags(STUB_VARIANT_TAGS))])
        self.assertEqual([v.title for v in conv.selected_variants.all()], ["日常装"])
        nai = msg.nai_prompts or {}
        self.assertEqual([(c["key"], c["prompt"], c["negative"]) for c in nai["character_prompts"]],
                         [("林晚", f"{STUB_CHARACTER_TAGS}, {STUB_VARIANT_TAGS}", "")])
        self.assertNotIn(STUB_CHARACTER_TAGS, nai["base_prompt"], "a character's words stay in her own segment")
        picture: dict = msg.prompt or {}
        self.assertEqual(picture["exposed_keys"], ["林晚", "林晚/日常装"], "a costume brings its character")
        self.assertEqual([(r["key"], r["kind"]) for r in picture["assembly"]],
                         [("林晚", "character"), ("林晚/日常装", "variant")])

    def test_writing_the_same_key_again_overrides_the_entry_in_place(self):
        conv = self._conv()
        first = self._send(conv, "把「黄昏」记下来，不用画")
        made = Setting.objects.get(title="黄昏", conversation=conv)
        self.assertEqual(made.origin_seq, first.seq)
        conv.selected_settings.remove(made)   # the user switches it off in between
        second = self._send(conv, "再把「黄昏」记下来一遍，不用画")
        self.assertEqual(Setting.objects.filter(title="黄昏").count(), 1, "same key, same row")
        again = Setting.objects.get(title="黄昏", conversation=conv)
        self.assertEqual(again.id, made.id)
        self.assertEqual(again.origin_seq, second.seq)
        self.assertNotEqual(again.content, made.content)
        self.assertIn(again.id, set(conv.selected_settings.values_list("id", flat=True)),
                      "an override switches the entry back on")
        self.assertEqual((second.model_raw or {}).get("edit_notes"), [ui_text.LIBRARY_WRITTEN.format(what="~ 黄昏")])
        self.assertIn(wording.ENTRY_FROM_LEAD_NO_DRAW, library.as_prompt_text(library.selection_for(conv)),
                      "written at a turn that drew nothing")

    def test_an_edit_inherits_the_exposure_and_follows_the_main_seed(self):
        conv = self._conv()
        first = self._send(conv, "把「黄昏」记下来，画一张", n=2)
        pictures = list(first.attachments.filter(kind="generated").order_by("position"))
        self.assertEqual(len(pictures), 2)

        # the second picture becomes the main one, named by its position
        r = self.c.post(f"/api/messages/{first.id}/main",
                        json.dumps({"index": pictures[1].position}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["messages"][0]["main_index"], pictures[1].position)

        second = self._send(conv, f"{EDIT_MARK}，角色「林晚」走进来", n=2)
        assert second.draw_brief is not None and second.prompt is not None
        draw_id = (first.nai_prompts or {})["draw_id"]
        self.assertEqual(second.draw_brief["inherits"], draw_id)
        self.assertEqual(second.prompt["inherits"], draw_id)
        # the lead named only what this picture adds; the rest came with the drawing
        self.assertEqual(second.draw_brief["expose"], ["林晚/日常装"])
        self.assertEqual(second.prompt["exposed_keys"], ["黄昏", "林晚", "林晚/日常装"])
        nai = second.nai_prompts or {}
        self.assertIn(STUB_SETTING_TAGS, nai["base_prompt"])
        self.assertEqual([c["prompt"] for c in nai["character_prompts"]], [f"{STUB_CHARACTER_TAGS}, {STUB_VARIANT_TAGS}"])
        # the first picture of the edit follows the main picture's seed; the second rolls its own
        seeds = [a.seed for a in second.attachments.filter(kind="generated").order_by("position")]
        self.assertEqual(seeds[0], pictures[1].seed)
        self.assertNotEqual(seeds[1], pictures[1].seed)

    def test_an_edit_that_sends_the_same_words_again_is_refused(self):
        """The lead asked for a change; a picture that sends NovelAI the very
        words of the picture it continues is the same picture paid for twice."""
        conv = self._conv()
        self._send(conv, "把「黄昏」记下来，画一张")
        second = self._turn(conv, f"{EDIT_MARK}：再亮一点")
        self.assertEqual(second.status, "error")
        self.assertIn(ui_text.EDIT_CHANGED_NOTHING, second.error)
        self.assertEqual(second.attachments.filter(kind="generated").count(), 0)

    def test_inheriting_a_drawing_without_a_stored_exposure_draws_new_but_follows_its_seed(self):
        """A drawing from before exposures were stored cannot be continued,
        only followed: the picture is drawn new under its main seed."""
        conv = self._conv()
        first = self._send(conv, "把「黄昏」记下来，画一张")
        Message.objects.filter(id=first.id).update(prompt=None)
        main = first.attachments.filter(kind="generated").order_by("position").first()
        assert main is not None
        second = self._send(conv, f"{EDIT_MARK}：再亮一点")
        assert second.draw_brief is not None and second.prompt is not None
        self.assertEqual(second.draw_brief["inherits"], (first.nai_prompts or {})["draw_id"])
        self.assertEqual(second.prompt["exposed_keys"], [])
        self.assertNotIn(STUB_SETTING_TAGS, (second.nai_prompts or {})["base_prompt"])
        got = second.attachments.filter(kind="generated").order_by("position").first()
        assert got is not None
        self.assertEqual(got.seed, main.seed)

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
        msg = self._send(conv, "画一张", n=2)
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

    def test_an_exposure_nobody_answers_to_and_an_inherits_nobody_made_are_said_on_the_row(self):
        conv = self._conv()
        first = self._send(conv, "画一张")
        main = first.attachments.filter(kind="generated").order_by("position").first()
        assert main is not None
        msg = Message.objects.create(conversation=conv, seq=first.seq + 2, role="assistant",
                                     status="pending", force_n_images=1)
        spec = {"description": "窗边", "expose": ["不存在"], "inherits": "nope", "text": "", "size": ""}
        ctx = pipeline._draw_context(conv, msg, spec, {})
        self.assertEqual(ctx.seed, main.seed, "an id nobody made: drawn new, following the latest drawing")
        self.assertIsNone(ctx.source_prompts)
        self.assertEqual(ctx.exposure.unknown, ["不存在"])
        self.assertEqual(ctx.exposure.keys, [])
        pipeline._prompts_and_paint(msg, spec, ctx)
        msg.refresh_from_db()
        self.assertIn(ui_text.EXPOSE_UNKNOWN.format(keys="不存在"), (msg.model_raw or {}).get("draw_notes") or [])

    def test_the_history_tells_the_lead_what_it_wrote_and_what_each_drawing_exposed(self):
        conv = self._conv()
        msg = self._send(conv, "把「黄昏」记下来，画一张")
        history = pipeline.build_history(conv, before_seq=msg.seq + 1)
        said = json.loads(next(m["content"] for m in history if m["role"] == "assistant"))
        self.assertEqual(set(said), {"reply", "settings", "characters", "edits", "draw"})
        self.assertEqual([s["key"] for s in said["settings"]], ["黄昏"])
        self.assertEqual(said["draw"]["expose"], ["黄昏"])
        notes = [p["text"] for m in history if m["role"] == "user" and isinstance(m["content"], list)
                 for p in m["content"] if p.get("type") == "text"]
        self.assertTrue(any(wording.APP_NOTE_EXPOSED.format(keys="黄昏") in t for t in notes), notes)
        self.assertTrue(any(wording.SYSTEM_NOTE_DRAW.format(id=(msg.nai_prompts or {})["draw_id"]) in t
                            for t in notes), notes)

    # ------------------------------------------------------------ the library

    def test_outside_a_project_a_project_edit_lands_in_this_conversation(self):
        """No project: an edit has nowhere to go but this conversation's layer."""
        conv = self._conv()
        self._send(conv, f"把「黄昏」记下来{PROJECT_MARK}，不用画")
        made = Setting.objects.get(title="黄昏")
        self.assertEqual((made.conversation_id, made.folder_id), (conv.id, None))
        self.assertEqual(Setting.objects.count(), 1)
        self.assertFalse(LibraryProposal.objects.filter(conversation=conv).exists())
        self.assertIn(made.id, set(conv.selected_settings.values_list("id", flat=True)))

    def test_in_a_project_a_project_edit_waits_for_approval_while_the_conversations_entry_is_written(self):
        folder = Folder.objects.create(name="项目")
        conv = self._conv(folder)
        msg = self._send(conv, f"把「黄昏」记下来{PROJECT_MARK}，不用画")
        own = Setting.objects.get(title="黄昏")
        self.assertEqual((own.conversation_id, own.folder_id), (conv.id, None))
        self.assertFalse(Setting.objects.filter(folder=folder, title="黄昏").exists())
        p = LibraryProposal.objects.get(conversation=conv)
        self.assertEqual((p.status, p.message_id, p.folder_id), ("pending", msg.id, folder.id))
        self.assertEqual([s["key"] for s in p.payload["settings"]], [P + "黄昏"])
        self.assertEqual(p.payload["settings"][0]["positive"], _tags(STUB_SETTING_TAGS))

    def test_in_a_project_the_leads_own_entries_are_still_the_conversations(self):
        folder = Folder.objects.create(name="项目")
        conv = self._conv(folder)
        self._send(conv, "把「黄昏」记下来，不用画")
        made = Setting.objects.get(title="黄昏")
        self.assertEqual((made.conversation_id, made.folder_id), (conv.id, None))
        self.assertFalse(LibraryProposal.objects.filter(conversation=conv).exists())

    def test_approving_promotes_the_conversations_entry_in_place_and_keeps_it_on(self):
        """The lead wrote it down to draw from it here; approved, the same row
        becomes the project's, still switched on in the conversation that made it."""
        folder = Folder.objects.create(name="项目")
        conv = self._conv(folder)
        self._send(conv, f"把「黄昏」记下来{PROJECT_MARK}，不用画")
        own = Setting.objects.get(title="黄昏")
        p = LibraryProposal.objects.get(conversation=conv)
        r = self.c.post(f"/api/proposals/{p.id}/resolve", json.dumps({"approve": True}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["status"], "applied")
        self.assertEqual(r.json()["applied_summary"], ui_text.PROPOSAL_UPDATED_SETTING.format(title="黄昏"))
        self.assertEqual(Setting.objects.filter(title="黄昏").count(), 1, "promoted, not copied")
        moved = Setting.objects.get(id=own.id)
        self.assertEqual((moved.folder_id, moved.conversation_id, moved.origin_seq), (folder.id, None, None))
        self.assertEqual(moved.positive, _tags(STUB_SETTING_TAGS))
        self.assertIn(moved.id, set(conv.selected_settings.values_list("id", flat=True)))
        self.assertIn(P + "黄昏", library.as_prompt_text(library.selection_for(conv)))

    def test_a_project_edit_is_exposed_in_its_edited_form_before_anyone_approves_it(self):
        folder = Folder.objects.create(name="项目")
        conv = self._conv(folder)
        project = Setting.objects.create(folder=folder, title="黄昏", content="旧的", category="光", positive=["sunset"])
        conv.selected_settings.add(project)   # the person switched it on
        msg = Message.objects.create(conversation=conv, seq=2, role="assistant", status="pending", force_n_images=1)
        edit = _lead_setting(P + "黄昏", "dusk, pink sky", category="光")
        result = {"reply": "", "settings": [], "characters": [],
                  "edits": {"settings": [edit], "characters": []},
                  "draw": {"description": "黄昏里的一个人", "expose": [P + "黄昏"], "inherits": "", "text": "", "size": ""}}
        overlay = pipeline._write_library(conv, msg, result)
        self.assertEqual(LibraryProposal.objects.get(conversation=conv).status, "pending")
        self.assertEqual(Setting.objects.get(id=project.id).positive, ["sunset"], "nothing is written until approved")
        self.assertEqual(set(overlay), {"黄昏"})
        ctx = pipeline._draw_context(conv, msg, result["draw"], overlay)
        self.assertEqual(ctx.exposure.keys, [P + "黄昏"])
        self.assertEqual(ctx.exposure.entries[0]["positive"], ["dusk", "pink sky"])
        self.assertTrue(ctx.exposure.entries[0].get("edited_this_turn"))
        self.assertIn(project.id, set(conv.selected_settings.values_list("id", flat=True)),
                      "exposing a project entry switches it on")
        nai_prompts, images = pipeline._prompts_and_paint(msg, result["draw"], ctx)
        self.assertIn("dusk, pink sky", nai_prompts["base_prompt"])
        self.assertNotIn("sunset", nai_prompts["base_prompt"])
        self.assertEqual(len(images), 1)
        msg.refresh_from_db()
        assert msg.prompt is not None
        self.assertEqual(msg.prompt["exposed_keys"], [P + "黄昏"])
        self.assertEqual(msg.prompt["exposed"][0]["positive"], ["dusk", "pink sky"])

    def test_an_exposure_list_adds_to_what_it_inherits_and_takes_keys_away_with_a_minus(self):
        conv = self._conv()
        library.write_conversation_entries(
            conv, 1, [_lead_setting("黄昏", "sunset"), _lead_setting("夜景", "night")],
            [_lead_character("林", "1girl", [("校服", "serafuku")])])
        got = library.resolve_exposure(conv, [library.REMOVE_PREFIX + "黄昏", "林/校服"], inherited=["黄昏", "夜景"])
        self.assertEqual(got.keys, ["夜景", "林", "林/校服"])
        self.assertEqual(got.unknown, [])
        self.assertEqual([e["positive"] for e in got.entries], [["night"], ["1girl"], ["serafuku"]])
        # a key named twice is exposed once; a key nobody has is reported, not invented
        got = library.resolve_exposure(conv, ["黄昏", "不存在", "林/没有的造型"], inherited=["黄昏"])
        self.assertEqual(got.keys, ["黄昏", "林"])
        self.assertEqual(got.unknown, ["不存在", "林/没有的造型"])
        self.assertEqual(library.exposure_text(got.entries).count("◆ "), 2)
        self.assertEqual(library.exposure_text([]), wording.EXPOSURE_NONE)

    def test_a_project_entry_is_named_with_its_prefix_and_an_edit_of_it_comes_before_the_exposure(self):
        folder = Folder.objects.create(name="项目")
        conv = self._conv(folder)
        project = Setting.objects.create(folder=folder, title="画风", content="厚涂", category="画风", positive=["painterly"])
        conv.selected_settings.add(project)
        for key in (P + "画风", "画风"):
            got = library.resolve_exposure(conv, [key])
            self.assertEqual([(e["shown_key"], e["layer"]) for e in got.entries], [(P + "画风", "project")], key)
        overlay = library.overlay_from_edits(
            {"settings": [_lead_setting(P + "画风", "watercolor", note="水彩")], "characters": []})
        got = library.resolve_exposure(conv, [P + "画风"], overlay=overlay)
        e = got.entries[0]
        self.assertEqual((e["positive"], e["note"], e["category"]), (["watercolor"], "水彩", "画风"))
        self.assertTrue(e.get("edited_this_turn"))
        self.assertEqual(Setting.objects.get(id=project.id).positive, ["painterly"])
        # the conversation's own entry of that name is another key, and is left alone
        library.write_conversation_entries(conv, 1, [_lead_setting("画风", "sketch")], [])
        got = library.resolve_exposure(conv, ["画风", P + "画风"], overlay=overlay)
        self.assertEqual([(e["shown_key"], e["positive"]) for e in got.entries],
                         [("画风", ["sketch"]), (P + "画风", ["watercolor"])])
        # the drawing model reads the keys plainly: it has one layer in front of it
        self.assertIn("◆ 画风（", library.exposure_text(got.entries))
        self.assertNotIn(P, library.exposure_text(got.entries))

    def test_the_lead_sees_only_what_the_person_has_switched_on(self):
        """A project character this conversation has not switched on is not in
        the lead's library at all, not even by name; naming her finds nothing
        and switches nothing on. Once the person switches one of her variants
        on, the lead reads her and that variant in full -- and only that one."""
        folder = Folder.objects.create(name="项目")
        conv = self._conv(folder)
        her = Character.objects.create(folder=folder, name="瓦洛东小姐", description="银发的画家",
                                       positive=["1girl", "silver hair"])
        casual = CharacterVariant.objects.create(character=her, title="便服", position=1, positive=["sweater"])
        CharacterVariant.objects.create(character=her, title="礼服", position=2, positive=["dress"])
        style = Setting.objects.create(folder=folder, title="画风", content="厚涂", positive=["painterly"])
        text = library.as_prompt_text(library.selection_for(conv))
        self.assertNotIn("瓦洛东小姐", text)
        self.assertNotIn("画风", text)
        self.assertIn(wording.LIBRARY_EMPTY, text)

        got = library.resolve_exposure(conv, ["瓦洛东小姐", P + "瓦洛东小姐/便服", P + "画风"])
        self.assertEqual(got.keys, [])
        self.assertEqual(got.unknown, ["瓦洛东小姐", P + "瓦洛东小姐/便服", P + "画风"])
        self.assertFalse(conv.selected_variants.exists())
        self.assertFalse(conv.selected_settings.exists())
        answer = {"draw": {"expose": [P + "瓦洛东小姐/便服", P + "画风"]}}
        self.assertEqual(library.unwritten_exposure(conv, answer), [P + "瓦洛东小姐/便服", P + "画风"])

        conv.selected_variants.add(casual)
        conv.selected_settings.add(style)
        text = library.as_prompt_text(library.selection_for(conv))
        self.assertIn("silver hair", text)
        self.assertIn(P + "瓦洛东小姐/便服", text)
        self.assertNotIn("礼服", text, "a variant left off stays out of sight")
        got = library.resolve_exposure(conv, [P + "瓦洛东小姐/便服", P + "瓦洛东小姐/礼服", P + "画风"])
        self.assertEqual(got.keys, [P + "瓦洛东小姐", P + "瓦洛东小姐/便服", P + "画风"])
        self.assertEqual(got.unknown, [P + "瓦洛东小姐/礼服"])
        self.assertEqual(sorted(v.title for v in conv.selected_variants.all()), ["便服"])
        self.assertEqual(library.unwritten_exposure(conv, {"draw": {"expose": [P + "画风", P + "瓦洛东小姐/礼服"]}}),
                         [P + "瓦洛东小姐/礼服"])

    def test_a_branch_rebuilds_the_conversation_layer_as_it_was_at_that_message(self):
        """A branch at an earlier message starts with the entries that point
        in the story had -- not the ones added later, and not none. (That
        point is the moment the next message was sent: see the test below
        for what a hand edit in between does.)"""
        conv = self._conv()
        first = self._send(conv, "把「黄昏」记下来，不用画")
        second = self._send(conv, "把「夜景」记下来，不用画")
        r = self.c.post(f"/api/messages/{first.id}/branch")
        self.assertEqual(r.status_code, 201, r.content[:200])
        early = Conversation.objects.get(id=r.json()["id"])
        self.assertEqual(list(early.own_settings.values_list("title", flat=True)), ["黄昏"])
        self.assertEqual(list(early.selected_settings.values_list("title", flat=True)), ["黄昏"])
        # a copy of the entry as it read, and the source untouched
        src = Setting.objects.get(conversation=conv, title="黄昏")
        copy = early.own_settings.get()
        self.assertEqual((copy.positive, copy.content, copy.origin_seq), (src.positive, src.content, src.origin_seq))
        self.assertEqual(set(conv.own_settings.values_list("title", flat=True)), {"黄昏", "夜景"})
        r = self.c.post(f"/api/messages/{second.id}/branch")
        late = Conversation.objects.get(id=r.json()["id"])
        self.assertEqual(set(late.own_settings.values_list("title", flat=True)), {"黄昏", "夜景"})

    def test_a_branch_at_an_earlier_message_undoes_later_edits_and_deletions_of_the_layer(self):
        """The conversation layer at that message, not as it reads now: an
        entry edited later comes back as it was, an entry deleted later
        comes back at all, and what was switched on then is switched on."""
        conv = self._conv()
        first = self._send(conv, "把「黄昏」记下来，不用画")
        dusk = Setting.objects.get(conversation=conv, title="黄昏")
        then = (dusk.content, dusk.positive)
        # later: the user rewrites it by hand, the lead adds another, and the user deletes the first
        r = self.c.patch(f"/api/settings/{dusk.id}", json.dumps({"content": "改成深夜", "positive": "midnight"}),
                         content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content[:200])
        second = self._send(conv, "把「夜景」记下来，不用画")
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
        self.assertEqual((got.title, got.content, got.positive), ("黄昏", "改成深夜", ["midnight"]))
        self.assertNotEqual(then, (got.content, got.positive))
        self.assertEqual(list(early.selected_settings.values_list("title", flat=True)), ["黄昏"])
        # a branch at the second (latest) message starts from now: 黄昏 is gone, 夜景 is there
        r = self.c.post(f"/api/messages/{second.id}/branch")
        late = Conversation.objects.get(id=r.json()["id"])
        self.assertEqual(set(late.own_settings.values_list("title", flat=True)), {"夜景"})
        # the source conversation itself is untouched by either branch
        self.assertEqual(set(conv.own_settings.values_list("title", flat=True)), {"夜景"})

    def test_a_branch_keeps_the_turn_numbers_its_entries_name(self):
        """An entry says which turn wrote it by seq, and the lead is told the
        drawing that turn made. A rerun leaves a gap in the seqs; a branch that
        numbered its copy afresh pointed the entry at the wrong turn, or at
        none, and the lead lost where its own entry came from."""
        conv = Conversation.objects.create(title="c")
        def row(**kw):
            return Message.objects.create(conversation=conv, status="done", **kw)
        row(seq=1, role="user", content="画一张")
        row(seq=2, role="assistant", content="旧的", archived=True, nai_prompts={"draw_id": "aaaaaa", "base_prompt": "x"})
        row(seq=3, role="assistant", content="好", nai_prompts={"draw_id": "bbbbbb", "base_prompt": "x"})
        row(seq=4, role="user", content="记下天台")
        last = row(seq=5, role="assistant", content="好", nai_prompts={"draw_id": "cccccc", "base_prompt": "x"})
        library.write_conversation_entries(conv, 5, [{"key": "天台", "note": "天台", "positive": "rooftop"}], [])
        mark = wording.ENTRY_FROM_LEAD.format(id="cccccc")
        self.assertIn(mark, library.as_prompt_text(library.selection_for(conv)))
        r = self.c.post(f"/api/messages/{last.id}/branch")
        self.assertEqual(r.status_code, 201, r.content[:200])
        branch = Conversation.objects.get(id=r.json()["id"])
        self.assertEqual(list(branch.messages.order_by("seq").values_list("seq", flat=True)), [1, 3, 4, 5])
        self.assertIn(mark, library.as_prompt_text(library.selection_for(branch)))

    def test_a_branch_of_a_history_without_snapshots_copies_the_layer_as_it_is_now(self):
        conv = self._conv()
        msg = self._send(conv, "把「黄昏」记下来，不用画")
        Message.objects.filter(conversation=conv).update(library_snapshot=None, library_before=None)
        r = self.c.post(f"/api/messages/{msg.id}/branch")
        branch = Conversation.objects.get(id=r.json()["id"])
        self.assertEqual(list(branch.own_settings.values_list("title", flat=True)), ["黄昏"])

    def test_a_new_conversation_in_a_project_starts_with_nothing_switched_on(self):
        """The project's settings used to come on by themselves; the user
        asked for an empty start (2026-10-17). They are listed to the lead,
        who switches one on by exposing it, and to the user, who ticks."""
        folder = Folder.objects.create(name="项目")
        Setting.objects.create(folder=folder, title="画风", content="厚涂", positive=["painterly"])
        r = self.c.post("/api/conversations", json.dumps({"folder_id": str(folder.id)}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content[:200])
        conv = Conversation.objects.get(id=r.json()["id"])
        self.assertEqual(list(conv.selected_settings.all()), [])
        # and one moved into the project later starts the same way
        other = Conversation.objects.create(title="t")
        r = self.c.post(f"/api/conversations/{other.id}/folder", json.dumps({"folder_id": str(folder.id)}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(list(other.selected_settings.all()), [])

    def test_both_layers_are_listed_to_the_lead_and_a_conversation_entry_can_be_promoted(self):
        folder = Folder.objects.create(name="项目")
        conv = self._conv(folder)
        project = Setting.objects.create(folder=folder, title="画风", content="厚涂")
        own = Setting.objects.create(conversation=conv, title="画风", content="水彩")
        conv.selected_settings.add(project, own)
        # two keys, two layers: both are read, each said to be where it is
        text = library.as_prompt_text(library.selection_for(conv))
        self.assertIn("厚涂", text)
        self.assertIn("水彩", text)
        self.assertIn(wording.ENTRY_FROM_PROJECT, text)
        self.assertIn(wording.ENTRY_FROM_USER, text)
        self.assertIn(P + "画风", text)

        r = self.c.post(f"/api/settings/{own.id}/promote")
        self.assertEqual(r.status_code, 400, "the project already has that title")
        project.delete()
        r = self.c.post(f"/api/settings/{own.id}/promote")
        self.assertEqual(r.status_code, 200)
        own.refresh_from_db()
        self.assertIsNone(own.conversation_id)
        self.assertEqual(own.folder_id, folder.id)

    def test_a_proposal_cannot_promote_onto_a_project_entry_of_the_same_name(self):
        """What the 「存到项目」 button refuses, the 「写入资料库」 button refuses
        too; it used to crash on the layer's unique title."""
        folder = Folder.objects.create(name="项目")
        conv = self._conv(folder)
        Setting.objects.create(folder=folder, title="画风", content="厚涂")
        own = Setting.objects.create(conversation=conv, title="画风", content="水彩")
        Character.objects.create(folder=folder, name="林", description="项目里的")
        mine = Character.objects.create(conversation=conv, name="林", description="对话里的")
        turn = Message.objects.create(conversation=conv, seq=1, role="assistant", status="done")
        p = LibraryProposal.objects.create(conversation=conv, folder=folder, message=turn, payload={
            "settings": [_lead_setting(P + "画风", note="新的")],
            "characters": [_lead_character(P + "林", note="新的")],
        })
        r = self.c.post(f"/api/proposals/{p.id}/resolve", json.dumps({"approve": True}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 409, r.content[:300])
        self.assertEqual(r.json()["error"], ui_text.PROPOSAL_NAME_TAKEN.format(name="画风"))
        p.refresh_from_db()
        self.assertEqual(p.status, "pending", "still waiting: tidy the project and press again")
        # nothing moved, nothing doubled, nothing overwritten
        self.assertEqual(Setting.objects.filter(folder=folder, conversation__isnull=True, title="画风").count(), 1)
        self.assertEqual(Setting.objects.get(conversation__isnull=True, title="画风").content, "厚涂")
        own.refresh_from_db()
        self.assertEqual(own.conversation_id, conv.id)
        self.assertEqual(Character.objects.filter(folder=folder, conversation__isnull=True, name="林").count(), 1)
        mine.refresh_from_db()
        self.assertEqual(mine.conversation_id, conv.id)

    def test_the_project_page_does_not_list_a_conversation_entry_written_inside_it(self):
        """An entry the lead wrote for a conversation that sits in the project
        is not the project's own; the project page used to list and edit it
        as such, and refuse a project entry of that title."""
        folder = Folder.objects.create(name="项目")
        conv = self._conv(folder)
        Setting.objects.create(conversation=conv, title="雨夜", content="本对话的")
        Character.objects.create(conversation=conv, name="阿岚")
        lib = self.c.get(f"/api/folders/{folder.id}/library").json()
        self.assertEqual([s["title"] for s in lib["settings"]], [])
        self.assertEqual([c["name"] for c in lib["characters"]], [])
        r = self.c.post(f"/api/folders/{folder.id}/settings", json.dumps({"title": "雨夜", "content": "项目的"}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content[:200])
        lib = self.c.get(f"/api/folders/{folder.id}/library").json()
        self.assertEqual([(s["title"], s["content"]) for s in lib["settings"]], [("雨夜", "项目的")])

    def test_the_selection_lists_both_layers(self):
        folder = Folder.objects.create(name="项目")
        conv = self._conv(folder)
        Character.objects.create(folder=folder, name="林")
        r = self.c.post(f"/api/conversations/{conv.id}/characters",
                        json.dumps({"name": "猫", "positive": "cat, orange cat", "prose": "A cat."}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content[:200])
        self.assertEqual((r.json()["positive"], r.json()["prose"]), (["cat", "orange cat"], "A cat."))
        self.assertEqual([v["title"] for v in r.json()["variants"]], [ui_text.DEFAULT_VARIANT_TITLE])
        payload = self.c.get(f"/api/conversations/{conv.id}/selection").json()
        names = {(c["name"], c["conversation_id"] is not None) for c in payload["characters"]}
        self.assertEqual(names, {("林", False), ("猫", True)})

    def test_a_character_recorded_without_a_costume_is_still_drawn_from(self):
        """A cat has no clothes, and the lead recorded none. The selection is
        made of costumes, so she would be invisible to both models; the write
        gives her the default costume."""
        conv = self._conv()
        library.write_conversation_entries(
            conv, 1, [], [_lead_character("橘白猫", "cat, orange cat, white fur", note="橘白相间的猫")])
        cat = Character.objects.get(name="橘白猫", conversation=conv)
        self.assertEqual([v.title for v in cat.variants.all()], [ui_text.DEFAULT_VARIANT_TITLE])
        self.assertEqual([v.title for v in conv.selected_variants.all()], [ui_text.DEFAULT_VARIANT_TITLE])
        text = library.as_prompt_text(library.selection_for(conv))
        self.assertIn("橘白猫", text)
        self.assertIn("orange cat", text)
        got = library.resolve_exposure(conv, ["橘白猫"])
        self.assertEqual(got.keys, ["橘白猫"])
        made = assemble.assemble(got.entries, assemble.clean_answer({"subjects": [{"key": "橘白猫"}]}))
        self.assertEqual([c["prompt"] for c in made["nai_prompts"]["character_prompts"]], ["cat, orange cat, white fur"])

    def test_the_lead_is_shown_each_subjects_negative_but_not_the_default_one(self):
        text = drawnote.describe_prompts({
            "base_prompt": "1girl",
            "character_prompts": [{"key": "林", "prompt": "black hair", "negative": "child"}],
            "negative_prompt": novelai.merge_negative("blurry, text"),
        })
        self.assertIn("negative_prompt: blurry, text", text)
        self.assertIn("character[林] negative: child", text)
        self.assertNotIn("lowres", text)

    def test_the_rail_lists_both_layers_even_under_one_name(self):
        """The lead is listed the project's 「@黄昏」 and the conversation's
        「黄昏」 as two entries (library.selection_for), so the rail shows the
        same two: what the lead can be told to expose, the person can see."""
        from core.views.projects import selection_payload
        folder = Folder.objects.create(name="项目")
        conv = self._conv(folder)
        Setting.objects.create(folder=folder, title="黄昏", positive=["sunset"])
        Setting.objects.create(conversation=conv, title="黄昏", positive=["dusk"])
        Setting.objects.create(folder=folder, title="雨夜", positive=["rain"])
        titles = [(s["title"], bool(s.get("conversation_id"))) for s in selection_payload(conv)["settings"]]
        self.assertEqual(sorted(titles), [("雨夜", False), ("黄昏", False), ("黄昏", True)])

    def test_same_words_compares_what_novelai_receives_and_nothing_else(self):
        a = {"base_prompt": "1girl, street", "character_prompts": [{"prompt": "black hair", "negative": ""}],
             "negative_prompt": "", "n_images": 1, "draw_id": "aaa", "tier": "mid"}
        b = {"base_prompt": "1girl, street", "character_prompts": [{"prompt": "black hair", "center_x": None}],
             "negative_prompt": "", "n_images": 2, "draw_id": "bbb"}
        self.assertTrue(llm.same_words(a, b))
        self.assertFalse(llm.same_words(a, {**b, "base_prompt": "1girl, street, cloud"}))
        self.assertFalse(llm.same_words(a, {**b, "character_prompts": [{"prompt": "red hair"}]}))
        self.assertFalse(llm.same_words(a, {**b, "character_prompts": [{"prompt": "black hair", "negative": "child"}]}),
                         "a subject's negative is sent too")
        self.assertFalse(llm.same_words(a, None))


class AssemblyTests(SimpleTestCase):
    """What NovelAI gets out of the exposed entries and the drawing model's
    answer: the one way the pieces go together."""

    def test_a_style_setting_leads_and_a_character_is_her_entry_plus_her_costume(self):
        exposed = [
            _exposed("图书馆", category="场景", positive=["library"], prose="Warm light comes through the window."),
            _exposed("画风", category="画风", positive=["painterly"]),
            _exposed("林", "character", positive=["1girl", "black hair"], negative=["child"]),
            _exposed("林/校服", "variant", positive=["blue cardigan", "open cardigan"], negative=["long hair"]),
        ]
        answer = assemble.clean_answer({
            "entries": [], "positive": "1girl, solo", "negative": "blurry", "prose": "She looks up.",
            "subjects": [{"key": "林", "center_x": 0.3, "center_y": 0.6}], "size": "landscape",
        })
        made = assemble.assemble(exposed, answer, text="Closed")
        nai = made["nai_prompts"]
        self.assertEqual(
            nai["base_prompt"],
            "1girl, solo, painterly, library, very aesthetic, masterpiece, "
            "Warm light comes through the window. She looks up. Text: Closed")
        self.assertEqual(nai["character_prompts"], [{
            "key": "林", "prompt": "1girl, black hair, blue cardigan, open cardigan",
            "negative": "child, long hair", "center_x": 0.3, "center_y": 0.6,
        }])
        self.assertEqual(nai["negative_prompt"], novelai.merge_negative("blurry"))
        self.assertEqual(nai["size"], "landscape")
        self.assertEqual([(r["key"], r["kind"], r["used"]) for r in made["assembly"]],
                         [("图书馆", "setting", True), ("画风", "setting", True),
                          ("林", "character", True), ("林/校服", "variant", True)])
        # with no text to render the picture is told so, and in the tags, not after a text
        plain = assemble.assemble(exposed, answer)["nai_prompts"]["base_prompt"]
        self.assertIn(novelai.NO_TEXT_WORD, plain)
        self.assertNotIn(assemble.TEXT_PREFIX, plain)
        self.assertEqual(assemble.lint(nai), [])

    def test_a_settings_negative_goes_to_the_picture_and_a_subjects_to_the_subject(self):
        exposed = [_exposed("夜景", positive=["night"], negative=["sunlight"]),
                   _exposed("林", "character", positive=["1girl"], negative=["child"])]
        answer = assemble.clean_answer({"subjects": [{"key": "林"}], "negative": "blurry"})
        nai = assemble.assemble(exposed, answer)["nai_prompts"]
        self.assertTrue(nai["negative_prompt"].startswith("sunlight, blurry, "))
        self.assertNotIn("child", nai["negative_prompt"])
        self.assertEqual(nai["character_prompts"][0]["negative"], "child")

    def test_this_pictures_changes_to_an_entry_apply_here_only(self):
        exposed = [
            _exposed("林", "character", positive=["1girl", "black hair"], prose="Her hair is tied back."),
            _exposed("林/校服", "variant", positive=["blue cardigan", "open cardigan"]),
            _exposed("雨夜", positive=["rain", "night"], negative=["sunlight"]),
        ]
        change = {"use": True, "add_positive": "", "remove_positive": "", "add_negative": "", "remove_negative": "",
                  "add_prose": "", "remove_prose": ""}
        answer = assemble.clean_answer({
            "entries": [
                {**change, "key": "林/校服", "add_positive": "unworn cardigan", "remove_positive": "open cardigan"},
                {**change, "key": "林", "add_positive": "sitting, looking at viewer",
                 "remove_prose": "Her hair is tied back.", "add_prose": "Her hair is down."},
                {**change, "key": "雨夜", "use": False},
            ],
            "positive": "1girl", "negative": "", "prose": "",
            "subjects": [{"key": "林", "center_x": None, "center_y": None}], "size": "portrait",
        })
        made = assemble.assemble(exposed, answer)
        nai = made["nai_prompts"]
        self.assertEqual(nai["character_prompts"][0]["prompt"],
                         "1girl, black hair, sitting, looking at viewer, blue cardigan, unworn cardigan, Her hair is down.")
        self.assertNotIn("rain", nai["base_prompt"])
        self.assertNotIn("sunlight", nai["negative_prompt"])
        self.assertEqual([(r["key"], r["used"], r["positive"]) for r in made["assembly"]], [
            ("林", True, ["1girl", "black hair", "sitting", "looking at viewer"]),
            ("林/校服", True, ["blue cardigan", "unworn cardigan"]),
            ("雨夜", False, ["rain", "night"]),
        ])
        # the entries themselves read as they did: nothing is written back
        self.assertEqual(exposed[1]["positive"], ["blue cardigan", "open cardigan"])
        self.assertEqual(exposed[0]["prose"], "Her hair is tied back.")

    def test_a_sentence_this_picture_adds_comes_after_the_entrys_own_and_never_replaces_it(self):
        """The drawing model writes a pose sentence for a subject; the entry's
        own sentence (where the mole is, how the hair falls) is what keeps the
        character the same person, so it stays unless the model says in so
        many words that it does not hold for this picture."""
        exposed = [_exposed("林", "character", positive=["1girl"],
                            prose="The mole sits under her right eye.")]
        change = {"key": "林", "use": True, "add_positive": "", "remove_positive": "", "add_negative": "",
                  "remove_negative": "", "add_prose": "She looks back over her shoulder.", "remove_prose": ""}
        made = assemble.assemble(exposed, assemble.clean_answer({"entries": [change], "subjects": [{"key": "林"}]}))
        self.assertEqual(made["nai_prompts"]["character_prompts"][0]["prompt"],
                         "1girl, The mole sits under her right eye. She looks back over her shoulder.")
        self.assertEqual(made["assembly"][0]["prose"],
                         "The mole sits under her right eye. She looks back over her shoulder.")
        # and a model that answers in the old shape, or with nothing, changes nothing
        kept = assemble.assemble(exposed, assemble.clean_answer({"entries": [{"key": "林", "prose": "x"}]}))
        self.assertEqual(kept["assembly"][0]["prose"], "The mole sits under her right eye.")

    def test_a_picture_takes_sentences_out_one_by_one_and_the_rest_stay(self):
        """The hair is up in this picture, so the sentence about it falling
        loose is taken out; where the mole sits still holds and stays. A
        sentence named in part, without its stop, is still the sentence."""
        exposed = [_exposed("林", "character", positive=["1girl"],
                            prose="Her curls fall loose to her waist. The small mole sits under her right eye. "
                                  "A thin scar crosses her left eyebrow.")]
        change = {"key": "林", "use": True, "add_positive": "", "remove_positive": "", "add_negative": "",
                  "remove_negative": "", "add_prose": "Her curls are pinned up in a bun.",
                  "remove_prose": "Her curls fall loose to her waist. thin scar crosses her left eyebrow"}
        made = assemble.assemble(exposed, assemble.clean_answer({"entries": [change], "subjects": [{"key": "林"}]}))
        self.assertEqual(made["assembly"][0]["prose"],
                         "The small mole sits under her right eye. Her curls are pinned up in a bun.")
        # an answer in the earlier shape: true takes all of the entry's own
        # sentences out, false none
        old = assemble.clean_answer({"entries": [{**change, "remove_prose": True}]})
        self.assertEqual(assemble.assemble(exposed, old)["assembly"][0]["prose"], "Her curls are pinned up in a bun.")
        none = assemble.clean_answer({"entries": [{**change, "remove_prose": False, "add_prose": ""}]})
        self.assertEqual(assemble.assemble(exposed, none)["assembly"][0]["prose"], exposed[0]["prose"])

    def test_a_tag_written_twice_in_whatever_dressing_goes_out_once(self):
        exposed = [_exposed("画风", category="画风", positive=["film grain", "{{rim light}}"]),
                   _exposed("光", positive=["rim_light", "backlighting"])]
        nai = assemble.assemble(exposed, assemble.clean_answer({"positive": "1girl, Film Grain"}))["nai_prompts"]
        self.assertEqual(nai["base_prompt"],
                         "1girl, Film Grain, {{rim light}}, backlighting, very aesthetic, masterpiece, no text")

    def test_the_unknown_tag_check_skips_the_pieces_of_a_sentence(self):
        """A sentence with a comma in it is not several tags: with the turn's
        record at hand the check leaves its pieces alone."""
        exposed = [_exposed("光", positive=["backlighting"],
                            prose="warm light falls on her cheek, her right hand, and the book")]
        made = assemble.assemble(exposed, assemble.clean_answer({}))
        # the index stands in for itself here, so the default negative is left out of the question
        nai = {**made["nai_prompts"], "negative_prompt": ""}
        with mock.patch.object(llm.danbooru, "unknown", side_effect=lambda tags: list(tags)):
            self.assertEqual(llm.unknown_tags(nai, made["assembly"]), ["backlighting"])
            # without the record the short pieces of the sentence are taken for tags
            self.assertIn("her right hand", llm.unknown_tags(nai))
