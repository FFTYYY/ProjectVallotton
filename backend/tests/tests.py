"""API + pipeline tests. External services (grok / novelai) are mocked."""

from __future__ import annotations

import io
import json
from datetime import timedelta
import uuid
from unittest import mock

from dataclasses import replace

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from core.models import (
    Attachment,
    Character,
    CharacterVariant,
    Conversation,
    Folder,
    LibraryProposal,
    Message,
    Setting,
)
from core import ui_text
from core.prompts import wording
from core.services import (
    gensettings, jobs, library, llm, novelai, pipeline, prompt, providers,
)

TEST_CREDS = {
    "APP_USERNAME": "tester",
    "APP_PASSWORD": "test-password-123",
    "ADMIN_USERNAME": "admin",
    "ADMIN_PASSWORD": "admin-password-123",
    "MCP_TOKEN": "mcp-token-abc",
}


def _png_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (120, 160, 220)).save(buf, format="PNG")
    return buf.getvalue()


# what the LEAD model returns: a reply, and optionally a plain-language brief
FAKE_TEXT_RESULT = {"reply": "你好，我是瓦洛东。", "draw": None, "edits": None}

FAKE_IMAGE_RESULT = {
    "reply": "构图我选了俯视角。",
    # no n_images: the lead model is not asked for one, and the app's count
    # (force_n_images, from 出图数量) is what _paint acts on
    "draw": {
        "main": "一个银发少女站在花园里",
        "scene": "白天的花园",
        "size": "portrait",
    },
    "edits": None,
}

# what the DRAWING model turns that brief into
FAKE_DRAW_RESULT = {
    # draw_turn stamps 1 and _paint overwrites it with the app's count
    "n_images": 1,
    "size": "portrait",
    "base_prompt": "1girl, garden, best quality",
    "character_prompts": [
        {"prompt": "silver hair, blue eyes", "center_x": None, "center_y": None}
    ],
    "negative_prompt": "",
}


def _project(edits: dict) -> dict:
    """
    The same edits with every entry scoped to the project.

    The project is the layer the user asks for by name; an entry that says
    nothing belongs to the conversation. These proposal tests are about the
    project layer -- approval, the automatic modes, merging up the folder
    chain -- so they say so, the way a model that read the prompt would.
    """
    return {
        k: ([{**e, "scope": "project"} for e in v] if isinstance(v, list) else v)
        for k, v in edits.items()
    }


def _sync_jobs():
    """
    Run generation jobs inline instead of in a thread, with no reflection.

    The default reflection round would otherwise run against whatever provider
    the environment names -- a real one, over the network, with a real key.
    A test of reflection itself mocks the model and asks for rounds in its own
    harness (tests_reflection).
    """
    return _Both(
        mock.patch.multiple(
            jobs,
            start_generation=pipeline.run_generation,
            start_redraw=pipeline.run_redraw,
        ),
        mock.patch.object(pipeline, "_reflection_rounds", return_value=0),
    )


class _Both:
    """Two context managers as one, so `with _sync_jobs():` stays as it was."""

    def __init__(self, *parts):
        self.parts = parts

    def __enter__(self):
        for p in self.parts:
            p.__enter__()
        return self

    def __exit__(self, *exc):
        for p in reversed(self.parts):
            p.__exit__(*exc)
        return False


@override_settings(**TEST_CREDS)
class AuthTests(TestCase):
    def login(self, c: Client, username="tester", password="test-password-123"):
        return c.post(
            "/api/auth/login",
            json.dumps({"username": username, "password": password}),
            content_type="application/json",
        )

    def test_login_success_and_me(self):
        c = Client()
        self.assertEqual(c.get("/api/auth/me").json()["authenticated"], False)
        resp = self.login(c)
        self.assertEqual(resp.status_code, 200)
        me = c.get("/api/auth/me").json()
        self.assertTrue(me["authenticated"])
        self.assertEqual(me["username"], "tester")

    def test_login_failure(self):
        c = Client()
        resp = self.login(c, password="wrong")
        self.assertEqual(resp.status_code, 401)

    def test_login_throttle_locks_after_8_fails(self):
        c = Client()
        for _ in range(8):
            self.login(c, password="wrong")
        resp = self.login(c)  # correct creds but locked now
        self.assertEqual(resp.status_code, 429)

    def test_api_requires_auth(self):
        c = Client()
        self.assertEqual(c.get("/api/conversations").status_code, 401)


@override_settings(**TEST_CREDS)
class ChatTests(TestCase):
    def setUp(self):
        self.c = Client()
        self.c.post(
            "/api/auth/login",
            json.dumps({"username": "tester", "password": "test-password-123"}),
            content_type="application/json",
        )

    def _new_conv(self) -> str:
        return self.c.post(
            "/api/conversations", "{}", content_type="application/json"
        ).json()["id"]

    def test_conversation_crud(self):
        cid = self._new_conv()
        resp = self.c.get("/api/conversations").json()
        self.assertEqual(len(resp["conversations"]), 1)
        resp = self.c.patch(
            f"/api/conversations/{cid}",
            json.dumps({"title": "改名"}),
            content_type="application/json",
        )
        self.assertEqual(resp.json()["title"], "改名")
        # Archived, never deleted. There is no DELETE to call — an endpoint
        # that quietly contradicted that rule made it a sentence rather than a
        # fact, so it is gone.
        self.assertEqual(self.c.delete(f"/api/conversations/{cid}").status_code, 405)
        self.assertEqual(
            self.c.post(
                f"/api/conversations/{cid}/archive",
                json.dumps({"archived": True}),
                content_type="application/json",
            ).status_code,
            200,
        )
        self.assertTrue(Conversation.objects.get(id=cid).is_archived)

    def test_text_turn(self):
        cid = self._new_conv()
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=FAKE_TEXT_RESULT
        ), mock.patch.object(pipeline.llm, "make_title", return_value="问好"):
            resp = self.c.post(
                f"/api/conversations/{cid}/messages",
                {"text": "你好", "client_msg_id": "c1"},
            )
        self.assertEqual(resp.status_code, 201)
        msgs = self.c.get(f"/api/conversations/{cid}/messages").json()["messages"]
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[1]["status"], "done")
        self.assertEqual(msgs[1]["content"], "你好，我是瓦洛东。")
        self.assertEqual(
            Conversation.objects.get(id=cid).title, "问好"
        )

    def test_image_turn_saves_attachments(self):
        cid = self._new_conv()
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=FAKE_IMAGE_RESULT
        ), mock.patch.object(
            pipeline.llm, "draw_turn", return_value=FAKE_DRAW_RESULT
        ), mock.patch.object(
            pipeline.novelai, "generate_one", return_value=(_png_bytes(), 42)
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            self.c.post(
                f"/api/conversations/{cid}/messages", {"text": "画个女孩", "n_images": "2"}
            )
        msgs = self.c.get(f"/api/conversations/{cid}/messages").json()["messages"]
        assistant = msgs[1]
        self.assertEqual(assistant["kind"], "image")
        self.assertEqual(len(assistant["attachments"]), 2)
        self.assertEqual(assistant["attachments"][0]["kind"], "generated")
        # media serving works
        url = assistant["attachments"][0]["url"]
        resp = self.c.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_upload_and_busy_409(self):
        cid = self._new_conv()
        png = _png_bytes()
        # do NOT run jobs synchronously → assistant stays pending
        with mock.patch.object(jobs, "start_generation"):
            resp = self.c.post(
                f"/api/conversations/{cid}/messages",
                {"text": "参考这张", "images": io.BytesIO(png)},
            )
            self.assertEqual(resp.status_code, 201)
            resp2 = self.c.post(
                f"/api/conversations/{cid}/messages", {"text": "再来一条"}
            )
            self.assertEqual(resp2.status_code, 409)

    def test_idempotent_client_msg_id(self):
        cid = self._new_conv()
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=FAKE_TEXT_RESULT
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            r1 = self.c.post(
                f"/api/conversations/{cid}/messages",
                {"text": "你好", "client_msg_id": "dup-1"},
            )
            r2 = self.c.post(
                f"/api/conversations/{cid}/messages",
                {"text": "你好", "client_msg_id": "dup-1"},
            )
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(
            Message.objects.filter(conversation_id=cid, role="user").count(), 1
        )

    def test_cancel(self):
        cid = self._new_conv()
        with mock.patch.object(jobs, "start_generation"):
            resp = self.c.post(f"/api/conversations/{cid}/messages", {"text": "hi"})
        assistant_id = resp.json()["messages"][1]["id"]
        resp = self.c.post(f"/api/messages/{assistant_id}/cancel")
        # cancel is immediate — the row is already cancelled before the worker
        # (which may still be blocked in grok) ever returns
        self.assertEqual(resp.json()["status"], "cancelled")
        self.assertEqual(Message.objects.get(id=assistant_id).status, "cancelled")
        # a worker that finishes afterwards must discard its result (no-op writes)
        with mock.patch.object(
            pipeline.llm, "lead_turn", return_value=FAKE_TEXT_RESULT
        ):
            pipeline.run_generation(assistant_id)
        m = Message.objects.get(id=assistant_id)
        self.assertEqual(m.status, "cancelled")
        self.assertEqual(m.content, "")  # grok's reply was discarded

    def test_force_n_images_parallel(self):
        cid = self._new_conv()
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=FAKE_IMAGE_RESULT
        ), mock.patch.object(
            pipeline.llm, "draw_turn", return_value=FAKE_DRAW_RESULT
        ), mock.patch.object(
            pipeline.novelai, "generate_one", return_value=(_png_bytes(), 1)
        ) as gen, mock.patch.object(pipeline.llm, "make_title", return_value=""):
            # FAKE_IMAGE_RESULT asks for 2; force 4
            self.c.post(f"/api/conversations/{cid}/messages", {"text": "画", "n_images": "4"})
        msgs = self.c.get(f"/api/conversations/{cid}/messages").json()["messages"]
        self.assertEqual(len(msgs[1]["attachments"]), 4)
        self.assertEqual(gen.call_count, 4)

    def test_branch_copies_the_rows_and_shares_the_files(self):
        cid = self._new_conv()
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=FAKE_IMAGE_RESULT
        ), mock.patch.object(
            pipeline.llm, "draw_turn", return_value=FAKE_DRAW_RESULT
        ), mock.patch.object(
            pipeline.novelai, "generate_one", return_value=(_png_bytes(), 5)
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            self.c.post(f"/api/conversations/{cid}/messages", {"text": "画女孩"})
        msgs = self.c.get(f"/api/conversations/{cid}/messages").json()["messages"]
        anchor = msgs[1]["id"]
        resp = self.c.post(f"/api/messages/{anchor}/branch")
        self.assertEqual(resp.status_code, 201)
        new_cid = resp.json()["id"]
        self.assertNotEqual(new_cid, cid)
        branch_msgs = self.c.get(f"/api/conversations/{new_cid}/messages").json()["messages"]
        self.assertEqual(len(branch_msgs), 2)
        self.assertEqual(branch_msgs[0]["content"], "画女孩")
        # The rows are the branch's own; the bytes on disk are not copied.
        # A generated image is never edited and never deleted, so a second copy
        # would differ from the first in nothing but the disk it fills.
        orig = Attachment.objects.get(id=msgs[1]["attachments"][0]["id"])
        branch_att = Attachment.objects.filter(
            message__conversation_id=new_cid, kind="generated"
        ).first()
        assert branch_att is not None, "branch should carry the generated image"
        self.assertNotEqual(branch_att.id, orig.id)
        self.assertEqual(branch_att.file.name, orig.file.name)
        # editing/continuing the branch does not touch the original
        Conversation.objects.filter(id=new_cid).update(title="改了")
        self.assertEqual(Conversation.objects.get(id=cid).title, "新对话")

    def test_a_branch_copies_every_message_column_except_the_ones_named_here(self):
        """The branch once dropped `prompt` and `main_index` and nobody noticed
        until the first edit in a branch drew a fresh picture. This test
        makes the next new column a decision: add it to the copy, or to the
        list of columns a copy deliberately does not carry."""
        from django.db.models import ForeignKey
        not_carried = {
            "id", "seq", "created_at", "updated_at",          # the copy's own
            "conversation",                                   # the branch
            "client_msg_id", "phase", "cancel_requested", "heartbeat_at",   # in-flight only
            "force_n_images", "force_reflection_rounds", "sticky_refs",     # read while pending only
            "archived", "notice_kind", "run_settled",         # archived rows and notes are not copied; runs do not cross
        }
        columns = {f.name for f in Message._meta.get_fields()
                   if getattr(f, "concrete", False) and not f.auto_created}
        cid = self._new_conv()
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=FAKE_IMAGE_RESULT
        ), mock.patch.object(
            pipeline.llm, "draw_turn", return_value=FAKE_DRAW_RESULT
        ), mock.patch.object(
            pipeline.novelai, "generate_one", return_value=(_png_bytes(), 5)
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            self.c.post(f"/api/conversations/{cid}/messages", {"text": "画女孩"})
        drawn = Message.objects.get(conversation_id=cid, role="assistant")
        Message.objects.filter(id=drawn.id).update(error="留着的说明", reflection_log=[{"round": 1}])
        drawn.refresh_from_db()
        resp = self.c.post(f"/api/messages/{drawn.id}/branch")
        copy = Message.objects.get(conversation_id=resp.json()["id"], role="assistant")
        for name in columns - not_carried:
            f = Message._meta.get_field(name)
            a, b = getattr(drawn, f.attname if isinstance(f, ForeignKey) else name),                    getattr(copy, f.attname if isinstance(f, ForeignKey) else name)
            self.assertEqual(a, b, f"branch copy differs in column {name!r}")

    def test_a_branch_from_the_end_takes_the_library_as_it_stands_now(self):
        cid = self._new_conv()
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=FAKE_IMAGE_RESULT
        ), mock.patch.object(
            pipeline.llm, "draw_turn", return_value=FAKE_DRAW_RESULT
        ), mock.patch.object(
            pipeline.novelai, "generate_one", return_value=(_png_bytes(), 5)
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            self.c.post(f"/api/conversations/{cid}/messages", {"text": "画女孩"})
        drawn = Message.objects.get(conversation_id=cid, role="assistant")
        # after the turn: the user adds an entry and edits its text, lines not yet rewritten
        r = self.c.post(f"/api/conversations/{cid}/settings",
                        json.dumps({"title": "黄昏", "content": "傍晚",
                                    "tags": [{"part": "天色", "tags": "sunset"}]}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content[:200])
        made = Setting.objects.get(title="黄昏", conversation_id=cid)
        Setting.objects.filter(id=made.id).update(content="深夜", text_edited_at=timezone.now(),
                                                  lines_ai_at=timezone.now() - timedelta(hours=1))
        resp = self.c.post(f"/api/messages/{drawn.id}/branch")
        self.assertEqual(resp.status_code, 201)
        branch = Setting.objects.get(title="黄昏", conversation_id=resp.json()["id"])
        self.assertEqual(branch.content, "深夜")
        # still waiting for its lines in the branch, as it was in the source
        behind = branch.lines_ai_at is None or (branch.text_edited_at is not None
                                                and branch.lines_ai_at < branch.text_edited_at)
        self.assertTrue(behind)

    def test_the_lead_sees_the_main_picture_first(self):
        cid = self._new_conv()
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=FAKE_IMAGE_RESULT
        ), mock.patch.object(
            pipeline.llm, "draw_turn", return_value=FAKE_DRAW_RESULT
        ), mock.patch.object(
            pipeline.novelai, "generate_one", return_value=(_png_bytes(), 5)
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            self.c.post(f"/api/conversations/{cid}/messages", {"text": "画女孩", "n_images": "3"})
        drawn = Message.objects.get(conversation_id=cid, role="assistant")
        Message.objects.filter(id=drawn.id).update(main_index=2)
        conv = Conversation.objects.get(id=cid)
        history = pipeline.build_history(conv, before_seq=drawn.seq + 1)
        note = history[-1]
        urls = [p["image_url"]["url"] for p in note["content"] if p.get("type") == "image_url"]
        main = next(a for a in drawn.attachments.all() if a.position == 2)
        self.assertEqual(urls[0], pipeline._image_data_url(main))
        self.assertEqual(len(urls), pipeline.HISTORY.max_generated_per_turn)

    def test_a_failed_picture_in_a_batch_is_said_on_the_row(self):
        cid = self._new_conv()
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise pipeline.novelai.NovelAIError("novelai timed out")
            return _png_bytes(), 5
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=FAKE_IMAGE_RESULT
        ), mock.patch.object(
            pipeline.llm, "draw_turn", return_value=FAKE_DRAW_RESULT
        ), mock.patch.object(
            pipeline, "_generate_in_thread", side_effect=flaky
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            self.c.post(f"/api/conversations/{cid}/messages", {"text": "画女孩", "n_images": "2"})
        drawn = Message.objects.get(conversation_id=cid, role="assistant")
        self.assertEqual(drawn.status, "done")
        self.assertEqual(drawn.attachments.filter(kind="generated").count(), 1)
        self.assertEqual(len((drawn.model_raw or {}).get("paint_errors") or []), 1)
        self.assertIn("novelai timed out", (drawn.model_raw or {})["paint_errors"][0])
        raw = self.c.get(f"/api/messages/{drawn.id}/raw").json()
        self.assertEqual(len(raw["paint_errors"]), 1)

    def test_one_switch_at_a_time(self):
        cid = self._new_conv()
        r = self.c.post(f"/api/conversations/{cid}/settings",
                        json.dumps({"title": "黄昏", "tags": [{"part": "天色", "tags": "sunset"}]}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content[:200])
        sid = Setting.objects.get(title="黄昏").id
        r2 = self.c.post(f"/api/conversations/{cid}/settings",
                         json.dumps({"title": "雨夜", "tags": [{"part": "天气", "tags": "rain"}]}),
                         content_type="application/json")
        self.assertEqual(r2.status_code, 201, r2.content[:200])
        rid = Setting.objects.get(title="雨夜").id
        conv = Conversation.objects.get(id=cid)
        on = set(conv.selected_settings.values_list("id", flat=True))
        self.assertEqual(on, {sid, rid})
        r = self.c.post(f"/api/conversations/{cid}/selection/toggle",
                        json.dumps({"kind": "setting", "id": str(sid), "on": False}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content[:200])
        # only the one named changed; the other stays as the server had it
        self.assertEqual(set(conv.selected_settings.values_list("id", flat=True)), {rid})

    def test_the_row_says_which_references_the_lead_was_given(self):
        conv = Conversation.objects.get(id=self._new_conv())
        asked = Message.objects.create(conversation=conv, seq=1, role="user", status="done", content="照这张画")
        upload = Attachment(message=asked, kind="upload", mime="image/png", width=64, height=64)
        data = _png_bytes()
        upload.size_bytes = len(data)
        upload.file.save("ref.png", ContentFile(data), save=True)
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=FAKE_IMAGE_RESULT
        ), mock.patch.object(
            pipeline.llm, "draw_turn", return_value=FAKE_DRAW_RESULT
        ), mock.patch.object(
            pipeline.novelai, "generate_one", return_value=(_png_bytes(), 5)
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            self.c.post(f"/api/conversations/{conv.id}/messages", {"text": "照这张画"})
        turn = Message.objects.filter(conversation=conv, role="assistant").order_by("-seq").first()
        assert turn is not None
        self.assertEqual((turn.model_raw or {}).get("references_sent"), [str(upload.id)])
        raw = self.c.get(f"/api/messages/{turn.id}/raw").json()
        self.assertEqual(len(raw["references"]), 1)
        self.assertIn(str(upload.id), raw["references"][0]["url"])

    def test_the_inspector_shows_the_leads_answer_before_the_app_tidied_it(self):
        answer = {"reply": "好", "draw": {"main": "一个女孩", "subjects": [{"look": "红发"}]}, "edits": None}
        with mock.patch.object(llm, "_ask", return_value=(answer, {})):
            out = llm.lead_turn([{"role": "user", "content": "画"}])
        self.assertEqual(out["raw_answer"], answer)
        # the tidied draw dropped the nameless subject; the raw one still has it
        self.assertEqual(out["raw_answer"]["draw"]["subjects"], [{"look": "红发"}])

    def test_a_stop_that_kept_pictures_says_so_on_a_finished_turn(self):
        cid = self._new_conv()
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=FAKE_IMAGE_RESULT
        ), mock.patch.object(
            pipeline.llm, "draw_turn", return_value=FAKE_DRAW_RESULT
        ), mock.patch.object(
            pipeline.novelai, "generate_one", return_value=(_png_bytes(), 5)
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            self.c.post(f"/api/conversations/{cid}/messages", {"text": "画女孩"})
        turn = Message.objects.get(conversation_id=cid, role="assistant")
        Message.objects.filter(id=turn.id).update(status="cancelled")
        turn.refresh_from_db()
        pipeline._finish_stopped(turn)
        turn.refresh_from_db()
        self.assertEqual(turn.status, "done")
        self.assertEqual(turn.error, ui_text.STOPPED_KEPT_PICTURES)
        shown = self.c.get(f"/api/conversations/{cid}/messages").json()["messages"][-1]
        self.assertEqual(shown["error"], ui_text.STOPPED_KEPT_PICTURES)
        self.assertEqual(shown["used_entries"], [])

    def test_moving_a_conversation_by_drag_prunes_the_old_projects_selection(self):
        a = Folder.objects.create(name="甲")
        b = Folder.objects.create(name="乙")
        conv = Conversation.objects.create(title="t", folder=a)
        old = Setting.objects.create(folder=a, title="甲的画风", tags=[{"id": "s1", "part": "画风", "tags": "painterly"}])
        new = Setting.objects.create(folder=b, title="乙的画风", tags=[{"id": "s1", "part": "画风", "tags": "sketch"}])
        conv.selected_settings.add(old)
        r = self.c.post("/api/projects/move", json.dumps({"kind": "conversation", "id": str(conv.id),
                                                          "parent_id": str(b.id), "index": 0}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content[:200])
        on = set(conv.selected_settings.values_list("id", flat=True))
        self.assertNotIn(old.id, on)
        self.assertIn(new.id, on)

    def test_a_branch_carries_the_proposal_cards_of_the_turns_it_copies(self):
        cid = self._new_conv()
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value={**FAKE_IMAGE_RESULT, "edits": _project({
                "settings": [{"title": "黄昏", "scope": "conversation",
                              "tags": [{"part": "天色", "tags": "sunset"}]}], "characters": []})}
        ), mock.patch.object(
            pipeline.llm, "draw_turn", return_value=FAKE_DRAW_RESULT
        ), mock.patch.object(
            pipeline.novelai, "generate_one", return_value=(_png_bytes(), 5)
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            self.c.post(f"/api/conversations/{cid}/messages", {"text": "记下黄昏"})
        drawn = Message.objects.get(conversation_id=cid, role="assistant")
        self.assertEqual(LibraryProposal.objects.filter(conversation_id=cid).count(), 1)
        resp = self.c.post(f"/api/messages/{drawn.id}/branch")
        new_cid = resp.json()["id"]
        cards = LibraryProposal.objects.filter(conversation_id=new_cid)
        card = cards.first()
        assert card is not None
        self.assertEqual(str(card.message.conversation_id), new_cid)

    def test_a_branch_carries_the_picture_and_the_main_index_so_it_can_be_edited(self):
        cid = self._new_conv()
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=FAKE_IMAGE_RESULT
        ), mock.patch.object(
            pipeline.llm, "draw_turn", return_value=FAKE_DRAW_RESULT
        ), mock.patch.object(
            pipeline.novelai, "generate_one", return_value=(_png_bytes(), 5)
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            self.c.post(f"/api/conversations/{cid}/messages", {"text": "画女孩", "n_images": "2"})
        drawn = Message.objects.get(conversation_id=cid, role="assistant")
        self.assertIsInstance(drawn.prompt, dict)
        Message.objects.filter(id=drawn.id).update(main_index=1)
        resp = self.c.post(f"/api/messages/{drawn.id}/branch")
        self.assertEqual(resp.status_code, 201)
        copy = Message.objects.get(conversation_id=resp.json()["id"], role="assistant")
        # the slot table and the chosen picture travel with the drawing: they
        # are what the next edit in the branch changes and follows
        self.assertEqual(copy.prompt, drawn.prompt)
        self.assertEqual(copy.main_index, 1)
        self.assertEqual((copy.nai_prompts or {}).get("draw_id"), (drawn.nai_prompts or {}).get("draw_id"))

        # an edit in the branch is an edit of the copied picture, not a fresh draw
        edit_result = {**FAKE_IMAGE_RESULT,
                       "draw": {**FAKE_IMAGE_RESULT["draw"], "change": "把眼镜摘掉",
                                "continues": (drawn.nai_prompts or {}).get("draw_id")}}
        seen: dict = {}

        def fake_edit(picture, brief, *args, **kwargs):
            seen["picture"] = picture
            return FAKE_DRAW_RESULT

        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=edit_result
        ), mock.patch.object(
            pipeline.llm, "draw_edit", side_effect=fake_edit
        ), mock.patch.object(
            pipeline.llm, "draw_turn", side_effect=AssertionError("an edit must not draw fresh")
        ), mock.patch.object(
            pipeline.novelai, "generate_one", return_value=(_png_bytes(), 5)
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            self.c.post(f"/api/conversations/{copy.conversation_id}/messages", {"text": "把眼镜摘掉"})
        self.assertEqual(seen.get("picture"), drawn.prompt)

    def test_an_edit_that_sends_the_same_words_again_is_a_bad_answer(self):
        cid = self._new_conv()
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=FAKE_IMAGE_RESULT
        ), mock.patch.object(
            pipeline.llm, "draw_turn", return_value=FAKE_DRAW_RESULT
        ), mock.patch.object(
            pipeline.novelai, "generate_one", return_value=(_png_bytes(), 5)
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            self.c.post(f"/api/conversations/{cid}/messages", {"text": "画女孩"})
        drawn = Message.objects.get(conversation_id=cid, role="assistant")
        edit_result = {**FAKE_IMAGE_RESULT,
                       "draw": {**FAKE_IMAGE_RESULT["draw"], "change": "加一朵云",
                                "continues": (drawn.nai_prompts or {}).get("draw_id")}}
        paints: list = []
        # the drawing model answers the edit with the very words it sent before
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=edit_result
        ), mock.patch.object(
            pipeline.llm, "draw_edit", return_value=FAKE_DRAW_RESULT
        ), mock.patch.object(
            pipeline.novelai, "generate_one", side_effect=lambda *a, **k: paints.append(1) or (_png_bytes(), 5)
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            self.c.post(f"/api/conversations/{cid}/messages", {"text": "加一朵云"})
        failed = Message.objects.filter(conversation_id=cid, role="assistant").order_by("-seq").first()
        assert failed is not None
        self.assertEqual(failed.status, "error")
        self.assertIn("没有改出任何东西", failed.error)
        self.assertEqual(paints, [], "the same prompt must not be painted again")
        # a line the lead rewrote changes the words with no patch at all: that is an edit
        changed = {**FAKE_DRAW_RESULT, "character_prompts": [{"prompt": "silver hair, blue eyes, red cardigan"}]}
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=edit_result
        ), mock.patch.object(
            pipeline.llm, "draw_edit", return_value=changed
        ), mock.patch.object(
            pipeline.novelai, "generate_one", return_value=(_png_bytes(), 5)
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            self.c.post(f"/api/conversations/{cid}/messages", {"text": "开衫换成红色"})
        last = Message.objects.filter(conversation_id=cid, role="assistant").order_by("-seq").first()
        assert last is not None
        self.assertEqual(last.status, "done")

    def test_a_paint_that_fails_leaves_the_row_without_that_attempts_prompt(self):
        cid = self._new_conv()
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=FAKE_IMAGE_RESULT
        ), mock.patch.object(
            pipeline.llm, "draw_turn", return_value=FAKE_DRAW_RESULT
        ), mock.patch.object(
            pipeline.novelai, "generate_one", side_effect=pipeline.novelai.NovelAIError("novelai timed out")
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            self.c.post(f"/api/conversations/{cid}/messages", {"text": "画女孩"})
        turn = Message.objects.get(conversation_id=cid, role="assistant")
        self.assertEqual(turn.status, "error")
        # no picture was painted, so no slot table claims to be behind one
        # (the count-settled nai_prompts may stay: 重绘 can replay a failed paint)
        self.assertIsNone(turn.prompt)

    def test_an_edit_of_a_drawing_without_a_slot_table_fails_instead_of_drawing_fresh(self):
        cid = self._new_conv()
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=FAKE_IMAGE_RESULT
        ), mock.patch.object(
            pipeline.llm, "draw_turn", return_value=FAKE_DRAW_RESULT
        ), mock.patch.object(
            pipeline.novelai, "generate_one", return_value=(_png_bytes(), 5)
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            self.c.post(f"/api/conversations/{cid}/messages", {"text": "画女孩"})
        drawn = Message.objects.get(conversation_id=cid, role="assistant")
        # a row from before pictures had slots
        Message.objects.filter(id=drawn.id).update(prompt=None)
        edit_result = {**FAKE_IMAGE_RESULT,
                       "draw": {**FAKE_IMAGE_RESULT["draw"], "change": "加一朵云",
                                "continues": (drawn.nai_prompts or {}).get("draw_id")}}
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=edit_result
        ), mock.patch.object(
            pipeline.llm, "draw_turn", side_effect=AssertionError("must not draw a cloud and nothing else")
        ), mock.patch.object(
            pipeline.llm, "draw_edit", side_effect=AssertionError("there is no picture to edit")
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            self.c.post(f"/api/conversations/{cid}/messages", {"text": "加一朵云"})
        failed = Message.objects.filter(conversation_id=cid, role="assistant").order_by("-seq").first()
        assert failed is not None
        self.assertEqual(failed.status, "error")
        self.assertIn("没有可改的表征表", failed.error)

    def test_a_shared_picture_survives_the_other_conversation_dropping_it(self):
        """
        Branching stopped copying image files, so two rows can name one file.
        That makes the delete inside a reflection round dangerous in a way it
        was not before: it used to own every file it touched, and now it may
        be holding the only copy of a picture that another conversation is
        also showing.

        This is the test that lets _clone_attachment share instead of copy.
        Without it, sharing rests on an argument about which rows a branch can
        reach — true today, and nothing keeps it true.
        """
        from django.core.files.storage import default_storage

        conv = Conversation.objects.create(title="原对话")
        msg = Message.objects.create(
            conversation=conv, seq=1, role="assistant", status="done", content=""
        )
        kept = Attachment.objects.create(message=msg, kind="generated", mime="image/png")
        kept.file.save("shared.png", ContentFile(_png_bytes()), save=True)
        name = kept.file.name

        branched = Message.objects.create(
            conversation=Conversation.objects.create(title="分支"),
            seq=1, role="assistant", status="done", content="",
        )
        twin = Attachment.objects.create(
            message=branched, kind="generated", mime="image/png", file=name
        )

        pipeline._discard_images([kept])

        self.assertFalse(Attachment.objects.filter(id=kept.id).exists())
        self.assertTrue(Attachment.objects.filter(id=twin.id).exists())
        self.assertTrue(
            default_storage.exists(name),
            "the other conversation is still showing this picture",
        )

    def test_the_last_row_to_let_go_takes_the_file_with_it(self):
        """Sharing must not turn into never cleaning up."""
        from django.core.files.storage import default_storage

        conv = Conversation.objects.create(title="只有一份")
        msg = Message.objects.create(
            conversation=conv, seq=1, role="assistant", status="done", content=""
        )
        only = Attachment.objects.create(message=msg, kind="generated", mime="image/png")
        only.file.save("alone.png", ContentFile(_png_bytes()), save=True)
        name = only.file.name

        pipeline._discard_images([only])

        self.assertFalse(Attachment.objects.filter(id=only.id).exists())
        self.assertFalse(default_storage.exists(name))

    def test_redraw(self):
        cid = self._new_conv()
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=FAKE_IMAGE_RESULT
        ), mock.patch.object(
            pipeline.llm, "draw_turn", return_value=FAKE_DRAW_RESULT
        ), mock.patch.object(
            pipeline.novelai, "generate_one", return_value=(_png_bytes(), 7)
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            self.c.post(f"/api/conversations/{cid}/messages", {"text": "画"})
            msgs = self.c.get(f"/api/conversations/{cid}/messages").json()["messages"]
            resp = self.c.post(
                f"/api/messages/{msgs[1]['id']}/redraw",
                json.dumps({"n_images": 1}),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 201)
        msgs = self.c.get(f"/api/conversations/{cid}/messages").json()["messages"]
        # into the turn it came from: another picture on it, not a new turn
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[1]["status"], "done")
        self.assertEqual(len(msgs[1]["attachments"]), 2)
        places = [a["position"] for a in msgs[1]["attachments"]]
        self.assertEqual(len(set(places)), 2, f"two pictures share a place: {places}")

    def test_redrawing_an_old_turn_does_not_become_the_newest_one(self):
        """
        The lead model reads the newest picture turn back as "what you just
        made". A redraw that arrived as a new message put a picture from half
        an hour ago at the end of the conversation wearing that label.
        """
        cid = self._new_conv()
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=FAKE_IMAGE_RESULT
        ), mock.patch.object(
            pipeline.llm, "draw_turn", return_value=FAKE_DRAW_RESULT
        ), mock.patch.object(
            pipeline.novelai, "generate_one", return_value=(_png_bytes(), 7)
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            self.c.post(f"/api/conversations/{cid}/messages", {"text": "画第一张"})
            first = self.c.get(f"/api/conversations/{cid}/messages").json()["messages"][1]
            self.c.post(f"/api/conversations/{cid}/messages", {"text": "再画一张"})
            msgs = self.c.get(f"/api/conversations/{cid}/messages").json()["messages"]
            newest = msgs[-1]
            # now redraw the OLDER of the two picture turns
            self.c.post(f"/api/messages/{first['id']}/redraw")

        after = self.c.get(f"/api/conversations/{cid}/messages").json()["messages"]
        self.assertEqual(len(after), len(msgs), "the redraw added a turn")
        self.assertEqual(after[-1]["id"], newest["id"],
                         "an older turn's redraw ended up at the end of the conversation")
        self.assertEqual(len(after[1]["attachments"]), 2)

    def test_a_redraw_does_not_claim_a_continuous_round(self):
        """It belongs to no run — and now it is not even a new row to adopt."""
        cid = self._new_conv()
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=FAKE_IMAGE_RESULT
        ), mock.patch.object(
            pipeline.llm, "draw_turn", return_value=FAKE_DRAW_RESULT
        ), mock.patch.object(
            pipeline.novelai, "generate_one", return_value=(_png_bytes(), 7)
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            self.c.post(f"/api/conversations/{cid}/messages", {"text": "画"})
            row = self.c.get(f"/api/conversations/{cid}/messages").json()["messages"][1]
            self.c.post(f"/api/messages/{row['id']}/redraw")
        self.assertFalse(
            Message.objects.filter(conversation_id=cid, role="assistant",
                                   run_settled=False).exists()
        )

    def test_a_redraw_refuses_prompts_that_cannot_paint(self):
        """
        重绘 replays a row rather than asking a model, so the row is the only
        thing standing between the user and a bill. A row can be older than
        the shape the app expects — written by an earlier version, by MCP, or
        copied into a branch — and an empty base_prompt sent to NovelAI is a
        picture of nothing, paid for.
        """
        cid = self._new_conv()
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=FAKE_IMAGE_RESULT
        ), mock.patch.object(
            pipeline.llm, "draw_turn", return_value=FAKE_DRAW_RESULT
        ), mock.patch.object(
            pipeline.novelai, "generate_one", return_value=(_png_bytes(), 7)
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            self.c.post(f"/api/conversations/{cid}/messages", {"text": "画"})
        msgs = self.c.get(f"/api/conversations/{cid}/messages").json()["messages"]
        row = Message.objects.get(id=msgs[1]["id"])
        hollow = dict(row.nai_prompts or {})
        hollow["base_prompt"] = ""
        Message.objects.filter(id=row.id).update(nai_prompts=hollow)

        with mock.patch.object(pipeline.novelai, "generate_one") as paint:
            resp = self.c.post(f"/api/messages/{row.id}/redraw")
        self.assertEqual(resp.status_code, 400)
        paint.assert_not_called()

    def test_rerun_archives_old_assistant(self):
        cid = self._new_conv()
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=FAKE_TEXT_RESULT
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            self.c.post(f"/api/conversations/{cid}/messages", {"text": "你好"})
            resp = self.c.post(f"/api/conversations/{cid}/rerun")
        self.assertEqual(resp.status_code, 201)
        active = self.c.get(f"/api/conversations/{cid}/messages").json()["messages"]
        self.assertEqual(len(active), 2)  # user + new assistant (old one archived)
        self.assertEqual(
            Message.objects.filter(conversation_id=cid, archived=True).count(), 1
        )

    def test_stale_pending_is_reaped(self):
        """
        By the sweeper on its clock, not by a page load. Reading a conversation
        used to run the reap, which put a write-capable sweep on a 1.3-second
        poll; the sweeper covers it, and a restart — the common case — is now
        concluded at start-up without waiting for a heartbeat to go stale.
        """
        cid = self._new_conv()
        with mock.patch.object(jobs, "start_generation"):
            resp = self.c.post(f"/api/conversations/{cid}/messages", {"text": "hi"})
        assistant_id = resp.json()["messages"][1]["id"]
        from datetime import timedelta

        from django.utils import timezone

        Message.objects.filter(id=assistant_id).update(
            heartbeat_at=timezone.now() - timedelta(seconds=9999)
        )
        # reading it changes nothing
        msgs = self.c.get(f"/api/conversations/{cid}/messages").json()["messages"]
        self.assertEqual(msgs[1]["status"], "pending")

        with mock.patch.object(jobs, "start_generation"):
            self.assertEqual(jobs.reap_stale_jobs(), 1)
        msgs = self.c.get(f"/api/conversations/{cid}/messages").json()["messages"]
        self.assertEqual(msgs[1]["status"], "error")


@override_settings(**TEST_CREDS)
# The token counts below are grok-4.6's marks (long context at 200k, ceiling
# at 80% of 500k); the default provider is deepseek, whose marks are higher.
@override_settings(LLM_PROVIDER="grok")
class ContextNoticeTests(TestCase):
    """The app warns, in its own voice, when a conversation's context fills up."""

    def _conv_with_tokens(self, tokens: int) -> Conversation:
        conv = Conversation.objects.create(title="长对话")
        Message.objects.create(
            conversation=conv, seq=1, role="user", status="done", content="hi"
        )
        Message.objects.create(
            conversation=conv, seq=2, role="assistant", status="done", content="ok"
        )
        pipeline.maybe_context_notice(conv, tokens)
        return conv

    def test_no_notice_below_thresholds(self):
        conv = self._conv_with_tokens(1000)
        self.assertFalse(conv.messages.filter(role="system").exists())

    def test_price_step_then_ceiling(self):
        conv = self._conv_with_tokens(250_000)   # past long-context threshold
        notes = conv.messages.filter(role="system")
        self.assertEqual([n.notice_kind for n in notes], ["long_context"])
        # firing again at the same tier must not duplicate
        pipeline.maybe_context_notice(conv, 260_000)
        self.assertEqual(conv.messages.filter(role="system").count(), 1)
        # crossing the ceiling adds the second, different note
        pipeline.maybe_context_notice(conv, 450_000)
        kinds = set(conv.messages.filter(role="system").values_list("notice_kind", flat=True))
        self.assertEqual(kinds, {"long_context", "context_warn"})

    def test_notice_is_not_sent_to_the_model(self):
        conv = self._conv_with_tokens(450_000)
        self.assertTrue(conv.messages.filter(role="system").exists())
        history = pipeline.build_history(conv, before_seq=9999)
        roles = [m["role"] for m in history]
        self.assertEqual(roles, ["user", "assistant"])  # no system row leaked

    def test_notice_is_not_copied_into_a_branch(self):
        conv = self._conv_with_tokens(450_000)
        c = Client()
        c.post(
            "/api/auth/login",
            json.dumps({"username": "tester", "password": "test-password-123"}),
            content_type="application/json",
        )
        anchor = conv.messages.filter(role="assistant").first()
        assert anchor is not None
        resp = c.post(f"/api/messages/{anchor.id}/branch")
        self.assertEqual(resp.status_code, 201)
        branch = Conversation.objects.get(id=resp.json()["id"])
        # a "context nearly full" note would be wrong in a fresh short branch
        self.assertFalse(branch.messages.filter(role="system").exists())


@override_settings(**TEST_CREDS)
class ReviewRegressionTests(TestCase):
    """Guards for bugs found in the independent audit."""

    def setUp(self):
        self.c = Client()
        self.c.post(
            "/api/auth/login",
            json.dumps({"username": "tester", "password": "test-password-123"}),
            content_type="application/json",
        )

    def test_forced_count_is_persisted_so_ui_and_redraw_agree(self):
        cid = self.c.post(
            "/api/conversations", "{}", content_type="application/json"
        ).json()["id"]
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=FAKE_IMAGE_RESULT
        ), mock.patch.object(
            pipeline.llm, "draw_turn", return_value=FAKE_DRAW_RESULT
        ), mock.patch.object(
            pipeline.novelai, "generate_one", return_value=(_png_bytes(), 1)
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            self.c.post(
                f"/api/conversations/{cid}/messages", {"text": "画", "n_images": "4"}
            )
        msg = Message.objects.get(conversation_id=cid, role="assistant")
        self.assertEqual(len(msg.attachments.all()), 4)
        # the stored prompts must say 4 too, or the placeholders and 重绘 lie
        prompts = msg.nai_prompts
        assert prompts is not None, "an image turn must store its prompts"
        self.assertEqual(prompts["n_images"], 4)

    def test_stopping_keeps_images_already_generated(self):
        """
        Stop means "no more", not "undo".

        A picture that exists was paid for and is what the user asked for, so
        the turn finishes as an ordinary one rather than as a cancelled one.
        That is not cosmetic: build_history excludes cancelled rows, so a
        picture kept under that status would be invisible to the model for the
        rest of the conversation.
        """
        conv = Conversation.objects.create(title="c")
        Message.objects.create(
            conversation=conv, seq=1, role="user", status="done", content="画"
        )
        msg = Message.objects.create(
            conversation=conv, seq=2, role="assistant", status="pending",
            kind="image", nai_prompts={**FAKE_DRAW_RESULT, "n_images": 1},
            heartbeat_at=timezone.now(),
        )

        def slow_generate(*_args, **_kw):
            # the user hits stop while this call is in flight
            Message.objects.filter(id=msg.id).update(status="cancelled")
            return _png_bytes(), 7

        with mock.patch.object(pipeline.novelai, "generate_one", slow_generate):
            pipeline.run_redraw(msg.id)

        msg.refresh_from_db()
        self.assertEqual(msg.status, "done")
        # the image arrived before the stop was observed — keep it
        self.assertEqual(msg.attachments.count(), 1)

    def test_branch_skips_failed_turns(self):
        conv = Conversation.objects.create(title="c")
        Message.objects.create(
            conversation=conv, seq=1, role="user", status="done", content="一"
        )
        Message.objects.create(
            conversation=conv, seq=2, role="assistant", status="error", error="boom"
        )
        anchor = Message.objects.create(
            conversation=conv, seq=3, role="user", status="done", content="二"
        )
        resp = self.c.post(f"/api/messages/{anchor.id}/branch")
        branch = Conversation.objects.get(id=resp.json()["id"])
        contents = list(branch.messages.values_list("content", flat=True))
        self.assertEqual(contents, ["一", "二"])  # the failed turn is not resurrected

    def test_client_ip_ignores_spoofed_left_entries(self):
        from django.test import RequestFactory

        from core.http import client_ip

        req = RequestFactory().get("/")
        # a caller-forged value, then the real client, then our own nginx
        req.META["HTTP_X_FORWARDED_FOR"] = "9.9.9.9, 203.0.113.7, 172.18.0.2"
        with override_settings(TRUSTED_PROXY_HOPS=1):
            self.assertEqual(client_ip(req), "203.0.113.7")


class EveryPictureHasToBeOneOpusDrawsFreeTests(TestCase):
    """
    A size is not a taste, it is a bill.

    NovelAI's Opus tier draws for free only up to 1024x1024 worth of pixels
    (one at a time, no base image, steps <= 28). Past that every generation
    bills Anlas, and on an account whose Anlas is spent that is not a worse
    picture — it is a refusal, and the run stops.

    Measured the hard way: the app ran at 1024x1536 for one afternoon and
    every generation failed. The size looked like a preference and was a
    payment term, so it is asserted here rather than remembered.
    """

    def test_every_free_size_is_actually_free(self):
        """All three, not just the one that gets used most."""
        for name, (w, h) in novelai.NORMAL_SIZES.items():
            with self.subTest(size=name):
                self.assertLessEqual(
                    w * h, novelai.OPUS_FREE_PIXELS,
                    f"{name} is {w}x{h} = {w * h:,} pixels, over the "
                    f"{novelai.OPUS_FREE_PIXELS:,} Opus draws free",
                )

    def test_the_two_bands_are_the_same_shapes(self):
        """
        A tier changes how big, never which way round. Which way round is the
        drawing model's decision, so a band missing one would silently redirect
        a landscape brief into a portrait.
        """
        self.assertEqual(set(novelai.NORMAL_SIZES), set(novelai.LARGE_SIZES))

    def test_the_paid_band_is_the_one_that_costs(self):
        """Measured: 1024x1536 bills 45 Anlas a picture."""
        for name, (w, h) in novelai.LARGE_SIZES.items():
            if name == "square":
                continue
            with self.subTest(size=name):
                self.assertGreater(w * h, novelai.OPUS_FREE_PIXELS)

    def test_the_free_tiers_draw_at_a_free_size(self):
        """
        The point of the low and mid tiers. Measured on a live Opus account:
        v4.5 at 832x1216 moved neither Anlas nor the V5 allowance; v5 at the
        same size moved the allowance and not Anlas.
        """
        for tier in ("low", "mid"):
            with self.subTest(tier=tier):
                sizes = novelai.tier_of(tier)["sizes"]
                self.assertIs(sizes, novelai.NORMAL_SIZES)

    def test_the_app_and_the_vendor_agree_on_what_the_tiers_are(self):
        """
        The names belong to the app and the meanings to the drawing service,
        which is the whole point of them living apart — but a tier the settings
        page offers and the drawing service has never heard of would quietly
        fall back to the default, and look like the control does nothing.
        """
        self.assertEqual(set(gensettings.QUALITY_TIERS), set(novelai.TIERS))
        self.assertIn(gensettings.DEFAULTS["quality"], novelai.TIERS)

    def test_the_tier_reaches_the_request(self):
        """A control that does not change the request is a control that lies."""
        for tier, model, band in (
            ("low", "nai-diffusion-4-5-full", novelai.NORMAL_SIZES),
            ("mid", "nai-diffusion-5-full", novelai.NORMAL_SIZES),
            ("high", "nai-diffusion-5-full", novelai.LARGE_SIZES),
        ):
            with self.subTest(tier=tier):
                p = novelai._build_payload("a", [], "", "portrait", 1, tier)
                self.assertEqual(p["model"], model)
                self.assertEqual(
                    (p["parameters"]["width"], p["parameters"]["height"]),
                    band["portrait"],
                )

    def test_the_tier_is_settled_onto_the_row_so_a_redraw_keeps_it(self):
        """
        重绘 replays the stored prompts verbatim. Without the tier stored, a
        picture redrawn after the conversation was moved to 高 would come back
        at a size that bills Anlas, which is not what redrawing asked for.
        """
        conv = Conversation.objects.create(title="档位", gen_settings={"quality": "low"})
        msg = Message.objects.create(
            conversation=conv, seq=1, role="assistant", status="pending", content=""
        )
        settled = pipeline._with_count(msg, {"base_prompt": "x", "n_images": 1})
        self.assertEqual(settled["tier"], "low")

        # moved afterwards; the row keeps what it was drawn at
        Conversation.objects.filter(id=conv.id).update(gen_settings={"quality": "high"})
        msg.refresh_from_db()
        again = pipeline._with_count(msg, settled)
        self.assertEqual(again["tier"], "low")

    def test_a_conversation_from_before_this_existed_reads_as_mid(self):
        conv = Conversation.objects.create(title="老对话", gen_settings=None)
        self.assertEqual(gensettings.of(conv)["quality"], "mid")

    def test_an_unknown_tier_still_draws(self):
        self.assertIs(
            novelai.tier_of("nonesuch")["sizes"], novelai.NORMAL_SIZES,
            "an unrecognised tier must not be the expensive one",
        )

    def test_the_steps_stay_under_the_free_ceiling(self):
        """The other half of the same deal, and just as easy to nudge."""
        payload = novelai._build_payload("a", [], "", "portrait", 1)
        self.assertLessEqual(payload["parameters"]["steps"], 28)

    def test_one_at_a_time(self):
        """More than one in a request costs Anlas even on Opus."""
        payload = novelai._build_payload("a", [], "", "portrait", 1)
        self.assertEqual(payload["parameters"]["n_samples"], 1)

    def test_the_sizes_the_model_may_ask_for_are_the_sizes_that_exist(self):
        """A name the drawing model can pick but SIZES lacks falls back
        silently to portrait, which would hide a typo rather than show it."""
        self.assertEqual(set(novelai.SIZES), {"portrait", "landscape", "square"})


class QualityWordsAreTheAppsTests(TestCase):
    """
    The quality words used to be whatever the drawing model felt like writing:
    of twelve measured calls it wrote them seven times and left them out five.
    They move 97.5% of the pixels, so "sometimes" is the worst of the three
    possible policies. NovelAI's own client appends them in the browser; the
    `qualityToggle` field in the payload does nothing on the server (measured:
    true and false draw the same image pixel for pixel), so writing them into
    the prompt is the only way to have them.
    """

    def _base(self, prompt: str, tier: str = "mid") -> str:
        return novelai._build_payload(prompt, [], "", "portrait", 1, tier)["input"]

    def test_they_are_there_whatever_the_model_wrote(self):
        for tier in novelai.TIERS:
            with self.subTest(tier=tier):
                base = self._base("1girl, solo, cafe", tier)
                for word in (w.strip() for w in novelai.QUALITY_WORDS.split(",")):
                    self.assertIn(word, base)

    def test_the_prompt_itself_survives(self):
        base = self._base("1girl, solo, cafe")
        self.assertTrue(base.startswith("1girl, solo, cafe"))

    def test_a_word_the_model_already_wrote_is_not_sent_twice(self):
        """
        Repeating a tag is how NovelAI is told to weight it more heavily, so a
        duplicate here would quietly mean something other than "be consistent".
        """
        base = self._base("1girl, very aesthetic, cafe")
        self.assertEqual(base.lower().count("very aesthetic"), 1)
        self.assertIn("masterpiece", base)

    def test_the_payload_and_the_v4_caption_say_the_same_thing(self):
        """
        Two fields carry the prompt and the model reads the second one. They
        were already built from one variable; this keeps it that way.
        """
        payload = novelai._build_payload("1girl, solo", [], "", "portrait", 1)
        self.assertEqual(
            payload["input"],
            payload["parameters"]["v4_prompt"]["caption"]["base_caption"],
        )

    def test_an_empty_prompt_still_gets_them(self):
        self.assertEqual(self._base(""), novelai.QUALITY_WORDS)

    def test_the_words_are_not_danbooru_tags_and_are_known_not_to_be(self):
        """
        Measured against the bundled index: none of NovelAI's quality words are
        tags on the site. CONVENTION_WORDS is what keeps the tag check from
        reporting the app's own vocabulary on every picture.
        """
        from core.services import danbooru

        for word in (w.strip() for w in novelai.QUALITY_WORDS.split(",")):
            self.assertIn(word, novelai.CONVENTION_WORDS)
            self.assertNotIn(danbooru.normalize(word), danbooru.index().exact)


class ReadingAPromptAsTagsTests(TestCase):
    """
    A prompt is not only tags. What may be looked up on Danbooru and what may
    not is NovelAI's syntax to decide, which is why this lives next to it.
    """

    def test_plain_tags_come_through_in_order(self):
        self.assertEqual(
            novelai.tags_to_check("1girl, solo, cafe"), ["1girl", "solo", "cafe"]
        )

    def test_the_emphasis_syntax_is_stripped_to_the_tag(self):
        self.assertEqual(novelai.tags_to_check("1.2::red dress::"), ["red dress"])
        self.assertEqual(novelai.tags_to_check("-0.8::feet::"), ["feet"])
        self.assertEqual(novelai.tags_to_check("{smile}, [blush]"), ["smile", "blush"])

    def test_parentheses_belong_to_the_tag_and_are_not_stripped(self):
        """
        A great many tags carry a parenthesised qualifier, and an early version
        of this stripped the closing one — which turned `oil painting (medium)`,
        a tag with 1,813 pictures, into an unknown tag on every painterly image.
        """
        self.assertEqual(
            novelai.tags_to_check("oil painting (medium), hatsune_miku_(vocaloid)"),
            ["oil painting (medium)", "hatsune_miku_(vocaloid)"],
        )
        from core.services import danbooru

        self.assertEqual(danbooru.unknown(["oil painting (medium)"]), [])

    def test_a_sentence_is_not_a_tag(self):
        """drawing.txt allows a clause of English for what tags cannot hold."""
        said = "1girl, a girl sits by the window while the rain runs down it, cafe"
        self.assertEqual(novelai.tags_to_check(said), ["1girl", "cafe"])

    def test_novelais_own_vocabulary_is_left_out(self):
        self.assertEqual(novelai.tags_to_check("1girl, very aesthetic, masterpiece"),
                         ["1girl"])


@override_settings(**TEST_CREDS)
class QuickPhraseTests(TestCase):
    def setUp(self):
        self.c = Client()
        self.c.post(
            "/api/auth/login",
            json.dumps({"username": "tester", "password": "test-password-123"}),
            content_type="application/json",
        )

    def _make(self, *titles):
        ids = []
        for t in titles:
            r = self.c.post(
                "/api/phrases",
                json.dumps({"title": t, "content": f"{t} 的内容"}),
                content_type="application/json",
            )
            self.assertEqual(r.status_code, 201)
            ids.append(r.json()["id"])
        return ids

    def _titles(self):
        return [p["title"] for p in self.c.get("/api/phrases").json()["phrases"]]

    def test_a_new_phrase_goes_at_the_end(self):
        """
        The list is hand-ordered, so dropping a new one into the middle would
        rearrange someone's muscle memory on their behalf.
        """
        self._make("一", "二", "三")
        self.assertEqual(self._titles(), ["一", "二", "三"])

    def test_they_stay_where_they_were_put(self):
        ids = self._make("一", "二", "三")
        resp = self.c.post(
            "/api/phrases/reorder",
            json.dumps({"ids": [ids[2], ids[0], ids[1]]}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([p["title"] for p in resp.json()["phrases"]], ["三", "一", "二"])
        self.assertEqual(self._titles(), ["三", "一", "二"], "and it survives a reload")

    def test_one_the_browser_had_not_heard_of_is_not_lost(self):
        """
        A phrase created in another tab mid-drag. The order arriving does not
        mention it, and the two readings of "not mentioned" are silently
        dropping it and silently promoting it to the front. It keeps its place
        after the ones that were named.
        """
        ids = self._make("一", "二", "三")
        # the one left out is the one sitting FIRST, so "keeps its own place"
        # and "goes after the named ones" cannot both look right
        resp = self.c.post(
            "/api/phrases/reorder",
            json.dumps({"ids": [ids[2], ids[1]]}),
            content_type="application/json",
        )
        self.assertEqual([p["title"] for p in resp.json()["phrases"]], ["三", "二", "一"])

    def test_an_order_naming_nothing_real_changes_nothing(self):
        self._make("一", "二")
        resp = self.c.post(
            "/api/phrases/reorder",
            json.dumps({"ids": ["not-an-id"]}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._titles(), ["一", "二"])

    def test_reorder_rejects_what_is_not_an_order(self):
        for body in ({"ids": "一二三"}, {"ids": [1, 2]}, {}):
            with self.subTest(body=body):
                resp = self.c.post(
                    "/api/phrases/reorder",
                    json.dumps(body),
                    content_type="application/json",
                )
                self.assertEqual(resp.status_code, 400)

    def test_crud(self):
        # create
        resp = self.c.post(
            "/api/phrases",
            json.dumps({"title": "质量词", "content": "best quality, absurdres"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        pid = resp.json()["id"]
        # list
        listed = self.c.get("/api/phrases").json()["phrases"]
        self.assertEqual([p["title"] for p in listed], ["质量词"])
        # update (partial)
        resp = self.c.patch(
            f"/api/phrases/{pid}",
            json.dumps({"title": "改名"}),
            content_type="application/json",
        )
        self.assertEqual(resp.json()["title"], "改名")
        self.assertEqual(resp.json()["content"], "best quality, absurdres")
        # validation
        bad = self.c.post(
            "/api/phrases",
            json.dumps({"title": "", "content": "x"}),
            content_type="application/json",
        )
        self.assertEqual(bad.status_code, 400)
        # delete
        self.assertEqual(self.c.delete(f"/api/phrases/{pid}").status_code, 200)
        self.assertEqual(self.c.get("/api/phrases").json()["phrases"], [])

    def test_requires_auth(self):
        self.assertEqual(Client().get("/api/phrases").status_code, 401)


@override_settings(**TEST_CREDS)
class ReferenceImagesGoStraightToTheModelTests(TestCase):
    """
    A separate vision model used to describe every upload and the text went to
    the lead model in place of the picture. The lead models read well enough now
    that the middleman only lost detail, so the picture goes as a picture.
    """

    def setUp(self):
        self.c = Client()
        self.c.post(
            "/api/auth/login",
            json.dumps({"username": "tester", "password": "test-password-123"}),
            content_type="application/json",
        )

    def _conv_with_upload(self, description=""):
        conv = Conversation.objects.create(title="带参考图")
        m = Message.objects.create(
            conversation=conv, seq=1, role="user", status="done", content="照这张画"
        )
        att = Attachment(
            message=m, kind="upload", mime="image/png", width=64, height=64,
            description=description,
        )
        data = _png_bytes()
        att.size_bytes = len(data)
        att.file.save("ref.png", ContentFile(data), save=True)
        return conv

    def _parts(self, conv):
        history = pipeline.build_history(conv, before_seq=9)
        return [p for m in history if isinstance(m["content"], list) for p in m["content"]]

    def test_the_picture_itself_is_what_goes(self):
        """
        Byte for byte, not a copy of it.

        It used to be scaled to 1344px and re-encoded as JPEG. The model is
        being asked to judge a drawing it is about to redraw, so every artifact
        introduced on the way there is one it cannot tell apart from an
        artifact in the work.
        """
        import base64

        conv = self._conv_with_upload()
        parts = self._parts(conv)
        self.assertEqual([p["type"] for p in parts if p["type"] == "image_url"], ["image_url"])

        url = parts[-1]["image_url"]["url"]
        att = Attachment.objects.filter(kind="upload").first()
        assert att is not None
        head, _, b64 = url.partition(",")
        self.assertEqual(head, f"data:{att.mime};base64")
        self.assertEqual(base64.b64decode(b64), att.file.read())

    def test_it_is_not_the_thumbnail(self):
        """
        Two copies of a picture exist and they are for different readers. The
        small one is what a browser is sent first; the model gets the picture.
        """
        from core.services import imaging

        conv = self._conv_with_upload()
        att = Attachment.objects.filter(kind="upload").first()
        assert att is not None
        imaging.make_thumbnail(att)
        att.refresh_from_db()
        self.assertTrue(att.thumb)

        url = self._parts(conv)[-1]["image_url"]["url"]
        import base64

        _, _, b64 = url.partition(",")
        self.assertEqual(base64.b64decode(b64), att.file.read())
        self.assertNotEqual(base64.b64decode(b64), att.thumb.read())

    def test_an_old_turns_description_no_longer_stands_in_for_it(self):
        """Rows described back then keep the text; the picture still goes."""
        parts = self._parts(self._conv_with_upload(description="一座灯塔"))
        self.assertTrue(any(p["type"] == "image_url" for p in parts))
        self.assertNotIn("一座灯塔", " ".join(p.get("text", "") for p in parts))

    def test_no_separate_describing_call_is_made(self):
        """The step is gone entirely — not merely skipped when unconfigured."""
        self.assertFalse(hasattr(pipeline, "_ensure_descriptions"))
        from core.services import imaging

        self.assertFalse(hasattr(imaging, "describe_image"))

    def test_there_is_no_vision_prompt_left_to_load(self):
        from core import prompts

        self.assertFalse(hasattr(prompts, "vision"))
        self.assertFalse((prompts.PROMPT_DIR / "vision.txt").exists())


@override_settings(**TEST_CREDS)
class InternalApiTests(TestCase):
    def _headers(self, token="mcp-token-abc"):
        return {"Authorization": f"Bearer {token}"}

    def test_auth_required(self):
        c = Client()
        self.assertEqual(c.get("/api/internal/quota").status_code, 401)
        self.assertEqual(
            c.get("/api/internal/quota", headers=self._headers("wrong")).status_code,
            401,
        )

    def test_internal_chat(self):
        c = Client()
        # MCP now runs the conversation's own reflection setting (one round by
        # default), which would call a real provider here
        with mock.patch.object(pipeline, "_reflection_rounds", return_value=0), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=FAKE_IMAGE_RESULT
        ), mock.patch.object(
            pipeline.llm, "draw_turn", return_value=FAKE_DRAW_RESULT
        ), mock.patch.object(
            pipeline.novelai, "generate_one", return_value=(_png_bytes(), 5)
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            resp = c.post(
                "/api/internal/chat",
                json.dumps({"message": "画一个女孩", "n_images": 2}),
                content_type="application/json",
                headers=self._headers(),
            )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["images"]), 2)
        self.assertTrue(data["images"][0]["data_base64"])
        self.assertEqual(data["images"][0]["content_type"], "image/png")
        # archived in the web DB too
        self.assertEqual(Conversation.objects.filter(source="mcp").count(), 1)

    def test_internal_generate(self):
        c = Client()
        with mock.patch.object(
            pipeline.novelai, "generate_one", return_value=(_png_bytes(), 5)
        ):
            resp = c.post(
                "/api/internal/generate",
                json.dumps({"base_prompt": "1girl, best quality", "n_images": 2}),
                content_type="application/json",
                headers=self._headers(),
            )
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["images"]), 2)
        self.assertEqual(Attachment.objects.filter(kind="generated").count(), 2)


@override_settings(**TEST_CREDS)
class ProjectTests(TestCase):
    """Folders, placement, and the library they carry."""

    def setUp(self):
        self.c = Client()
        self.c.post(
            "/api/auth/login",
            json.dumps({"username": "tester", "password": "test-password-123"}),
            content_type="application/json",
        )

    def _folder(self, name, parent=None):
        return self.c.post(
            "/api/folders",
            json.dumps({"name": name, "parent_id": parent}),
            content_type="application/json",
        ).json()

    def _conv(self, title="对话"):
        return self.c.post(
            "/api/conversations",
            json.dumps({"title": title}),
            content_type="application/json",
        ).json()

    def _move(self, kind, item_id, parent_id, index=0):
        return self.c.post(
            "/api/projects/move",
            json.dumps({"kind": kind, "id": item_id, "parent_id": parent_id,
                        "index": index}),
            content_type="application/json",
        )

    def test_new_folder_goes_to_the_top(self):
        first = self._folder("先建的")
        second = self._folder("后建的")
        tree = self.c.get("/api/projects/tree").json()
        names = [f["name"] for f in sorted(tree["folders"], key=lambda f: f["position"])]
        self.assertEqual(names, ["后建的", "先建的"])
        self.assertLess(second["position"], first["position"])

    def test_move_conversation_into_folder(self):
        f = self._folder("项目 A")
        conv = self._conv()
        self.assertEqual(self._move("conversation", conv["id"], f["id"]).status_code, 200)
        detail = self.c.get(f"/api/conversations/{conv['id']}/messages").json()
        self.assertEqual(detail["conversation"]["folder_id"], f["id"])
        self.assertEqual(detail["conversation"]["folder_path"], ["项目 A"])

    def test_folder_cannot_be_dragged_into_itself(self):
        parent = self._folder("外")
        child = self._folder("内", parent["id"])
        self.assertEqual(self._move("folder", parent["id"], child["id"]).status_code, 400)

    def test_a_folder_cannot_be_deleted_at_all(self):
        """
        Folders are archived, never deleted — the same rule conversations
        live by. The endpoint that used to be here took the whole library
        with it, unconfirmed and untraceable.
        """
        f = self._folder("临时")
        conv = self._conv()
        self._move("conversation", conv["id"], f["id"])
        self.assertEqual(self.c.delete(f"/api/folders/{f['id']}").status_code, 405)
        self.assertTrue(Folder.objects.filter(id=f["id"]).exists())
        self.assertEqual(
            Conversation.objects.get(id=conv["id"]).folder_id, uuid.UUID(f["id"])
        )

    def test_library_is_inherited_downwards(self):
        outer = self._folder("外层")
        inner = self._folder("内层", outer["id"])
        self.c.post(
            f"/api/folders/{outer['id']}/settings",
            json.dumps({"title": "统一画风", "content": "水彩"}),
            content_type="application/json",
        )
        self.c.post(
            f"/api/folders/{inner['id']}/characters",
            json.dumps({"name": "小鹿", "description": "银发",
                        "variant_title": "常服", "variant_content": "白裙"}),
            content_type="application/json",
        )
        lib = self.c.get(f"/api/folders/{inner['id']}/library").json()
        self.assertEqual([s["title"] for s in lib["settings"]], [])
        self.assertEqual([s["title"] for s in lib["inherited"]["settings"]], ["统一画风"])
        self.assertEqual([c["name"] for c in lib["characters"]], ["小鹿"])
        # a character always starts with one variant, so it is usable at once
        self.assertEqual(len(lib["characters"][0]["variants"]), 1)

    def test_selection_is_limited_to_what_the_folder_can_reach(self):
        a = self._folder("A")
        b = self._folder("B")
        s_a = self.c.post(
            f"/api/folders/{a['id']}/settings",
            json.dumps({"title": "A 的设定", "content": "x"}),
            content_type="application/json",
        ).json()
        s_b = self.c.post(
            f"/api/folders/{b['id']}/settings",
            json.dumps({"title": "B 的设定", "content": "y"}),
            content_type="application/json",
        ).json()
        conv = self._conv()
        self.c.post(
            f"/api/conversations/{conv['id']}/folder",
            json.dumps({"folder_id": a["id"]}),
            content_type="application/json",
        )
        resp = self.c.post(
            f"/api/conversations/{conv['id']}/selection",
            json.dumps({"setting_ids": [s_a["id"], s_b["id"]], "variant_ids": []}),
            content_type="application/json",
        ).json()
        chosen = [s["title"] for s in resp["settings"] if s["selected"]]
        self.assertEqual(chosen, ["A 的设定"])   # B is out of reach, silently ignored

    def test_moving_out_of_a_folder_drops_unreachable_selections(self):
        f = self._folder("F")
        s = self.c.post(
            f"/api/folders/{f['id']}/settings",
            json.dumps({"title": "设定", "content": "x"}),
            content_type="application/json",
        ).json()
        conv = self._conv()
        self.c.post(
            f"/api/conversations/{conv['id']}/folder",
            json.dumps({"folder_id": f["id"]}),
            content_type="application/json",
        )
        self.c.post(
            f"/api/conversations/{conv['id']}/selection",
            json.dumps({"setting_ids": [s["id"]], "variant_ids": []}),
            content_type="application/json",
        )
        self.c.post(
            f"/api/conversations/{conv['id']}/folder",
            json.dumps({"folder_id": None}),
            content_type="application/json",
        )
        self.assertEqual(
            Conversation.objects.get(id=conv["id"]).selected_settings.count(), 0
        )


@override_settings(**TEST_CREDS)
class TwoModelWorkflowTests(TestCase):
    """The lead model briefs; the drawing model writes tags; edits need consent."""

    def setUp(self):
        self.c = Client()
        self.c.post(
            "/api/auth/login",
            json.dumps({"username": "tester", "password": "test-password-123"}),
            content_type="application/json",
        )
        self.folder = self.c.post(
            "/api/folders", json.dumps({"name": "项目"}), content_type="application/json"
        ).json()
        self.conv = self.c.post(
            "/api/conversations", "{}", content_type="application/json"
        ).json()
        self.c.post(
            f"/api/conversations/{self.conv['id']}/folder",
            json.dumps({"folder_id": self.folder["id"]}),
            content_type="application/json",
        )

    def _send(self, lead_result, text="画一张"):
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=lead_result
        ) as lead, mock.patch.object(
            pipeline.llm, "draw_turn", return_value=FAKE_DRAW_RESULT
        ) as draw, mock.patch.object(
            pipeline.novelai, "generate_one", return_value=(_png_bytes(), 1)
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            self.c.post(f"/api/conversations/{self.conv['id']}/messages", {"text": text, "reflection_rounds": "0"})
        return lead, draw

    def _proposals(self):
        return self.c.get(
            f"/api/conversations/{self.conv['id']}/proposals"
        ).json()["proposals"]

    def test_brief_goes_to_the_drawing_model_not_to_novelai(self):
        _lead, draw = self._send(FAKE_IMAGE_RESULT)
        self.assertTrue(draw.called)
        brief = draw.call_args[0][0]
        self.assertEqual(brief["main"], "一个银发少女站在花园里")
        msg = Message.objects.get(conversation_id=self.conv["id"], role="assistant")
        # the transcript keeps both halves: the intent and the resulting tags
        assert msg.draw_brief is not None
        self.assertEqual(msg.draw_brief["main"], "一个银发少女站在花园里")
        assert msg.nai_prompts is not None
        self.assertEqual(msg.nai_prompts["base_prompt"], "1girl, garden, best quality")

    def test_no_brief_means_no_drawing_call(self):
        _lead, draw = self._send(FAKE_TEXT_RESULT, text="你好")
        self.assertFalse(draw.called)

    def test_selection_reaches_both_models(self):
        char = self.c.post(
            f"/api/folders/{self.folder['id']}/characters",
            json.dumps({"name": "小鹿", "description": "银发少女",
                        "variant_title": "常服", "variant_content": "白色连衣裙"}),
            content_type="application/json",
        ).json()
        self.c.post(
            f"/api/conversations/{self.conv['id']}/selection",
            json.dumps({"setting_ids": [],
                        "variant_ids": [char["variants"][0]["id"]]}),
            content_type="application/json",
        )
        lead, draw = self._send(FAKE_IMAGE_RESULT)

        # the lead model sees it appended to the user's turn
        joined = json.dumps(lead.call_args[0][0], ensure_ascii=False)
        self.assertIn("小鹿", joined)
        self.assertIn("白色连衣裙", joined)
        # and the drawing model, which has nothing else to go on, gets it too —
        # as prose, since this entry has no tag lines yet
        self.assertIn("白色连衣裙", draw.call_args[0][1])
        # the turn records what was selected, so the image can say so later
        msg = Message.objects.get(conversation_id=self.conv["id"], role="assistant")
        assert msg.selection_snapshot is not None
        self.assertEqual(msg.selection_snapshot["characters"][0]["name"], "小鹿")

    def test_edits_are_only_proposed_never_written(self):
        result = {
            **FAKE_TEXT_RESULT,
            "edits": _project({
                "settings": [{"title": "夜景", "content": "冷色调，霓虹"}],
                "characters": [
                    {"name": "小鹿", "description": "银发",
                     "variants": [{"title": "泳装", "content": "蓝色比基尼"}]}
                ],
            }),
        }
        self._send(result, text="把小鹿的泳装记下来")
        # nothing written yet
        self.assertEqual(Setting.objects.count(), 0)
        self.assertEqual(Character.objects.count(), 0)

        proposals = self._proposals()
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["status"], "pending")

        # the user agrees → now it lands
        resp = self.c.post(
            f"/api/proposals/{proposals[0]['id']}/resolve",
            json.dumps({"approve": True}),
            content_type="application/json",
        ).json()
        self.assertEqual(resp["status"], "applied")
        self.assertEqual(Setting.objects.get(title="夜景").content, "冷色调，霓虹")
        self.assertEqual(
            CharacterVariant.objects.get(title="泳装").content, "蓝色比基尼"
        )

    def test_a_pending_proposal_survives_the_next_message(self):
        result = {
            **FAKE_TEXT_RESULT,
            "edits": _project({"settings": [{"title": "夜景", "content": "x"}], "characters": []}),
        }
        self._send(result, text="记一下")
        self._send(FAKE_TEXT_RESULT, text="先聊点别的")
        pending = [p for p in self._proposals() if p["status"] == "pending"]
        self.assertEqual(len(pending), 1)   # still there, still approvable

    def test_proposal_edits_existing_entries_by_title(self):
        self.c.post(
            f"/api/folders/{self.folder['id']}/settings",
            json.dumps({"title": "夜景", "content": "旧内容"}),
            content_type="application/json",
        )
        result = {
            **FAKE_TEXT_RESULT,
            "edits": _project({"settings": [{"title": "夜景", "content": "新内容"}],
                               "characters": []}),
        }
        self._send(result, text="改一下夜景")
        pid = self._proposals()[0]["id"]
        self.c.post(
            f"/api/proposals/{pid}/resolve",
            json.dumps({"approve": True}),
            content_type="application/json",
        )
        self.assertEqual(Setting.objects.count(), 1)          # edited, not duplicated
        self.assertEqual(Setting.objects.get(title="夜景").content, "新内容")

    def test_rejecting_a_proposal_writes_nothing(self):
        result = {
            **FAKE_TEXT_RESULT,
            "edits": _project({"settings": [{"title": "夜景", "content": "x"}], "characters": []}),
        }
        self._send(result, text="记一下")
        pid = self._proposals()[0]["id"]
        resp = self.c.post(
            f"/api/proposals/{pid}/resolve",
            json.dumps({"approve": False}),
            content_type="application/json",
        ).json()
        self.assertEqual(resp["status"], "rejected")
        self.assertEqual(Setting.objects.count(), 0)

    def test_branch_lands_in_the_same_folder_with_the_same_selection(self):
        s = self.c.post(
            f"/api/folders/{self.folder['id']}/settings",
            json.dumps({"title": "画风", "content": "水彩"}),
            content_type="application/json",
        ).json()
        self.c.post(
            f"/api/conversations/{self.conv['id']}/selection",
            json.dumps({"setting_ids": [s["id"]], "variant_ids": []}),
            content_type="application/json",
        )
        self._send(FAKE_TEXT_RESULT, text="你好")
        anchor = Message.objects.filter(
            conversation_id=self.conv["id"], role="assistant"
        ).first()
        assert anchor is not None
        branch = self.c.post(f"/api/messages/{anchor.id}/branch").json()
        self.assertEqual(branch["folder_id"], self.folder["id"])
        self.assertEqual(
            Conversation.objects.get(id=branch["id"]).selected_settings.count(), 1
        )


class ProviderRegistryTests(TestCase):
    """
    The seam a second vendor goes through. These are about the contract, not
    about anybody's API — tmp/probe_provider.py exercises the real endpoints.
    """

    def test_every_registered_provider_answers_the_whole_interface(self):
        for name, cls in providers.REGISTRY.items():
            p = cls()
            self.assertEqual(p.name, name, "REGISTRY key must match provider.name")
            self.assertTrue(p.label, f"{name} needs a display label")
            self.assertIsInstance(p.capabilities, providers.Capabilities)
            self.assertGreater(p.context_limit, 0)
            self.assertGreater(p.long_context_threshold, 0)
            self.assertLessEqual(p.long_context_threshold, p.context_limit)

    def test_the_switch_selects_the_named_provider(self):
        with override_settings(LLM_PROVIDER="deepseek"):
            self.assertEqual(providers.active().name, "deepseek")
        with override_settings(LLM_PROVIDER="grok"):
            self.assertEqual(providers.active().name, "grok")

    def test_an_unknown_name_says_so_instead_of_falling_back(self):
        with override_settings(LLM_PROVIDER="gemini"):
            with self.assertRaises(providers.LLMError) as caught:
                providers.active()
        # the message has to name the options, or the typo costs an hour
        self.assertIn("deepseek", str(caught.exception))
        self.assertIn("grok", str(caught.exception))

    def test_the_context_notice_follows_the_provider(self):
        """A 300k conversation is long on Grok and unremarkable on DeepSeek."""
        def notices(provider):
            conv = Conversation.objects.create(title="c")
            Message.objects.create(
                conversation=conv, seq=1, role="user", status="done", content="hi"
            )
            with override_settings(LLM_PROVIDER=provider):
                pipeline.maybe_context_notice(conv, 300_000)
            return list(
                conv.messages.filter(role="system").values_list("notice_kind", flat=True)
            )

        self.assertEqual(notices("grok"), ["long_context"])   # past its 200k step
        self.assertEqual(notices("deepseek"), [])             # 400k mark, 1M ceiling


@override_settings(LLM_PROVIDER="deepseek", DEEPSEEK_RETRIES=1)
class DeepSeekRetryTests(TestCase):
    """
    DeepSeek answers with something unusable now and then — its own docs say
    content can come back empty — so one more attempt is made before the turn
    is called a failure. Driven through a fake transport: the point is the
    decision, not their uptime.
    """

    def _replies(self, *bodies):
        """Queue up raw API responses, oldest first."""
        it = iter(bodies)

        def fake_post(url, headers, payload, timeout, who, retries=3, answer_seconds=None):
            return next(it)

        return mock.patch.object(providers.deepseek, "post_with_retry", fake_post)

    @staticmethod
    def _resp(content, finish="stop"):
        return {
            "choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }

    def _complete(self, *bodies, on_retry=None):
        with self._replies(*bodies):
            return providers.active().complete(
                system="s", messages=[], schema=llm.DRAW_SCHEMA, timeout=1,
                on_retry=on_retry,
            )

    def test_unparseable_json_is_retried_once_and_reported(self):
        seen = []
        out = self._complete(
            self._resp("对不起，我没法照做"),
            self._resp('{"base_prompt": "1girl"}'),
            on_retry=seen.append,
        )
        self.assertEqual(out.parsed["base_prompt"], "1girl")
        self.assertEqual(out.retries, 1)
        self.assertEqual(len(seen), 1, "the caller must be told, so it can say so")

    def test_empty_content_is_the_same_budget(self):
        """Its docs call this out by name; it is not a separate allowance."""
        out = self._complete(self._resp(""), self._resp('{"base_prompt": "x"}'))
        self.assertEqual(out.retries, 1)

    def test_a_truncated_answer_is_retried_rather_than_half_parsed(self):
        out = self._complete(
            self._resp('{"base_prompt": "1girl, garde', finish="length"),
            self._resp('{"base_prompt": "x"}'),
        )
        self.assertEqual(out.retries, 1)

    def test_it_gives_up_after_the_budget_rather_than_looping(self):
        seen = []
        with self.assertRaises(providers.LLMError):
            self._complete(self._resp("nope"), self._resp("still nope"), on_retry=seen.append)
        self.assertEqual(len(seen), 1, "one retry means exactly one extra attempt")

    def test_a_good_first_answer_costs_nothing_extra(self):
        out = self._complete(self._resp('{"base_prompt": "x"}'))
        self.assertEqual(out.retries, 0)

    @override_settings(DEEPSEEK_RETRIES=0)
    def test_the_budget_can_be_switched_off(self):
        with self.assertRaises(providers.LLMError):
            self._complete(self._resp(""))

    def test_the_retry_count_reaches_the_turn_so_the_ui_can_show_it(self):
        with self._replies(
            self._resp(""), self._resp('{"scene": "x", "size": "portrait"}')
        ):
            out = llm.draw_turn({"main": "灯塔", "size": "portrait"})
        self.assertEqual(out["usage"]["retries"], 1)


@override_settings(LLM_PROVIDER="deepseek")
class ReasoningIsKeptTests(TestCase):
    """
    The model's own working is the honest answer to "why these tags"; it was
    being read off the wire and dropped on the floor.
    """

    def _reply(self, content, reasoning):
        return {
            "choices": [{"message": {"content": content, "reasoning_content": reasoning},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2},
        }

    def _complete(self, reply):
        with mock.patch.object(providers.deepseek, "post_with_retry",
                               lambda *a, **k: reply):
            return providers.active().complete(
                system="s", messages=[], schema=llm.DRAW_SCHEMA, timeout=1
            )

    def test_it_comes_back_on_the_completion(self):
        out = self._complete(self._reply('{"base_prompt": "x"}', "想了想：先定人数 tag。"))
        self.assertEqual(out.reasoning, "想了想：先定人数 tag。")

    def test_a_provider_that_sends_none_is_not_a_problem(self):
        reply = self._reply('{"base_prompt": "x"}', "")
        del reply["choices"][0]["message"]["reasoning_content"]
        self.assertEqual(self._complete(reply).reasoning, "")

    def test_a_runaway_chain_is_clipped_rather_than_stored_whole(self):
        out = self._complete(self._reply('{"base_prompt": "x"}', "长" * 50_000))
        self.assertLess(len(out.reasoning), 50_000)
        self.assertIn("已截断", out.reasoning)

    def test_the_turn_carries_it_without_polluting_the_prompts(self):
        """nai_prompts is replayed verbatim by 重绘 — nothing extra may ride in it."""
        with mock.patch.object(
            providers.deepseek, "post_with_retry",
            lambda *a, **k: self._reply('{"scene": "x", "size": "portrait"}', "因为…"),
        ):
            drawn = llm.draw_turn({"main": "灯塔", "size": "portrait"})
        self.assertEqual(drawn["reasoning"], "因为…")
        # and llm.prompts_from is what strips it — asked here rather than
        # mirrored, so that this cannot pass while the pipeline keeps a key the
        # test does not know about
        self.assertIn("reasoning", drawn)
        self.assertNotIn("reasoning", llm.prompts_from(drawn))
        self.assertNotIn("tool_runs", llm.prompts_from(drawn))


class AssembledWordingTests(TestCase):
    """
    Everything the models read that is not a .txt file lives in one module, so
    it can be reviewed. These pin the two things that keep going wrong: a
    string sneaking back into the code, and a label quietly turning into an
    instruction that overrides the prompt it contradicts.
    """

    FILES = [
        "core/services/llm.py",
        "core/services/pipeline.py",
        "core/services/library.py",
        "core/services/imaging.py",
        "core/services/danbooru.py",
        "core/services/providers/base.py",
    ]

    #: Chinese that stays in code on purpose: it is shown to the user, never
    #: sent to a model. Keyed by file, with why.
    UI_COPY = {
        "core/services/llm.py": ["抱歉，我走神了"],
        "core/services/pipeline.py": ["万", "新对话", "这个对话已经用掉约", "接近模型上限",
                                      "超出后最早的消息", "或用某条消息下的", "出图数量"],
        "core/services/providers/base.py": ["后面还有", "这个对话的上下文已超过",
                                            "继续聊没问题"],
    }

    def test_no_model_facing_string_is_left_in_the_code(self):
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        strays = []
        for rel in self.FILES:
            for i, line in enumerate(Path(root, rel).read_text(encoding="utf-8").splitlines(), 1):
                t = line.strip()
                if t.startswith("#") or not re.search(r"[\"'][^\"']*[一-鿿]", line):
                    continue
                if any(w in line for w in self.UI_COPY.get(rel, [])):
                    continue
                strays.append(f"{rel}:{i}  {t[:70]}")
        self.assertEqual(strays, [], "move these into core/prompts/wording.py")

    #: What each placeholder in wording.py is filled with, and by whom. This
    #: table IS the contract — str.format has no other one — so renaming a
    #: placeholder in wording.py fails here instead of mid-turn with a KeyError.
    PLACEHOLDERS = {
        "LIBRARY_VARIANT_LABEL": {"title"},     # library.as_prompt_text
        "APP_NOTE_REFERENCES_SKIPPED": {"n"},   # pipeline.build_history
        "SYSTEM_NOTE_DRAW": {"id"},             # drawnote.lines
                "TITLE_USER": {"user", "reply"},        # llm.make_title
        "SCHEMA_PROSE_LISTS": {"fields"},       # providers.base.schema_prose
        "SCHEMA_PROSE_NULLABLE": {"fields"},    # providers.base.schema_prose
        "SCHEMA_PROSE_LIST_CAP": {"field", "cap"},
        "TOOL_TAGS_QUERY": {"query"},            # danbooru.answer
        "TOOL_TAGS_TRIMMED": {"n"},              # danbooru.answer
        "CURRENT_SETTING": {"title"},                         # llm.format_current
        "SUBJECT_KEY": {"n"},                                 # services.prompt.from_model
        "LIBRARY_IN_PROJECT": {"project"},                    # library.as_prompt_text
        "LIBRARY_CHARACTER_HEADING": {"name"},                # library.as_prompt_text
        "LIBRARY_SETTING_HEADING": {"title"},                 # library.as_prompt_text
        "LIBRARY_OFF_CHARACTER": {"name", "text"},            # library._off_entries
        "LIBRARY_OFF_VARIANTS": {"titles"},                   # library._off_entries
        "LIBRARY_OFF_SETTING": {"title", "text"},             # library._off_entries
        "CURRENT_SUBJECT": {"key"},                           # llm.format_current
        "CURRENT_SUBJECT_FROM": {"name"},                     # llm.format_current
        "SYSTEM_NOTE_MAIN": {"id", "n"},                      # views.chat.set_main_image
        "LINES_FOR_SETTING": {"title"},             # services.library_lines.brief
        "LINES_FOR_CHARACTER": {"name"},            # services.library_lines.brief
        "LINES_FOR_VARIANT": {"name", "title"},     # services.library_lines.brief
    }

    #: The same contract for core/ui_text.py, the sentences a person reads.
    UI_PLACEHOLDERS = {
        "TOO_MANY_ATTEMPTS": {"minutes"},            # views.auth
        "LANGUAGE_UNKNOWN": {"codes"},               # views.appsettings
        "TITLE_REQUIRED": {"max"},                   # views.phrases / views.projects
        "NAME_REQUIRED": {"max"},                    # views.projects
        "CONTENT_REQUIRED": {"max"},                 # views.phrases
        "FOLDER_TOO_DEEP": {"max"},                  # views.projects
        "IMAGE_TOO_BIG": {"mb"},                     # views.chat
        "UNSUPPORTED_IMAGE_FORMAT": {"fmt"},         # views.chat
        "TOO_MANY_IMAGES_UPLOAD": {"max"},           # views.chat
        "TOO_MANY_IMAGES": {"max"},                  # views.internal
        "FILE_REJECTED": {"name", "why"},            # views.chat
        "IMAGE_MISSING_DATA": {"index"},             # views.internal
        "IMAGE_DECODE_FAILED": {"index", "error"},   # views.internal
        "IMAGE_OVER_LIMIT": {"index"},               # views.internal
        "IMAGE_INVALID": {"index", "error"},         # views.internal
        "BRANCH_TITLE": {"title"},                   # views.chat.branch
        "CONTEXT_NEAR_LIMIT": {"used", "limit"},     # pipeline.context_note
        "LONG_CONTEXT": {"threshold"},               # providers.base
        "LONG_CONTEXT_PRICED": {"threshold"},        # providers.xai
        "REASONING_CLIPPED": {"chars"},              # providers.base.clip_reasoning
        "CONTINUOUS_START": {"of"},                             # continuous.arm
        "CONTINUOUS_SUMMARY": {"head", "of", "done", "failed"},  # continuous._close
        "PROMPT_UNKNOWN_LINES": {"who", "lines"},   # services.prompt.check
        "PROMPT_UNKNOWN_SETTING_LINES": {"title", "lines"},   # services.prompt.check
        "PATCH_FIELD_HALVES": {"where", "key"},               # services.prompt.apply
        "PROMPT_IDENTITY_LINE_MISSING": {"who", "lines"},    # services.prompt.check
        "PROMPT_PATCH_ON_UNPICKED_LINE": {"who", "lines"},    # services.prompt.check
        "PROMPT_LOOK_ECHOES_LINES": {"who", "tags"},          # services.prompt.check
        "PROMPT_SENTENCE_IN_TAGS": {"where", "text"},         # services.prompt.check
        "PROMPT_LINE_NOT_ENGLISH": {"who", "line"},           # services.prompt.check
        "PAINT_ONE_FAILED": {"n", "error"},                   # services.pipeline._paint
        "PROMPT_MULTI_WITHOUT_CENTER": {"who"},               # services.prompt.check
        "PROPOSAL_LINES_DROPPED": {"name", "n", "parts"},     # services.proposals
        "PROPOSAL_NAME_TAKEN": {"name"},                      # services.proposals
        "PROPOSAL_NOT_RECORDED": {"error"},                   # services.pipeline._record_proposal
        "COPY_FROM_MISSED": {"name", "picture"},              # services.pipeline._resolve_copies
        "PATCH_NOT_AN_OBJECT": {"where"},                     # services.prompt.apply
        "PATCH_OP_NOT_ALLOWED": {"where", "op", "allowed"},   # services.prompt.apply
        "PATCH_NO_SUCH_SUBJECT": {"who"},                     # services.prompt.apply
        "PATCH_NO_SUCH_SUBJECT_SLOT": {"who", "slot"},        # services.prompt.apply
        "PATCH_PATCHES_SET_ONLY": {"who"},                    # services.prompt.apply
        "PATCH_NO_SUCH_SLOT": {"slot"},                       # services.prompt.apply
        "PROPOSAL_CREATED_SETTING": {"title"},                # services.proposals
        "PROPOSAL_UPDATED_SETTING": {"title"},                # services.proposals
        "PROPOSAL_CREATED_CHARACTER": {"name"},               # services.proposals
        "PROPOSAL_UPDATED_CHARACTER": {"name"},               # services.proposals
        "PROPOSAL_CREATED_VARIANT": {"name", "title"},        # services.proposals
        "PROPOSAL_UPDATED_VARIANT": {"name", "title"},        # services.proposals
        "PROPOSAL_CREATED_SETTING_OWN": {"title"},            # services.proposals
        "PROPOSAL_CREATED_CHARACTER_OWN": {"name"},           # services.proposals
        "PROPOSAL_CREATED_VARIANT_OWN": {"name", "title"},    # services.proposals
        "MODEL_OUT_OF_TOKENS": {"name"},            # providers.deepseek.complete
        "MODEL_EMPTY": {"name"},                    # providers.deepseek.complete
        "MODEL_NOTHING_USABLE": {"name"},           # providers.deepseek.complete
        "MODEL_ANSWER_TOO_LONG": {"name", "minutes"},  # providers.base.post_with_retry
        "MODEL_UNREADABLE": {"name"},               # providers.base.parse_json_lenient
    }

    def test_every_ui_placeholder_is_one_a_caller_actually_fills_in(self):
        import re

        found = {}
        for name in dir(ui_text):
            if name.startswith("_"):
                continue
            value = getattr(ui_text, name)
            if isinstance(value, str) and re.search(r"\{\w+\}", value):
                found[name] = set(re.findall(r"\{(\w+)\}", value))
        self.assertEqual(found, self.UI_PLACEHOLDERS,
                         "a placeholder was renamed, added or removed without its caller")
        for name, keys in found.items():
            getattr(ui_text, name).format(**{k: "x" for k in keys})

    def test_every_placeholder_is_one_a_caller_actually_fills_in(self):
        import re

        found = {}
        for name in dir(wording):
            if name.startswith("_"):
                continue
            value = getattr(wording, name)
            if isinstance(value, str) and re.search(r"\{\w+\}", value):
                found[name] = set(re.findall(r"\{(\w+)\}", value))
        self.assertEqual(
            found,
            self.PLACEHOLDERS,
            "a placeholder was renamed, added or removed without its caller",
        )
        for name, keys in found.items():
            getattr(wording, name).format(**{k: "x" for k in keys})

    def test_the_library_label_states_nothing_about_what_to_do_with_it(self):
        """
        It used to end 「必须体现在提示词里」, which contradicts drawing.txt —
        that says the picture description outranks the library and only what
        is in frame gets written. A label in code outranks a prompt file in
        practice, because it arrives later and reads as more specific.
        """
        for label in (wording.LIBRARY_BLOCK_HEADING, wording.BRIEF_LINES_HEADING):
            for imperative in ("必须", "要", "不要", "应该", "请"):
                self.assertNotIn(imperative, label, f"{label!r} is instructing")

    def test_an_empty_library_is_stated_rather_than_left_out(self):
        conv = Conversation.objects.create(title="c")
        Message.objects.create(
            conversation=conv, seq=1, role="user", status="done", content="画一张"
        )

        # a real Selection with nothing ticked: falsy, but not None — which is
        # exactly the case that used to produce no block at all
        empty = library.Selection(settings=[], variants=[])
        self.assertFalse(empty)
        history = pipeline.build_history(conv, before_seq=9, selection=empty)
        joined = " ".join(
            p.get("text", "") for m in history if isinstance(m["content"], list)
            for p in m["content"]
        )
        self.assertIn(wording.LIBRARY_EMPTY, joined)

    def test_the_drawing_model_is_told_so_too(self):
        with mock.patch.object(llm, "_run", return_value=({"scene": "x"}, {})) as run:
            llm.draw_turn({"composition": "灯塔", "size": "portrait"}, "")
        sent = run.call_args[0][1][0]["content"]
        self.assertIn(wording.BRIEF_LINES_HEADING, sent)
        self.assertIn(wording.LIBRARY_NO_LINES, sent)

    def test_a_selected_library_still_arrives_under_its_heading(self):
        with mock.patch.object(llm, "_run", return_value=({"scene": "x"}, {})) as run:
            llm.draw_turn({"composition": "灯塔", "size": "portrait"}, "◆ 设定「统一画风」：厚涂")
        sent = run.call_args[0][1][0]["content"]
        self.assertIn(wording.BRIEF_LINES_HEADING, sent)
        self.assertIn("厚涂", sent)


class SchemaProseTests(TestCase):
    """
    What a provider without native structured output is told instead.

    Generated from the very schema the strict providers are handed, so the two
    cannot drift; and DeepSeek's docs require the literal word "json" and a
    sample structure to be present, which is easy to delete by accident.
    """

    def test_it_carries_the_two_things_deepseeks_docs_require(self):
        prose = providers.base.schema_prose(llm.DRAW_SCHEMA)
        self.assertIn("json", prose.lower())
        self.assertIn("{", prose)

    def test_it_describes_the_real_shape(self):
        prose = providers.base.schema_prose(llm.DRAW_SCHEMA)
        for field in ("count", "style", "camera", "scene", "light",
                      "characters", "negative_prompt", "size"):
            self.assertIn(field, prose)
        # dropped from both schemas — the app decides how many to paint
        self.assertNotIn("n_images", prose)

    def test_a_nullable_branch_shows_the_substantive_side(self):
        """`draw` is object-or-null; a skeleton of `null` would teach nothing."""
        prose = providers.base.schema_prose(llm.LEAD_SCHEMA)
        for field in ("reply", "draw", "subjects", "composition", "scene", "camera"):
            self.assertIn(field, prose)

    def test_unparseable_output_names_the_provider(self):
        with self.assertRaises(providers.LLMError) as caught:
            providers.base.parse_json_lenient("sorry, I cannot", "deepseek")
        self.assertIn("deepseek", str(caught.exception))

    def test_reflection_is_described_exactly_like_an_ordinary_turn(self):
        """
        Reflection is the same model under the same system prompt, so it
        answers in the same shape. lead.txt describes one output format; a
        reflection turn that wanted a different one would leave the model with
        two descriptions and nothing to tell it which turn it is in.
        """
        self.assertEqual(llm.REFLECT_SCHEMA["schema"], llm.LEAD_SCHEMA["schema"])
        self.assertEqual(
            providers.base.schema_prose(llm.REFLECT_SCHEMA),
            providers.base.schema_prose(llm.LEAD_SCHEMA),
        )


class AnswersThatAlmostParseTests(TestCase):
    """
    A model writing several lines of Chinese into a JSON string will now and
    then write the line break itself rather than \\n. The answer is otherwise
    exactly what was asked for, and the whole turn used to fail on it — with
    an error whose visible part was the picture description, because that is
    what the first 300 characters of such an answer are.

    json's own `strict` switch is about control characters inside strings.
    Reading someone else's text is precisely when it should be off.
    """

    def test_a_real_line_break_inside_a_string_is_read(self):
        raw = '{"reply": "好", "draw": {"main": "少女站在城墙上\n侧身回头"}}'
        got = providers.base.parse_json_lenient(raw, "deepseek")
        self.assertEqual(got["draw"]["main"], "少女站在城墙上\n侧身回头")

    def test_it_still_digs_the_object_out_of_prose(self):
        raw = '好的，这是结果：\n```json\n{"reply": "好"}\n```'
        self.assertEqual(providers.base.parse_json_lenient(raw, "deepseek"),
                         {"reply": "好"})

    def test_both_at_once(self):
        raw = '这是结果：\n{"reply": "好\n第二行"}\n希望有帮助'
        self.assertEqual(providers.base.parse_json_lenient(raw, "deepseek")["reply"],
                         "好\n第二行")

    def test_something_that_is_not_an_object_is_still_a_failure(self):
        for raw in ("", "抱歉，我做不到", "[1, 2, 3]", '{"reply": "好'):
            with self.assertRaises(providers.LLMError):
                providers.base.parse_json_lenient(raw, "deepseek")


class APictureOnScreenIsAPictureSheCanSeeTests(TestCase):
    """
    A turn's pictures go into the history because the turn HAS pictures.

    They used to go in because the row was labelled `kind="image"` and had a
    status on a list — two labels that are supposed to agree with the contents
    and are both written under a `status="pending"` guard, which a stop wins.
    A turn stopped after painting therefore kept its picture on screen and lost
    it to the model: you stop a run, look at the picture you just made, ask for
    a change, and she cannot see the thing you are talking about.

    The line is drawn where the transcript draws it — a turn is in the history
    if the user can see it — so these cases are not a list of accidents to
    remember but one rule with four readings.
    """

    def setUp(self):
        self.conv = Conversation.objects.create(title="看得见的")
        Message.objects.create(conversation=self.conv, seq=1, role="user",
                               status="done", content="画一张")

    def _turn(self, *, picture: bool, **over) -> Message:
        m = Message.objects.create(conversation=self.conv, seq=2, role="assistant",
                                   content=over.pop("content", "给你"), **over)
        if picture:
            att = Attachment(message=m, kind="generated", mime="image/png",
                             width=64, height=96, position=0, seed=1, size_bytes=1)
            att.file.save("x.png", ContentFile(_png_bytes()), save=True)
        return m

    def _history(self):
        h = pipeline.build_history(self.conv, before_seq=99)
        pictures = sum(
            1
            for msg in h
            if isinstance(msg.get("content"), list)
            for part in msg["content"]
            if part.get("type") == "image_url"
        )
        return h, pictures

    def test_a_finished_turn(self):
        self._turn(picture=True, status="done", kind="image")
        _, pictures = self._history()
        self.assertEqual(pictures, 1)

    def test_a_turn_stopped_after_it_painted(self):
        # what the user is looking at when they press stop mid-run
        self._turn(picture=True, status="cancelled", kind="image")
        _, pictures = self._history()
        self.assertEqual(pictures, 1)

    def test_a_turn_whose_label_never_caught_up(self):
        # kind is written under a status="pending" guard; a stop can win that
        # race and leave the pictures with a row that still says "text"
        self._turn(picture=True, status="cancelled", kind="text")
        _, pictures = self._history()
        self.assertEqual(pictures, 1)

    def test_a_turn_that_failed_after_it_painted(self):
        # continuous.py says this in words already: a turn that produced
        # pictures is not a failure, however it ended
        self._turn(picture=True, status="error", kind="image")
        _, pictures = self._history()
        self.assertEqual(pictures, 1)

    def test_a_stop_before_anything_came_back_is_still_a_turn_that_spoke(self):
        # the transcript shows its words under a 「已停止」, so the model sees
        # them too
        self._turn(picture=False, status="cancelled", kind="text", content="我想想")
        h, pictures = self._history()
        self.assertEqual(pictures, 0)
        self.assertEqual([m["role"] for m in h], ["user", "assistant"])

    def test_a_failure_with_nothing_to_show_is_not_a_turn(self):
        # its words are behind a fold and are about the failure, not the
        # picture — the user does not read them as part of the conversation
        self._turn(picture=False, status="error", kind="text", content="半句话")
        h, pictures = self._history()
        self.assertEqual(pictures, 0)
        self.assertEqual([m["role"] for m in h], ["user"])

    def test_a_turn_still_being_made_is_not_history_yet(self):
        self._turn(picture=True, status="pending", kind="image")
        h, _ = self._history()
        self.assertEqual([m["role"] for m in h], ["user"])

    def test_a_branch_copies_exactly_what_the_model_is_given(self):
        """
        The branch and the history are one definition, and the comment in the
        branch has always said so. They are the same rows because they answer
        the same question.
        """
        stopped = self._turn(picture=True, status="cancelled", kind="text")
        client = Client()
        client.force_login(
            get_user_model().objects.create_user(username="brancher", password="x")
        )
        r = client.post(f"/api/messages/{stopped.id}/branch")
        self.assertEqual(r.status_code, 201)
        branch = Conversation.objects.get(id=r.json()["id"])
        self.assertEqual(branch.messages.count(), 2)
        copy = branch.messages.order_by("seq").last()
        assert copy is not None
        self.assertEqual(copy.attachments.filter(kind="generated").count(), 1)


class AFailureKeepsWhatWouldExplainItTests(TestCase):
    """
    An answer that could not be read is kept whole.

    The transcript can only afford a line of it, and a line is not enough: a
    JSON object fails at ONE character, and which character decides whether
    this was a stray line break, a full-width colon, or a sentence written
    before the object. Throwing the rest away leaves guessing as the only way
    to find out — which is exactly where this went the first time.
    """

    def setUp(self):
        self.client = Client()
        self.client.force_login(
            get_user_model().objects.create_user(username="yyy", password="pw")
        )
        self.conv = Conversation.objects.create(title="读不懂的回答")

    def test_the_error_carries_the_answer_it_could_not_read(self):
        with self.assertRaises(providers.LLMError) as caught:
            providers.base.parse_json_lenient("抱歉，我写不出来", "deepseek")
        self.assertEqual(caught.exception.raw, "抱歉，我写不出来")

    @override_settings(**TEST_CREDS)
    def test_it_lands_on_the_row_and_comes_back_from_检查(self):
        answer = '{"reply": "好", "draw": {"main": "断在这里'
        with _sync_jobs(), mock.patch.object(
            pipeline.llm,
            "lead_turn",
            side_effect=providers.LLMError("deepseek returned unparseable JSON: …",
                                           raw=answer),
        ):
            self.client.post(
                f"/api/conversations/{self.conv.id}/messages", {"text": "画一张"}
            )
        row = self.conv.messages.filter(role="assistant").order_by("-seq").first()
        assert row is not None
        self.assertEqual(row.status, "error")
        self.assertEqual((row.model_raw or {}).get("unparsed"), answer)

        seen = self.client.get(f"/api/messages/{row.id}/raw").json()
        self.assertEqual(seen["unparsed"], answer)

    @override_settings(**TEST_CREDS)
    def test_a_turn_that_failed_some_other_way_keeps_nothing(self):
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", side_effect=RuntimeError("网络断了")
        ):
            self.client.post(
                f"/api/conversations/{self.conv.id}/messages", {"text": "画一张"}
            )
        row = self.conv.messages.filter(role="assistant").order_by("-seq").first()
        assert row is not None
        self.assertEqual(row.status, "error")
        self.assertNotIn("unparsed", row.model_raw or {})


class AWallIsNotABadMinuteTests(TestCase):
    """
    Some failures come back the same way however many times you ask.

    Every retry budget in this app — the free goes inside a turn, the run's own
    rounds, the next round of a continuous run — exists for a vendor having a
    bad minute. An account that is out of Anlas answers every request
    identically, so those budgets spend themselves against it and change
    nothing. 102 of those against one wall in an afternoon is the thing this
    stops.
    """

    def test_what_counts_as_a_wall(self):
        for status in (401, 402, 403):
            self.assertTrue(pipeline.dead_end(novelai.NovelAIError("no", status=status)))
        for status in (429, 500, 502, 503, 400, 404):
            self.assertFalse(pipeline.dead_end(novelai.NovelAIError("no", status=status)))
        # nothing came back at all, which is exactly what a retry is for
        self.assertFalse(pipeline.dead_end(novelai.NovelAIError("timed out")))
        self.assertFalse(pipeline.dead_end(ValueError("something else")))

    def test_a_wall_is_asked_once(self):
        tries = []

        def attempt():
            tries.append(1)
            raise novelai.NovelAIError("novelai HTTP 402: out of Anlas", status=402)

        with self.assertRaises(novelai.NovelAIError):
            pipeline._with_retries("painting", uuid.uuid4(), attempt)
        self.assertEqual(len(tries), 1)

    def test_a_bad_minute_still_gets_its_free_goes(self):
        """The budget is not being removed, only pointed away from walls."""
        tries = []

        def attempt():
            tries.append(1)
            raise novelai.NovelAIError("novelai HTTP 429: slow down", status=429)

        with mock.patch.object(pipeline, "_check_cancel"), mock.patch.object(
            pipeline, "_set_phase"
        ):
            with self.assertRaises(novelai.NovelAIError):
                pipeline._with_retries("painting", uuid.uuid4(), attempt)
        self.assertEqual(len(tries), settings.FREE_RETRIES_IN_RUN + 1)

    def test_a_wall_ends_the_run_rather_than_being_hit_once_per_round(self):
        """
        The run is what turns one refusal into a hundred. Closing it is the
        whole point — a turn failing is one failure, a run failing is one
        failure per tick until somebody notices.
        """
        conv = Conversation.objects.create(
            title="连续生成", continuous_run={
                "left": 9, "of": 10, "text": "继续", "free": 2, "done": 0,
                "failed": 0, "n_images": 1, "reflection_rounds": 0,
                "sticky_refs": False,
            }
        )
        msg = Message.objects.create(
            conversation=conv, seq=1, role="assistant", status="pending", content=""
        )
        with mock.patch.object(
            pipeline, "build_history",
            side_effect=novelai.NovelAIError("novelai HTTP 402: out of Anlas", status=402),
        ):
            pipeline.run_generation(msg.id)

        conv.refresh_from_db()
        msg.refresh_from_db()
        self.assertEqual(msg.status, "error")
        self.assertIsNone(conv.continuous_run, "the run should be over")

    def test_an_ordinary_failure_leaves_the_run_alone(self):
        """A bad minute must not end a run of ten. That is the other half."""
        conv = Conversation.objects.create(
            title="连续生成", continuous_run={
                "left": 9, "of": 10, "text": "继续", "free": 2, "done": 0,
                "failed": 0, "n_images": 1, "reflection_rounds": 0,
                "sticky_refs": False,
            }
        )
        msg = Message.objects.create(
            conversation=conv, seq=1, role="assistant", status="pending", content=""
        )
        with mock.patch.object(
            pipeline, "build_history",
            side_effect=novelai.NovelAIError("novelai timed out"),
        ), mock.patch.object(pipeline.continuous, "reconcile"):
            pipeline.run_generation(msg.id)

        conv.refresh_from_db()
        self.assertIsNotNone(conv.continuous_run, "one bad minute is not the end")

    def test_reflection_does_not_spend_its_rounds_on_a_wall(self):
        """
        Reflection has its own budget, and it used to feed it to the same wall
        one round at a time.
        """
        from core.services import reflection

        tries = []

        def redraw(_brief):
            tries.append(1)
            raise novelai.NovelAIError("novelai HTTP 402: out of Anlas", status=402)

        hooks = reflection.Hooks(
            redraw=redraw,
            check_cancel=lambda: None,
            set_phase=lambda _p: None,
            image_url=lambda _a: None,
            hopeless=pipeline.dead_end,
        )
        with self.assertRaises(novelai.NovelAIError):
            reflection._redraw_with_retries({}, reflection.Outcome(
                brief={}, nai_prompts={}, images=[]
            ), 1, 4, 4, hooks)
        self.assertEqual(len(tries), 1)


@override_settings(
    DEEPSEEK_API_KEY="k", OPENROUTER_API_KEY="or", OPENROUTER_MODEL="vendor/same-model"
)
class OneModelSeveralRoadsTests(TestCase):
    """
    A model can be reached more than one way, and nothing above the provider
    is allowed to find out which way it was reached.
    """

    def setUp(self):
        from core.services.providers import base, deepseek

        deepseek._known = base.Roads(blocked_for=600.0)
        self.provider = deepseek.DeepSeekProvider()

    @staticmethod
    def _ok(content='{"reply":"\u597d"}'):
        return {
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {},
        }

    def _complete(self, post):
        from core.services.providers import deepseek

        with mock.patch.object(deepseek, "post_with_retry", side_effect=post):
            return self.provider.complete(
                system="SYS",
                messages=[{"role": "user", "content": "\u753b\u4e00\u5f20"}],
                schema=llm.LEAD_SCHEMA,
                timeout=30,
            )

    @staticmethod
    def _out(*names, status=None):
        """A poster that cannot get through on `names` and answers elsewhere."""
        seen = []

        def post(url, headers, payload, timeout, who, **kw):
            seen.append((who, kw.get("retries")))
            if who in names:
                raise providers.LLMError(f"{who} is out", status=status)
            return OneModelSeveralRoadsTests._ok()

        return post, seen

    def test_a_road_that_is_out_is_stepped_over_not_waited_on(self):
        post, seen = self._out("deepseek")
        out = self._complete(post)
        self.assertEqual(out.parsed["reply"], "\u597d")
        self.assertEqual([w for w, _ in seen], ["deepseek", "openrouter"])

    def test_a_road_with_somewhere_to_fall_back_to_is_not_retried_on_itself(self):
        """
        Retrying a road that just timed out means waiting out the same timeout
        for the same reason while the next road sits there answering. This is
        the difference between the first user waiting one timeout and waiting
        three of them plus the sleeps between.
        """
        post, seen = self._out("deepseek")
        self._complete(post)
        self.assertEqual(dict((w, r) for w, r in seen), {"deepseek": 1, "openrouter": 3})

    def test_with_one_road_the_retry_ladder_is_exactly_what_it_always_was(self):
        with override_settings(OPENROUTER_API_KEY=""):
            post, seen = self._out()
            self._complete(post)
        self.assertEqual(seen, [("deepseek", 3)])

    def test_the_next_call_does_not_pay_to_learn_the_same_thing_again(self):
        post, _ = self._out("deepseek")
        self._complete(post)
        post2, seen = self._out()
        self._complete(post2)
        self.assertEqual([w for w, _ in seen], ["openrouter"])

    def test_the_cheaper_road_is_used_again_the_moment_it_answers(self):
        """A fallback that becomes permanent is a bug, not a fallback."""
        from core.services.providers import base, deepseek

        post, _ = self._out("deepseek")
        self._complete(post)
        self.assertTrue(deepseek._known.blocked(self.provider._roads()[0]))

        class Answered:
            status_code = 200

        with mock.patch.object(base.httpx, "Client") as client:
            client.return_value.__enter__.return_value.post.return_value = Answered()
            self.assertEqual(self.provider.recheck_roads(), ["deepseek"])

        post2, seen = self._out()
        self._complete(post2)
        self.assertEqual([w for w, _ in seen], ["deepseek"])

    def test_the_probe_knocks_on_the_door_the_caller_will_use(self):
        """
        Measured 2026-09-15, while DeepSeek was down: GET /user/balance
        answered 200 in 0.1s and GET /models did too, while POST
        /chat/completions timed out at 45s. One hostname, more than one
        service. A probe aimed anywhere but the endpoint that does the work
        can call a road open while the only part anyone needs is shut.
        """
        from core.services.providers import base, deepseek

        road = self.provider._roads()[0]
        deepseek._known.block(road)

        class Answered:
            status_code = 200

        with mock.patch.object(base.httpx, "Client") as client:
            post = client.return_value.__enter__.return_value.post
            post.return_value = Answered()
            self.provider.recheck_roads()
            url = post.call_args.args[0]
            body = post.call_args.kwargs["json"]
            headers = post.call_args.kwargs["headers"]

        self.assertEqual(url, road.url)
        self.assertEqual(headers, road.headers)
        self.assertEqual(body["messages"][0]["content"], "hi")

    def test_a_road_that_stays_shut_stays_stepped_over(self):
        from core.services.providers import base, deepseek

        road = self.provider._roads()[0]
        deepseek._known.block(road)
        with mock.patch.object(base.httpx, "Client", side_effect=OSError("still down")):
            self.assertEqual(self.provider.recheck_roads(), [])
        self.assertTrue(deepseek._known.blocked(road))

    def test_an_answer_that_cannot_be_used_does_not_send_us_somewhere_else(self):
        """
        Unusable output is the model's, and it is the same model down either
        road. Changing roads over it would spend the fallback on something the
        fallback cannot fix, and hide a bad prompt behind a vendor switch.
        """
        seen = []

        def post(url, headers, payload, timeout, who, **kw):
            seen.append(who)
            return self._ok(content="\u8fd9\u4e0d\u662f JSON")

        with self.assertRaises(providers.LLMError):
            self._complete(post)
        self.assertEqual(set(seen), {"deepseek"})

    def test_a_request_the_endpoint_read_and_rejected_does_not_move_us(self):
        """
        400 is about the body, and the identical body is wrong everywhere. Going
        on would buy a second 400 and leave a working road marked broken.
        """
        from core.services.providers import deepseek

        post, seen = self._out("deepseek", status=400)
        with self.assertRaises(providers.LLMError):
            self._complete(post)
        self.assertEqual([w for w, _ in seen], ["deepseek"])
        self.assertFalse(deepseek._known.blocked(self.provider._roads()[0]))

    def test_a_refusal_aimed_at_this_account_does_move_us(self):
        """
        401/402/403/429 are about who is asking, not what was asked, and the
        other road bills a different account and holds a different limit.
        """
        for status in (401, 402, 403, 429, 503):
            with self.subTest(status=status):
                self.setUp()
                post, seen = self._out("deepseek", status=status)
                self._complete(post)
                self.assertEqual([w for w, _ in seen], ["deepseek", "openrouter"])

    def test_reporting_a_road_out_does_what_a_timeout_would_have_done(self):
        """
        The whole design. There is one authority on which roads work — what the
        backend last found out — and a person clicking writes to exactly that,
        setting the state a request timing out would have set. Nothing
        downstream can tell the two apart, which is why nothing downstream has
        to know a person was involved.

        What it buys is who noticed: finding this out the automatic way costs a
        full request timeout, and whoever is watching 「思考中」 knows first.
        """
        from core.services.providers import deepseek

        official = self.provider._roads()[0]
        self.assertTrue(self.provider.step_over("deepseek", over=True))
        self.assertTrue(deepseek._known.blocked(official))

        post, seen = self._out()
        self._complete(post)
        self.assertEqual([w for w, _ in seen], ["openrouter"])

    def test_the_backend_still_takes_the_cheaper_road_back(self):
        """
        A report is not a preference. It is deliberately not stored, so the
        probe puts the official road back the moment it answers — whatever
        anyone clicked, and without anyone having to click again.

        A stored "use the fallback" would have been a second authority on the
        same fact, and the two would disagree exactly when it mattered: the
        probe saying the official road is back, the preference saying keep
        paying for the other one.
        """
        from core.services.providers import base, deepseek

        self.provider.step_over("deepseek", over=True)

        class Answered:
            status_code = 200

        with mock.patch.object(base.httpx, "Client") as client:
            client.return_value.__enter__.return_value.post.return_value = Answered()
            self.assertEqual(self.provider.recheck_roads(), ["deepseek"])

        post, seen = self._out()
        self._complete(post)
        self.assertEqual([w for w, _ in seen], ["deepseek"])
        self.assertFalse(deepseek._known.blocked(self.provider._roads()[0]))

    def test_saying_a_road_is_back_lets_the_next_turn_find_out(self):
        """
        Undo, and the same kind of claim in the other direction: it does not
        assert the road works, it just stops stepping over it. If the person
        was wrong the next turn learns so in the ordinary way.
        """
        from core.services.providers import deepseek

        post, _ = self._out("deepseek")
        self._complete(post)
        official = self.provider._roads()[0]
        self.assertTrue(deepseek._known.blocked(official))

        self.assertTrue(self.provider.step_over("deepseek", over=False))
        self.assertFalse(deepseek._known.blocked(official))

    def test_a_road_nobody_has(self):
        self.assertFalse(self.provider.step_over("nonesuch", over=True))

    def test_the_page_is_told_what_the_backend_found(self):
        post, _ = self._out("deepseek")
        self._complete(post)
        status = {r["name"]: r for r in self.provider.roads_status()}
        self.assertTrue(status["deepseek"]["stepped_over"])
        self.assertFalse(status["openrouter"]["stepped_over"])
        self.assertTrue(status["openrouter"]["in_use"])
        self.assertFalse(status["deepseek"]["in_use"])
        self.assertGreater(status["deepseek"]["seconds_left"], 0)

    def test_the_report_is_not_written_down_anywhere(self):
        """
        The test for the thing that is absent. A stored copy of "use the
        fallback" is the obvious next feature and the bug: it would be a second
        authority on which road works, and it would disagree with the probe at
        exactly the moment the probe is right.

        So: report a road out, then throw the process's memory away. Nothing
        should have survived, because the only record was the one the backend
        keeps of what it last found out.
        """
        from core.services.providers import base, deepseek

        self.provider.step_over("deepseek", over=True)
        self.assertTrue(deepseek._known.blocked(self.provider._roads()[0]))

        deepseek._known = base.Roads(blocked_for=600.0)   # as if restarted
        self.assertFalse(deepseek._known.blocked(self.provider._roads()[0]))

        post, seen = self._out()
        self._complete(post)
        self.assertEqual([w for w, _ in seen], ["deepseek"])

    def test_a_provider_with_one_way_in_offers_no_choice(self):
        from core.services.providers import xai

        self.assertEqual(xai.GrokProvider().roads_status(), [])

    def test_something_is_actually_watching_for_the_preferred_road(self):
        """
        The whole arrangement rests on a thread nobody looks at. Everything
        else here tests what happens when it runs; this tests that it is
        started at all, because a probe that never fires turns "stepped over
        for now" into "on the fallback until someone restarts the backend".
        """
        started = []
        with mock.patch.object(jobs.threading, "Thread") as thread:
            thread.side_effect = lambda **kw: started.append(kw) or mock.MagicMock()
            jobs._sweeper_started = False
            try:
                jobs.start_sweeper()
            finally:
                jobs._sweeper_started = False
        self.assertIn("road-probe", [k.get("name") for k in started])

    def test_the_watcher_asks_the_provider_and_survives_a_bad_answer(self):
        """A probe that raises must not take the thread down with it."""
        with mock.patch.object(jobs.providers, "active") as active:
            active.return_value.recheck_roads.side_effect = OSError("still down")
            with mock.patch.object(jobs.time, "sleep", side_effect=[None, SystemExit]):
                with self.assertRaises(SystemExit):
                    jobs._probe_roads()
            self.assertEqual(active.return_value.recheck_roads.call_count, 1)

    def test_with_no_second_key_there_is_simply_one_road(self):
        with override_settings(OPENROUTER_API_KEY=""):
            self.assertEqual([r.name for r in self.provider._roads()], ["deepseek"])

    def test_every_road_is_asked_the_same_question(self):
        """
        Only the endpoint's name for the model, and its own routing, may
        differ. If anything else did, the answer would depend on the road and
        the layer above would have to know which one it got.
        """
        bodies = {}

        def post(url, headers, payload, timeout, who, **kw):
            bodies[who] = payload
            if who == "deepseek":
                raise providers.LLMError("out")
            return self._ok()

        self._complete(post)
        official, other = bodies["deepseek"], bodies["openrouter"]
        self.assertEqual(official["model"], "deepseek-flash")
        self.assertEqual(other["model"], "vendor/same-model")
        self.assertEqual(other.get("provider"), {"require_parameters": True})
        for f in ("messages", "response_format", "max_tokens", "reasoning_effort"):
            self.assertEqual(official.get(f), other.get(f), f)

    def test_when_every_road_is_blocked_it_still_asks_rather_than_refuse(self):
        from core.services.providers import deepseek

        for road in self.provider._roads():
            deepseek._known.block(road)
        post, seen = self._out()
        self._complete(post)
        self.assertEqual([w for w, _ in seen], ["deepseek"])

    def test_the_account_balance_is_read_off_the_official_road_only(self):
        """It is that account's balance. OpenRouter cannot answer for it."""
        import inspect

        from core.services.providers import deepseek

        src = inspect.getsource(deepseek.DeepSeekProvider.balance)
        self.assertIn("API_BASE", src)
        self.assertNotIn("OPENROUTER", src)

    def test_what_comes_back_is_the_same_whichever_road_it_came_down(self):
        """
        The invariant the whole design rests on. Not "the app keeps working" —
        every field a caller can read has to be identical, because the caller
        has no way to ask which road it got and must never need to.

        Measured 2026-09-15: the two endpoints do NOT agree on the wire.
        DeepSeek's own API returns the chain of thought as reasoning_content
        and leaves reasoning null; the OpenAI-compatible shape OpenRouter
        serves does the exact opposite. Reading only one name meant 「检查」
        came back empty for every hour the app spent on the fallback — the app
        still working, and quietly worse.
        """
        def answer(field):
            return {
                "choices": [{
                    "message": {"content": '{"reply":"\u597d"}', field: "\u60f3\u4e86\u60f3"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3, "cost": 0.001},
            }

        def official_only(url, headers, payload, timeout, who, **kw):
            if who == "deepseek":
                return answer("reasoning_content")
            raise providers.LLMError("out")

        def fallback_only(url, headers, payload, timeout, who, **kw):
            if who == "deepseek":
                raise providers.LLMError("out")
            return answer("reasoning")

        down_the_first = self._complete(official_only)
        self.setUp()
        down_the_second = self._complete(fallback_only)

        self.assertEqual(down_the_first.parsed, down_the_second.parsed)
        self.assertEqual(down_the_first.usage, down_the_second.usage)
        self.assertEqual(down_the_first.cost_ticks, down_the_second.cost_ticks)
        self.assertEqual(
            down_the_first.reasoning,
            down_the_second.reasoning,
            "\u300c\u68c0\u67e5\u300d has to show the same thing either way",
        )
        self.assertTrue(down_the_second.reasoning)

    def test_the_extra_things_one_road_says_do_not_leak_out(self):
        """
        OpenRouter's usage block carries cost, is_byok, cost_details. DeepSeek's
        carries none of them. The provider narrows usage to the two fields it
        promises, so a road cannot widen what the app sees.
        """
        def post(url, headers, payload, timeout, who, **kw):
            if who == "deepseek":
                raise providers.LLMError("out")
            return {
                "choices": [{
                    "message": {"content": '{"reply":"\u597d"}'},
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": 7, "completion_tokens": 3,
                    "cost": 0.001, "is_byok": False, "cost_details": {"x": 1},
                },
            }

        out = self._complete(post)
        self.assertEqual(set(out.usage), {"prompt_tokens", "completion_tokens"})

    def test_a_provider_with_one_way_in_has_nothing_to_recheck(self):
        from core.services.providers import xai

        self.assertEqual(xai.GrokProvider().recheck_roads(), [])


@override_settings(OPENROUTER_API_KEY="")
class ARetryIsTheSameRequestTests(TestCase):
    """
    A retry runs the same request again. Nothing is added to it, nothing about
    it is turned down.

    Models sample, so asking again is a fresh draw rather than a repeat — that
    is the whole reason a retry is worth making. An attempt that was told
    something the first one was not, or allowed to think less than the first
    one was, is a different request; whether it succeeds says nothing about
    whether the original can.
    """

    def _run(self, replies, **kw):
        from core.services.providers import deepseek

        sent = []

        def fake_post(url, headers, payload, timeout, who, **kw):
            # a copy: the same payload sent twice is one object seen twice
            sent.append(json.loads(json.dumps(payload)))
            return replies[min(len(sent) - 1, len(replies) - 1)]

        with mock.patch.object(deepseek, "post_with_retry", side_effect=fake_post):
            out = deepseek.DeepSeekProvider().complete(
                system="SYS",
                messages=[{"role": "user", "content": "画一张"}],
                schema=llm.LEAD_SCHEMA,
                timeout=30,
                **kw,
            )
        return out, sent

    @staticmethod
    def _reply(content, finish="stop", reasoning=""):
        return {
            "choices": [
                {
                    "message": {"content": content, "reasoning_content": reasoning},
                    "finish_reason": finish,
                }
            ],
            "usage": {},
        }

    def test_the_second_attempt_asks_exactly_what_the_first_did(self):
        out, sent = self._run(
            [self._reply("抱歉，我不能"), self._reply('{"reply":"好"}')],
            effort="max",
            temperature=0.3,
        )
        self.assertEqual(out.parsed["reply"], "好")
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0], sent[1])

    def test_an_answer_that_ran_out_of_room_is_asked_again_unchanged(self):
        _, sent = self._run(
            [self._reply('{"reply": "好', finish="length"), self._reply('{"reply":"好"}')],
            effort="max",
        )
        self.assertEqual(sent[0], sent[1])
        self.assertEqual(sent[1]["reasoning_effort"], "max")

    def test_running_out_of_room_says_which_lever_to_pull(self):
        """
        And says it to the log, not to the user.

        Which setting to raise is a fact about this deployment, so it still has
        to be written down somewhere — but the bubble prints the error's first
        line, and someone who asked for a picture should not be handed two
        environment variable names.
        """
        from core.services.providers import deepseek

        with mock.patch.object(
            deepseek, "post_with_retry",
            side_effect=lambda *a, **k: self._reply('{"reply": "好', finish="length"),
        ):
            with self.assertLogs("core.services.providers.deepseek", "WARNING") as logged:
                with self.assertRaises(providers.LLMError) as caught:
                    deepseek.DeepSeekProvider().complete(
                        system="SYS", messages=[], schema=llm.LEAD_SCHEMA, timeout=30,
                    )
        logs = chr(10).join(logged.output)
        self.assertIn("DEEPSEEK_REASONING_EFFORT", logs)
        self.assertIn("DEEPSEEK_MAX_TOKENS", logs)
        said = str(caught.exception)
        self.assertNotIn("DEEPSEEK_", said)
        self.assertIn("截断", said)


class ShapeIsAlwaysSaidOneWayOrTheOtherTests(TestCase):
    """
    Every request either has its schema enforced by the vendor or spells it
    out in words. They are the same decision, and the one place it is made is
    providers.base.system_for.

    The gap it closes: xAI asks for enforcement and falls back to plain JSON
    mode when a deployment refuses it. That fallback used to rebuild the
    request without adding the words, leaving the model to guess the shape
    from whatever the prompt file happened to say.
    """

    def test_enforced_requests_carry_no_prose(self):
        got = providers.base.system_for("SYS", llm.LEAD_SCHEMA, enforced=True)
        self.assertEqual(got, "SYS")

    def test_unenforced_requests_carry_the_shape(self):
        got = providers.base.system_for("SYS", llm.LEAD_SCHEMA, enforced=False)
        self.assertTrue(got.startswith("SYS"))
        self.assertIn("reply", got)

    def test_the_fallback_to_plain_json_mode_takes_the_words_with_it(self):
        from core.services.providers import xai

        sent = []

        def fake_post(url, headers, payload, timeout, who, **kw):
            sent.append(payload)
            if len(sent) == 1:
                raise providers.LLMError("xai HTTP 400: response_format not supported")
            return {
                "choices": [{"message": {"content": '{"reply":"好","draw":null,"edits":null}'}}],
                "usage": {},
            }

        with mock.patch.object(xai, "post_with_retry", side_effect=fake_post):
            out = xai.GrokProvider().complete(
                system="SYS",
                messages=[{"role": "user", "content": "画一张"}],
                schema=llm.LEAD_SCHEMA,
                timeout=30,
            )
        self.assertEqual(out.parsed["reply"], "好")
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]["response_format"]["type"], "json_schema")
        self.assertEqual(sent[1]["response_format"]["type"], "json_object")
        first_system = sent[0]["messages"][0]["content"]
        second_system = sent[1]["messages"][0]["content"]
        self.assertEqual(first_system, "SYS")
        self.assertIn("reply", second_system)
        self.assertIn("json", second_system.lower())

    def test_the_fallback_keeps_the_rest_of_the_request(self):
        from core.services.providers import xai

        sent = []

        def fake_post(url, headers, payload, timeout, who, **kw):
            sent.append(payload)
            if len(sent) == 1:
                raise providers.LLMError("xai HTTP 400: json_schema unsupported")
            return {"choices": [{"message": {"content": "{}"}}], "usage": {}}

        with mock.patch.object(xai, "post_with_retry", side_effect=fake_post):
            xai.GrokProvider().complete(
                system="SYS",
                messages=[{"role": "user", "content": "x"}],
                schema=llm.DRAW_SCHEMA,
                timeout=30,
                effort="low",
                temperature=0.2,
            )
        self.assertEqual(sent[1]["reasoning_effort"], "low")
        self.assertEqual(sent[1]["temperature"], 0.2)
        self.assertEqual(sent[1]["messages"][1:], sent[0]["messages"][1:])

    def test_json_wrapped_in_chatter_is_still_recovered(self):
        """The whole reason the lenient parser exists."""
        got = providers.base.parse_json_lenient(
            'Sure! Here you go:\n```json\n{"a": 1}\n```', "deepseek"
        )
        self.assertEqual(got, {"a": 1})


@override_settings(**TEST_CREDS)
class ImageCountIsTheAppsTests(TestCase):
    """
    How many images to paint belongs to the caller — 出图数量 on the web, the
    n_images argument over MCP — and to neither model. Both schemas dropped
    the field, so a model that tried to ask for four could not say so.
    """

    def setUp(self):
        self.c = Client()
        self.c.post(
            "/api/auth/login",
            json.dumps({"username": "tester", "password": "test-password-123"}),
            content_type="application/json",
        )

    def _draw(self, n_images, lead=None, draw=None):
        cid = self.c.post("/api/conversations", "{}", content_type="application/json").json()["id"]
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=lead or FAKE_IMAGE_RESULT
        ), mock.patch.object(
            pipeline.llm, "draw_turn", return_value=draw or FAKE_DRAW_RESULT
        ), mock.patch.object(
            pipeline.novelai, "generate_one", return_value=(_png_bytes(), 7)
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            self.c.post(
                f"/api/conversations/{cid}/messages", {"text": "画", "n_images": str(n_images)}
            )
        return Message.objects.get(conversation_id=cid, role="assistant")

    def test_neither_schema_asks_for_a_count(self):
        lead = llm.LEAD_SCHEMA["schema"]["properties"]["draw"]["anyOf"][1]
        self.assertNotIn("n_images", lead["properties"])
        self.assertNotIn("n_images", lead["required"])
        self.assertNotIn("n_images", llm.DRAW_SCHEMA["schema"]["properties"])
        self.assertNotIn("n_images", llm.DRAW_SCHEMA["schema"]["required"])

    def test_the_apps_count_is_what_gets_painted(self):
        msg = self._draw(3)
        self.assertEqual(len(msg.attachments.all()), 3)
        assert msg.nai_prompts is not None
        self.assertEqual(msg.nai_prompts["n_images"], 3)

    def test_a_model_that_asks_for_more_anyway_is_ignored(self):
        """Nothing stops a model from putting the key back; the app still wins."""
        msg = self._draw(1, draw={**FAKE_DRAW_RESULT, "n_images": 4})
        self.assertEqual(len(msg.attachments.all()), 1)

    def test_the_picture_description_carries_its_subjects(self):
        """Who is in the picture is a list of subjects, each with its own fields."""
        draw = llm.LEAD_SCHEMA["schema"]["properties"]["draw"]["anyOf"][1]
        self.assertIn("subjects", draw["properties"])
        self.assertIn("subjects", draw["required"])
        subject = draw["properties"]["subjects"]["items"]
        for field in ("who", "outfit", "look", "changes", "doing", "where"):
            self.assertIn(field, subject["properties"])

    def test_the_drawing_model_reads_subjects_before_the_picture(self):
        text = llm.format_brief({
            "subjects": [{"who": "林", "look": "银灰长发", "doing": "回头"}],
            "composition": "船头", "size": "portrait",
        })
        self.assertIn("「林」", text)
        self.assertIn("外观：银灰长发", text)
        self.assertIn("在做什么：回头", text)
        # the subjects first, then the picture around them
        self.assertLess(text.index("「林」"), text.index("船头"))

    def test_an_empty_field_is_left_out_rather_than_sent_blank(self):
        text = llm.format_brief({
            "subjects": [{"who": "林", "look": "", "doing": "回头"}],
            "composition": "走廊", "size": "portrait",
        })
        self.assertNotIn("外观", text)

    def test_the_brief_the_drawing_model_reads_says_nothing_about_count(self):
        text = llm.format_brief({"composition": "灯塔", "size": "landscape"})
        self.assertIn("画幅：landscape", text)
        self.assertNotIn("张数", text)

    def test_a_rerun_paints_the_same_number_as_a_fresh_send(self):
        """
        The regression this pins: a rerun row carried no count, so _paint fell
        back to the one inside nai_prompts — which for a fresh turn is 1. Two
        selected, one painted, silently.
        """
        cid = self.c.post("/api/conversations", "{}", content_type="application/json").json()["id"]
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=FAKE_IMAGE_RESULT
        ), mock.patch.object(
            pipeline.llm, "draw_turn", return_value=FAKE_DRAW_RESULT
        ), mock.patch.object(
            pipeline.novelai, "generate_one", return_value=(_png_bytes(), 7)
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            self.c.post(f"/api/conversations/{cid}/messages", {"text": "画", "n_images": "2"})
            first = Message.objects.get(conversation_id=cid, role="assistant", archived=False)
            self.assertEqual(len(first.attachments.all()), 2)

            self.c.post(
                f"/api/conversations/{cid}/rerun",
                json.dumps({"n_images": 2}),
                content_type="application/json",
            )
        again = Message.objects.get(conversation_id=cid, role="assistant", archived=False)
        self.assertEqual(len(again.attachments.all()), 2)
        self.assertEqual(again.force_n_images, 2)

    def test_a_rerun_without_a_count_uses_the_conversation_s(self):
        """
        An older client that sends nothing must not silently drop to one. It
        used to read the count off the turn it was replacing; the settings now
        belong to the conversation, so that is where it reads.
        """
        conv = Conversation.objects.create(title="c", gen_settings={"n_images": 3})
        Message.objects.create(
            conversation=conv, seq=1, role="user", status="done", content="画"
        )
        Message.objects.create(
            conversation=conv, seq=2, role="assistant", status="done", content="好",
            kind="image", force_n_images=1,
        )
        with mock.patch.object(jobs, "start_generation"):
            self.c.post(f"/api/conversations/{conv.id}/rerun")
        fresh = conv.messages.filter(archived=False, status="pending").first()
        assert fresh is not None
        self.assertEqual(fresh.force_n_images, 3)

    def test_a_redraw_says_its_count_on_the_row(self):
        conv = Conversation.objects.create(title="c")
        Message.objects.create(
            conversation=conv, seq=1, role="user", status="done", content="画"
        )
        src = Message.objects.create(
            conversation=conv, seq=2, role="assistant", status="done", content="好",
            kind="image", nai_prompts={**FAKE_DRAW_RESULT, "n_images": 4},
        )
        with mock.patch.object(jobs, "start_redraw"):
            self.c.post(f"/api/messages/{src.id}/redraw")
        fresh = conv.messages.filter(status="pending").first()
        assert fresh is not None
        self.assertEqual(fresh.force_n_images, 4)

    def test_mcp_names_its_own_count(self):
        """MCP has no 出图数量 control, so it passes one; the model still has none."""
        conv = Conversation.objects.create(title="MCP", source="mcp")
        Message.objects.create(
            conversation=conv, seq=1, role="user", status="done", content="画"
        )
        msg = Message.objects.create(
            conversation=conv, seq=2, role="assistant", status="pending",
            force_n_images=2, heartbeat_at=timezone.now(),
        )
        with mock.patch.object(
            pipeline.llm, "lead_turn", return_value=FAKE_IMAGE_RESULT
        ), mock.patch.object(
            pipeline.llm, "draw_turn", return_value=FAKE_DRAW_RESULT
        ), mock.patch.object(
            pipeline.novelai, "generate_one", return_value=(_png_bytes(), 7)
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            pipeline.run_generation(msg.id)
        self.assertEqual(len(Message.objects.get(id=msg.id).attachments.all()), 2)


@override_settings(**TEST_CREDS)
class InspectRawTests(TestCase):
    """
    The inspector reads what the two models actually said. It is a separate
    endpoint because model_raw is far too big to ride along on every poll.
    """

    def setUp(self):
        self.c = Client()
        self.c.post(
            "/api/auth/login",
            json.dumps({"username": "tester", "password": "test-password-123"}),
            content_type="application/json",
        )
        self.conv = Conversation.objects.create(title="看看它说了什么")
        Message.objects.create(
            conversation=self.conv, seq=1, role="user", status="done", content="画点什么"
        )
        self.msg = Message.objects.create(
            conversation=self.conv, seq=2, role="assistant", status="done",
            content="好", kind="image", prompt_tokens=1234,
            model_raw={
                "reply": "好",
                "draw": {"main": "灯塔", "size": "portrait", "n_images": 1},
                "edits": [{"kind": "setting", "title": "统一画风"}],
                "usage": {"prompt_tokens": 1234, "completion_tokens": 56},
                # a field the endpoint does not name must not leak through
                "internal_debug": "should not appear",
            },
            nai_prompts={"base_prompt": "lighthouse", "size": "portrait", "n_images": 1},
        )

    def test_it_returns_both_models_sides_of_the_turn(self):
        r = self.c.get(f"/api/messages/{self.msg.id}/raw")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["lead"]["reply"], "好")
        self.assertEqual(data["lead"]["draw"]["main"], "灯塔")
        self.assertEqual(data["lead"]["edits"][0]["title"], "统一画风")
        self.assertEqual(data["lead"]["usage"]["completion_tokens"], 56)
        self.assertEqual(data["drawing"]["base_prompt"], "lighthouse")
        self.assertEqual(data["prompt_tokens"], 1234)
        self.assertNotIn("internal_debug", json.dumps(data))

    def test_an_old_turns_description_is_still_readable(self):
        """Older turns keep the text a separate vision model wrote for them."""
        prompt_msg = self.conv.messages.get(seq=1)
        Attachment.objects.create(
            message=prompt_msg, kind="upload", mime="image/png", size_bytes=10,
            width=832, height=1216, description="一座灯塔，夜里，硬光从左上打来。",
        )
        Attachment.objects.create(
            message=prompt_msg, kind="upload", mime="image/png", size_bytes=10,
            width=64, height=64, description="",   # vision was off or refused
        )
        refs = self.c.get(f"/api/messages/{self.msg.id}/raw").json()["references"]
        self.assertEqual([r["index"] for r in refs], [1, 2])
        self.assertIn("灯塔", refs[0]["description"])
        self.assertEqual(refs[0]["size"], [832, 1216])
        self.assertIn("/api/media/", refs[0]["url"])
        self.assertIsNone(refs[1]["description"])

    def test_generated_images_are_not_mistaken_for_references(self):
        Attachment.objects.create(
            message=self.msg, kind="generated", mime="image/png", size_bytes=10,
        )
        self.assertEqual(self.c.get(f"/api/messages/{self.msg.id}/raw").json()["references"], [])

    def test_a_turn_that_never_reached_the_model_reads_empty(self):
        bare = Message.objects.create(
            conversation=self.conv, seq=3, role="assistant", status="done", content="嗯"
        )
        data = self.c.get(f"/api/messages/{bare.id}/raw").json()
        self.assertIsNone(data["lead"]["reply"])
        self.assertIsNone(data["drawing"])

    def test_only_assistant_messages_have_one(self):
        user_msg = self.conv.messages.get(seq=1)
        self.assertEqual(self.c.get(f"/api/messages/{user_msg.id}/raw").status_code, 404)

    def test_it_needs_a_session(self):
        anon = Client()
        r = anon.get(f"/api/messages/{self.msg.id}/raw")
        self.assertIn(r.status_code, (401, 403))


class SchemaProseSaysWhatTheSampleCannotTests(TestCase):
    """
    The rendered sample is one example, and an example cannot draw "any number
    of these" or "or null". Both were missing, and the intro's "形状必须与下面
    这个样例完全一致" turned each gap into a wrong instruction: one character
    prompt, one library entry, and a picture every single turn.

    Everything here is derived from the schema, so it cannot go stale the way a
    hand-written field list would.
    """

    def _prose(self, schema):
        return providers.base.schema_prose(schema)

    def test_every_array_field_is_named_as_a_list(self):
        prose = self._prose(llm.LEAD_SCHEMA)
        for field in ("edits.settings", "edits.characters",
                      "edits.characters[].variants"):
            self.assertIn(field, prose)
        self.assertIn("0 个、1 个或多个", prose)

    def test_a_capped_list_says_its_cap(self):
        """
        Against a schema written here, not one of the app's.

        None of the real schemas caps a list any more: the six that used to sit
        on character_prompts was the app's own invention and it silently
        dropped the seventh figure of a group scene. The rendering still has to
        work for the day a vendor states a real one, so it is exercised against
        a shape that exists only in this test.
        """
        capped = {
            "name": "capped",
            "schema": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "maxItems": 3,
                        "items": {"type": "string"},
                    },
                },
            },
        }
        self.assertIn("items（最多 3 个）", self._prose(capped))

    def test_the_fields_whose_null_is_the_ordinary_answer_say_so(self):
        prose = self._prose(llm.LEAD_SCHEMA)
        self.assertIn("可以直接写 null", prose)
        self.assertIn("draw、edits", prose)

    def test_a_nullable_scalar_carries_it_inline(self):
        self.assertIn('"<number> | null"', self._prose(llm.DRAW_SCHEMA))

    def test_the_intro_no_longer_demands_every_field(self):
        self.assertNotIn("每个字段都要有", wording.SCHEMA_PROSE_INTRO)
        self.assertNotIn("完全一致", wording.SCHEMA_PROSE_INTRO)

    def test_a_schema_with_no_lists_and_no_nulls_gets_neither_note(self):
        """The notes are facts about the schema, not decoration."""
        plain = {"name": "x", "schema": {
            "type": "object", "properties": {"a": {"type": "string"}},
        }}
        prose = self._prose(plain)
        self.assertNotIn("方括号", prose)
        self.assertNotIn("null", prose)


class OmittedReadsTheSameAsEmptyTests(TestCase):
    """
    wording.SCHEMA_PROSE_OUTRO promises the model that leaving a field out and
    filling it with "" are the same thing. That is a claim about these two
    functions, so it is checked against them rather than trusted.
    """

    def test_a_brief_with_fields_left_out_matches_one_spelled_out_in_full(self):
        sparse = llm._normalize_lead(
            {"reply": "好", "draw": {"composition": "灯塔与船头", "size": "portrait"}}
        )
        spelled = llm._normalize_lead({
            "reply": "好",
            "draw": {
                "change": "", "subjects": [], "composition": "灯塔与船头", "camera": "",
                "scene": "", "light": "", "style": "", "notes": "", "size": "portrait",
            },
            "edits": None,
        })
        self.assertEqual(sparse, spelled)
        self.assertEqual(
            sparse["draw"],
            {"change": "", "subjects": [], "composition": "灯塔与船头", "size": "portrait"},
        )

    def test_the_same_holds_for_the_drawing_model(self):
        brief = {"composition": "灯塔", "size": "portrait"}
        sparse = llm._normalize_draw({"scene": "lighthouse"}, brief, None)
        blank = {"tags": "", "prose": ""}
        spelled = llm._normalize_draw(
            {"scene": {"tags": "lighthouse", "prose": ""}, "count": dict(blank), "style": dict(blank),
             "camera": dict(blank), "light": dict(blank), "other": dict(blank), "text": "",
             "settings": [], "negative_prompt": "", "characters": [], "size": "portrait"},
            brief, None,
        )
        self.assertEqual(sparse, spelled)

    def test_the_wording_no_longer_claims_fields_may_not_be_omitted(self):
        self.assertNotIn("不要省略", wording.SCHEMA_PROSE_OUTRO)

    def test_the_two_fields_that_really_are_load_bearing(self):
        """Everything is optional except the two things there is no picture without."""
        self.assertIsNone(llm._normalize_lead({"reply": "好", "draw": {"notes": "没有主体"}})["draw"])
        with self.assertRaises(llm.ModelError):
            llm._normalize_draw({"negative_prompt": "text"}, {"size": "portrait"}, None)


class ProposalWithNowhereToGoTests(TestCase):
    """
    A conversation outside every project has no library to write to. That is
    the intended behaviour — the library belongs to a project — but the card
    has to say so up front rather than letting the button be pressed and fail.
    """

    def setUp(self):
        self.client.force_login(
            get_user_model().objects.create_user(username="homeless", password="x")
        )
        self.conv = Conversation.objects.create(title="散养对话")  # no folder
        self.msg = Message.objects.create(
            conversation=self.conv, seq=1, role="assistant", status="done", content="好"
        )
        self.proposal = LibraryProposal.objects.create(
            conversation=self.conv, message=self.msg, folder=None,
            payload={"settings": [{"title": "黄昏之城", "content": "永远停在黄昏"}]},
        )

    def test_the_payload_says_it_has_nowhere_to_land(self):
        r = self.client.get(f"/api/conversations/{self.conv.id}/proposals")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["proposals"][0]["folder_id"])

    def test_approving_it_is_refused_and_writes_nothing(self):
        r = self.client.post(
            f"/api/proposals/{self.proposal.id}/resolve",
            json.dumps({"approve": True}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Setting.objects.count(), 0)
        self.proposal.refresh_from_db()
        # still pending: a refusal is not an answer, so moving the conversation
        # into a project later leaves it there to approve
        self.assertEqual(self.proposal.status, "pending")

    def test_a_proposal_that_does_have_a_folder_says_which(self):
        folder = Folder.objects.create(name="夜航")
        conv = Conversation.objects.create(title="有家的对话", folder=folder)
        msg = Message.objects.create(
            conversation=conv, seq=1, role="assistant", status="done", content="好"
        )
        LibraryProposal.objects.create(
            conversation=conv, message=msg, folder=folder, payload={"settings": []}
        )
        r = self.client.get(f"/api/conversations/{conv.id}/proposals")
        self.assertEqual(r.json()["proposals"][0]["folder_id"], str(folder.id))


    def test_a_conversation_without_a_project_writes_to_its_own_layer(self):
        """
        No project means no project library — but the conversation has a
        layer of its own, and that is where the model's note lands, at once.
        The row is still written, so the transcript can show what happened.
        """
        conv = Conversation.objects.create(title="散养对话二号")
        msg = Message.objects.create(
            conversation=conv, seq=1, role="assistant", status="done", content="好"
        )
        pipeline._record_proposal(
            conv, msg, {"settings": [{"title": "黄昏之城", "content": "永远停在黄昏"}]}
        )
        proposal = LibraryProposal.objects.get(conversation=conv)
        self.assertIsNone(proposal.folder)
        self.assertEqual(proposal.status, "applied")
        self.assertEqual(proposal.payload["settings"][0]["title"], "黄昏之城")
        made = Setting.objects.get(title="黄昏之城")
        self.assertEqual(made.conversation_id, conv.id)
        self.assertIsNone(made.folder_id)
        # and it is in play for this conversation without anyone switching it on
        self.assertIn(made.id, set(conv.selected_settings.values_list("id", flat=True)))

    def test_nothing_worth_proposing_still_writes_nothing(self):
        """The silence was only wrong when there was something to say."""
        conv = Conversation.objects.create(title="散养对话三号")
        msg = Message.objects.create(
            conversation=conv, seq=1, role="assistant", status="done", content="好"
        )
        pipeline._record_proposal(conv, msg, {})
        self.assertFalse(LibraryProposal.objects.filter(conversation=conv).exists())


class ProposalMergesAcrossFoldersTests(TestCase):
    """
    A proposal edits the entry the conversation can actually see.

    A conversation reads its whole ancestry, so the character the model is
    talking about often lives in a parent folder. Applying the proposal used to
    look only in the conversation's own folder, so "add a costume to 林" created
    a second 林 one level down — two characters with the same name, one of them
    holding the new costume and nothing else.
    """

    def setUp(self):
        self.client.force_login(
            get_user_model().objects.create_user(username="librarian", password="x")
        )
        self.root = Folder.objects.create(name="夜航")
        self.child = Folder.objects.create(name="设定草稿", parent=self.root)
        self.lin = Character.objects.create(
            folder=self.root, name="林", description="银灰色长发", position=1
        )
        CharacterVariant.objects.create(
            character=self.lin, title="夜航制服", content="深藏青双排扣", position=1
        )
        self.conv = Conversation.objects.create(title="草稿里的对话", folder=self.child)
        self.msg = Message.objects.create(
            conversation=self.conv, seq=1, role="assistant", status="done", content="好"
        )

    def _propose(self, payload):
        proposal = LibraryProposal.objects.create(
            conversation=self.conv, message=self.msg, folder=self.child,
            payload=_project(payload),
        )
        r = self.client.post(
            f"/api/proposals/{proposal.id}/resolve",
            json.dumps({"approve": True}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_a_new_costume_lands_on_the_character_that_already_exists(self):
        self._propose({
            "characters": [{"name": "林", "variants": [
                {"title": "便服", "content": "米色针织衫"},
            ]}],
        })
        self.assertEqual(Character.objects.filter(name="林").count(), 1)
        self.assertEqual(self.lin.variants.count(), 2)
        self.assertEqual(self.lin.variants.get(title="便服").content, "米色针织衫")

    def test_a_setting_in_a_parent_folder_is_updated_not_duplicated(self):
        Setting.objects.create(folder=self.root, title="统一画风", content="厚涂", position=1)
        self._propose({"settings": [{"title": "统一画风", "content": "厚涂，冷色"}]})
        self.assertEqual(Setting.objects.filter(title="统一画风").count(), 1)
        self.assertEqual(Setting.objects.get(title="统一画风").content, "厚涂，冷色")

    def test_the_nearest_one_wins_when_both_levels_have_it(self):
        """Reading resolves most-specific-last; writing has to agree with it."""
        near = Character.objects.create(folder=self.child, name="林", position=1)
        self._propose({"characters": [{"name": "林", "description": "改了描述"}]})
        near.refresh_from_db()
        self.lin.refresh_from_db()
        self.assertEqual(near.description, "改了描述")
        self.assertEqual(self.lin.description, "银灰色长发")

    def test_a_genuinely_new_character_still_lands_in_this_folder(self):
        self._propose({"characters": [{"name": "老陈", "description": "船长"}]})
        self.assertEqual(Character.objects.get(name="老陈").folder, self.child)


class HistoryWindowTests(TestCase):
    """
    The window keeps the NEWEST turns.

    It used to take the oldest `max_messages * 2` rows and then slice the tail
    off that list, which silently threw away the most recent turns the moment a
    conversation grew past the outer bound. Nobody noticed because no
    conversation had got that long yet.
    """

    def test_a_long_conversation_keeps_its_newest_turns(self):
        conv = Conversation.objects.create(title="很长的对话")
        cap = pipeline.HISTORY.max_messages
        total = cap * 3
        for seq in range(1, total + 1):
            Message.objects.create(
                conversation=conv, seq=seq, status="done",
                role="user" if seq % 2 else "assistant", content=f"第{seq}句",
            )
        built = pipeline.build_history(conv, before_seq=total + 1)
        text = " ".join(
            m["content"] if isinstance(m["content"], str)
            else " ".join(p.get("text", "") for p in m["content"])
            for m in built
        )
        self.assertIn(f"第{total}句", text)          # the newest turn
        self.assertIn(f"第{total - 1}句", text)
        self.assertNotIn("第1句", text)              # the oldest fell out, as intended
        # exactly the window (the system prompt is the provider's, not this list's)
        self.assertEqual(len(built), cap)


class BranchCarriesTheDescriptionTests(TestCase):
    """
    A branch is meant to be able to carry on where the original left off, and
    the lead model continues a scene by reading back the picture description it
    wrote. The copy carried model_raw and nai_prompts but not draw_brief, so
    every branch started out amnesiac about its own pictures.
    """

    def setUp(self):
        self.client.force_login(
            get_user_model().objects.create_user(username="brancher", password="x")
        )

    def test_the_branch_copies_the_picture_description(self):
        conv = Conversation.objects.create(title="原对话")
        Message.objects.create(conversation=conv, seq=1, role="user",
                               status="done", content="画灯塔")
        src = Message.objects.create(
            conversation=conv, seq=2, role="assistant", status="done", kind="image",
            content="好", draw_brief={"main": "灯塔与船头", "size": "portrait"},
            nai_prompts={"base_prompt": "lighthouse", "size": "portrait", "n_images": 1},
            prompt_tokens=1234,
        )
        r = self.client.post(f"/api/messages/{src.id}/branch")
        self.assertEqual(r.status_code, 201)
        branch = Conversation.objects.get(id=r.json()["id"])
        copied = branch.messages.get(seq=2)
        self.assertEqual(copied.draw_brief, src.draw_brief)
        self.assertEqual(copied.prompt_tokens, src.prompt_tokens)
        # and it actually reaches the model
        text = " ".join(
            m["content"] for m in pipeline.build_history(branch, before_seq=99)
            if m["role"] == "assistant"
        )
        self.assertIn("灯塔与船头", text)


class BriefInHistoryTests(TestCase):
    """
    The lead model wrote the picture description; it should read its own words
    back rather than reconstruct them from the drawing model's tags.

    How many come back is a dial (pipeline.HISTORY), and it is being moved
    about, so these follow the dial rather than a number. A test that pinned
    "only the newest" would have to be rewritten every time the experiment is
    flipped, which is how a suite stops being believed.
    """

    def _conv(self):
        conv = Conversation.objects.create(title="回看画面描述")
        seq = 0

        def turn(brief, tags):
            nonlocal seq
            seq += 1
            Message.objects.create(
                conversation=conv, seq=seq, role="user", status="done", content=f"第{seq}句"
            )
            seq += 1
            Message.objects.create(
                conversation=conv, seq=seq, role="assistant", status="done",
                content="好", kind="image", draw_brief=brief, nai_prompts=tags,
            )
            return seq

        first = turn({"main": "灯塔与船头", "size": "portrait", "n_images": 1},
                     {"base_prompt": "lighthouse", "size": "portrait", "n_images": 1})
        second = turn({"main": "码头的雨", "size": "landscape", "n_images": 2},
                      {"base_prompt": "rain", "size": "landscape", "n_images": 2})
        return conv, first, second

    def _assistant_text(self, conv, before_seq=9999):
        return " ".join(
            m["content"] for m in pipeline.build_history(conv, before_seq=before_seq)
            if m["role"] == "assistant"
        )

    def _app_text(self, conv, before_seq=9999):
        """
        Everything the app itself said, in its own turns.

        One sender for all of them — see _app_note — so this looks in one place.
        """
        out = []
        for m in pipeline.build_history(conv, before_seq=before_seq):
            if m["role"] != "user" or not isinstance(m["content"], list):
                continue
            for part in m["content"]:
                if part["type"] == "text" and part["text"].startswith(
                    wording.SYSTEM_NOTE_OPEN
                ):
                    out.append(part["text"])
        return " ".join(out)

    def test_the_newest_description_always_comes_back(self):
        text = self._assistant_text(self._conv()[0])
        self.assertIn("码头的雨", text)

    def test_every_description_comes_back_because_it_is_part_of_the_answer(self):
        """
        There is no dial for this any more. A description is one field of the
        answer the assistant gave, and a window either carries a turn or it
        does not — editing what it said on the way past is the thing that
        taught it to answer in the wrong shape.
        """
        conv, _, _ = self._conv()
        text = self._assistant_text(conv)
        self.assertIn("灯塔与船头", text)
        self.assertIn("码头的雨", text)

    def test_by_default_every_turns_tags_come_back(self):
        """
        Not a dial reading — the dial's SETTING, which is the thing a live
        conversation actually gets.

        The lead model is writing the next brief, and the tags are the only
        record of what the last few briefs turned into: a scene it described
        three turns ago and one it described now differ in ways that show up
        nowhere else. On one turn's worth it was writing blind about
        everything before that.

        The test beside this one patches the dial, so it passes at any setting
        and cannot notice this being turned back down.
        """
        conv, _, _ = self._conv()
        said = self._app_text(conv)
        self.assertIn("rain", said)
        self.assertIn("lighthouse", said, "an earlier turn's tags went missing")

    def test_the_tags_are_the_apps_to_ration_and_are_said_by_the_app(self):
        """
        The tags came from the drawing model, not from the assistant, so they
        are in the app's own turn — and how many turns back they ride is still
        the app's dial, because it is the app's material.
        """
        conv, _, _ = self._conv()
        with mock.patch.object(
            pipeline, "HISTORY", replace(pipeline.HISTORY, prompt_turns=1)
        ):
            said = self._app_text(conv)
        self.assertIn("rain", said)
        self.assertNotIn("lighthouse", said)
        with mock.patch.object(
            pipeline, "HISTORY", replace(pipeline.HISTORY, prompt_turns=None)
        ):
            said = self._app_text(conv)
        self.assertIn("lighthouse", said)
        # and never in the assistant's mouth
        self.assertNotIn("lighthouse", self._assistant_text(conv))

    def test_the_assistant_turn_is_the_object_it_returned(self):
        """
        Verbatim, and parseable as what it is: five of fifty-eight measured
        DeepSeek calls answered with the prose block this replaces instead of
        with JSON. See reports/21-deepseek-failures.html.
        """
        conv, _, _ = self._conv()
        turns = [
            m["content"] for m in pipeline.build_history(conv, before_seq=9999)
            if m["role"] == "assistant"
        ]
        self.assertEqual(len(turns), 2)
        for said in turns:
            got = json.loads(said)
            self.assertEqual(set(got), {"reply", "draw", "edits"})
            self.assertEqual(got["reply"], "好")
        self.assertEqual(json.loads(turns[-1])["draw"]["main"], "码头的雨")

    def test_the_library_edits_it_proposed_come_back_with_it(self):
        """So it can see what it has already suggested, and not suggest it again."""
        conv = Conversation.objects.create(title="带着建议")
        Message.objects.create(conversation=conv, seq=1, role="user", status="done",
                               content="记一下这个设定")
        Message.objects.create(
            conversation=conv, seq=2, role="assistant", status="done", content="记下了",
            model_raw={"reply": "记下了",
                       "edits": _project({"settings": [{"title": "黄昏之城", "content": "永远停在黄昏"}],
                                          "characters": []})},
        )
        said = [m["content"] for m in pipeline.build_history(conv, before_seq=9999)
                if m["role"] == "assistant"][0]
        self.assertEqual(json.loads(said)["edits"]["settings"][0]["title"], "黄昏之城")

    def test_a_turn_that_drew_nothing_says_so_the_way_the_schema_does(self):
        conv = Conversation.objects.create(title="没画图")
        Message.objects.create(conversation=conv, seq=1, role="user", status="done",
                               content="先聊聊")
        Message.objects.create(conversation=conv, seq=2, role="assistant", status="done",
                               content="好啊")
        said = [m["content"] for m in pipeline.build_history(conv, before_seq=9999)
                if m["role"] == "assistant"][0]
        self.assertIsNone(json.loads(said)["draw"])

    def test_a_drawing_gets_a_name_and_keeps_it(self):
        """
        One name for one drawing — the brief, the tags made from it, and the
        pictures that came back. A reflection round comes back through here
        with new tags and it is the same drawing, so renaming it there would
        rename the thing the user is pointing at while they look at it.
        """
        conv = Conversation.objects.create(title="编号")
        msg = Message.objects.create(conversation=conv, seq=1, role="assistant",
                                     status="pending", content="")
        first = pipeline._with_count(msg, {"base_prompt": "x", "n_images": 1})
        self.assertTrue(first["draw_id"])

        # a reflection round: new tags, same drawing
        again = pipeline._with_count(msg, {**first, "base_prompt": "x, better"})
        self.assertEqual(again["draw_id"], first["draw_id"])

    def test_two_drawings_get_two_names(self):
        conv = Conversation.objects.create(title="编号")
        ids = set()
        for i in range(8):
            msg = Message.objects.create(conversation=conv, seq=i + 1, role="assistant",
                                         status="pending", content="")
            ids.add(pipeline._with_count(msg, {"base_prompt": "x"})["draw_id"])
        self.assertEqual(len(ids), 8)

    def test_the_name_is_one_a_person_can_read_back(self):
        """No i, l, o, 0 or 1: this gets read off a screen and typed by hand."""
        for _ in range(60):
            got = pipeline.new_draw_id()
            self.assertEqual(len(got), 6)
            self.assertFalse(set(got) & set("ilo01"), got)

    def test_the_history_says_which_drawing_it_is_talking_about(self):
        conv, _, _ = self._conv()
        said = self._app_text(conv)
        drawn = [
            (m.nai_prompts or {}).get("draw_id")
            for m in conv.messages.filter(role="assistant")
        ]
        for draw_id in [d for d in drawn if d]:
            self.assertIn(draw_id, said)

    def test_reflection_is_told_which_drawing_it_is_looking_at(self):
        """
        Reflection builds its own history — it does not go through
        build_history — which is exactly how it became the one place the name
        was missing. It is also the turn that needs it most: it is being shown
        a picture and asked about that picture.
        """
        from core.services import reflection

        block = reflection._images_block(
            ["att"],
            lambda a: "data:image/png;base64,AAA",
            {"base_prompt": "x", "draw_id": "abc234"},
        )
        assert block is not None
        self.assertIn("abc234", block["content"][0]["text"])

    def test_every_way_a_picture_reaches_a_model_names_it(self):
        """
        The rule as stated: whenever history carries a drawing, it carries the
        drawing's name. Written against every builder there is, so adding a
        third one without a name fails here rather than in a conversation.
        """
        from core.services import reflection

        prompts = {"base_prompt": "x", "size": "portrait", "n_images": 1,
                   "draw_id": "zz9876"}

        conv = Conversation.objects.create(title="到处都要有编号")
        Message.objects.create(conversation=conv, seq=1, role="user",
                               status="done", content="画")
        msg = Message.objects.create(conversation=conv, seq=2, role="assistant",
                                     status="done", content="好", nai_prompts=prompts)
        att = Attachment.objects.create(message=msg, kind="generated", mime="image/png")
        att.file.save("a.png", ContentFile(_png_bytes()), save=True)

        built = " ".join(
            p.get("text", "")
            for m in pipeline.build_history(conv, before_seq=9999)
            for p in (m["content"] if isinstance(m["content"], list) else [])
            if isinstance(p, dict)
        )
        block = reflection._images_block([att], lambda a: "data:image/png;base64,AAA",
                                         prompts)
        assert block is not None
        reflected = block["content"][0]["text"]

        for where, text in (("build_history", built), ("reflection", reflected)):
            with self.subTest(where=where):
                self.assertIn("zz9876", text)

    def test_a_turn_that_drew_nothing_is_not_announced(self):
        """An id with nothing to name is noise in the context."""
        conv = Conversation.objects.create(title="没画图")
        Message.objects.create(conversation=conv, seq=1, role="user",
                               status="done", content="聊聊")
        Message.objects.create(conversation=conv, seq=2, role="assistant",
                               status="done", content="好啊",
                               nai_prompts={"draw_id": "abcdef"})
        self.assertNotIn("abcdef", self._app_text(conv))

    def test_the_app_always_speaks_as_the_same_sender(self):
        """
        One voice, one sender, whatever the note is carrying.

        `system` is what these are, and it cannot be used: a note carrying
        pictures is refused outright — measured 2026-09-16, DeepSeek answers
        400 "Image in system message is unsupported". Sending only the plain
        ones as `system` was tried and reverted, because it made the app's
        identity depend on its contents: one speaker, two senders, inside a
        single conversation.

        So this asserts consistency rather than which sender won.
        """
        senders = {
            pipeline._app_note(["只有文字。"], [])["role"],
            pipeline._app_note(["带了图。"], ["data:image/png;base64,AAA"])["role"],
        }
        self.assertEqual(len(senders), 1, f"the app spoke as {senders}")

    def test_no_message_this_app_builds_puts_a_picture_in_a_system_turn(self):
        """
        The wall the rule above rests on: DeepSeek refuses it with a 400, so a
        history that grows one takes every turn carrying a picture down with it.
        """
        conv, _, _ = self._conv()
        for m in pipeline.build_history(conv, before_seq=9999):
            if m["role"] != "system":
                continue
            self.assertIsInstance(
                m["content"], str,
                "a system message with parts is one image away from a 400",
            )

    def test_nothing_the_app_says_is_put_in_anyone_elses_mouth(self):
        """
        The app is not the assistant and not the user. Whatever it wants the
        model to know goes in a turn with its own sender on it.
        """
        conv, _, _ = self._conv()
        history = pipeline.build_history(conv, before_seq=9999)
        for m in history:
            if m["role"] != "assistant":
                continue
            got = json.loads(m["content"])   # nothing appended to it, ever
            self.assertEqual(set(got), {"reply", "draw", "edits"})
        self.assertIn(wording.SYSTEM_NOTE_OPEN, self._app_text(conv))

    def test_branching_early_picks_the_brief_that_was_last_back_then(self):
        conv, first, _ = self._conv()
        # a branch cut right after the first drawing turn only sees seq <= first
        text = " ".join(
            m["content"] for m in pipeline.build_history(conv, before_seq=first + 1)
            if m["role"] == "assistant"
        )
        self.assertIn("灯塔与船头", text)
        self.assertNotIn("码头的雨", text)

    def test_a_turn_that_drew_nothing_carries_no_brief(self):
        conv = Conversation.objects.create(title="没画")
        Message.objects.create(conversation=conv, seq=1, role="user", status="done", content="聊聊")
        Message.objects.create(conversation=conv, seq=2, role="assistant", status="done", content="好啊")
        text = " ".join(
            m["content"] for m in pipeline.build_history(conv, before_seq=9999)
            if m["role"] == "assistant"
        )
        self.assertNotIn("画面描述", text)

    def test_the_brief_is_kept_on_every_row_either_way(self):
        conv, first, second = self._conv()
        briefs = list(
            conv.messages.filter(role="assistant").order_by("seq")
            .values_list("draw_brief", flat=True)
        )
        self.assertEqual([b["main"] for b in briefs], ["灯塔与船头", "码头的雨"])


class NoopProposalTests(TestCase):
    """
    The lead model can see the selected library, so it often restates a
    costume or a setting verbatim. Those must not become proposals the user
    has to dismiss for nothing.
    """

    def setUp(self):
        self.folder = Folder.objects.create(name="项目", position=0)
        Setting.objects.create(
            folder=self.folder, title="画风", content="厚涂，冷色调。", position=1
        )
        self.char = Character.objects.create(
            folder=self.folder, name="林", description="银发，紫眼。", position=1
        )
        CharacterVariant.objects.create(
            character=self.char, title="制服", content="藏青外套。", position=1
        )
        self.conv = Conversation.objects.create(title="c", folder=self.folder)
        self.msg = Message.objects.create(
            conversation=self.conv, seq=1, role="assistant", status="done"
        )

    def _record(self, edits):
        pipeline._record_proposal(self.conv, self.msg, edits)
        return LibraryProposal.objects.filter(message=self.msg).first()

    def test_identical_content_records_nothing(self):
        self.assertIsNone(
            self._record(
                {
                    "settings": [{"title": "画风", "content": "厚涂，冷色调。"}],
                    "characters": [
                        {
                            "name": "林",
                            "description": "",
                            "variants": [{"title": "制服", "content": "藏青外套。"}],
                        }
                    ],
                }
            )
        )

    def test_changed_variant_survives(self):
        prop = self._record(
            {
                "settings": [],
                "characters": [
                    {
                        "name": "林",
                        "description": "",
                        "variants": [
                            {"title": "制服", "content": "藏青外套。"},
                            {"title": "便装", "content": "米色针织衫。"},
                        ],
                    }
                ],
            }
        )
        assert prop is not None
        chars = prop.payload["characters"]
        self.assertEqual(len(chars), 1)
        # only the new costume is left for the user to look at
        self.assertEqual(
            [v["title"] for v in chars[0]["variants"]], ["便装"]
        )

    def test_blank_description_is_not_a_change(self):
        self.assertIsNone(
            self._record(
                {"settings": [], "characters": [{"name": "林", "description": "", "variants": []}]}
            )
        )

    def test_new_setting_survives(self):
        prop = self._record(
            {"settings": [{"title": "世界观", "content": "海上城邦。"}], "characters": []}
        )
        assert prop is not None
        self.assertEqual(prop.payload["settings"][0]["title"], "世界观")

    def test_conversation_without_folder_records_it_and_says_it_cannot_apply(self):
        """
        There is still nowhere to write it — but that is something to say, not
        something to do quietly. The row exists so the transcript can show what
        the model asked for and why it cannot be written; resolve refuses the
        approval separately. This test used to assert the opposite, which is
        how the silence survived being noticed twice.
        """
        loose = Conversation.objects.create(title="无项目")
        msg = Message.objects.create(
            conversation=loose, seq=1, role="assistant", status="done"
        )
        pipeline._record_proposal(
            loose, msg, {"settings": [{"title": "x", "content": "y"}], "characters": []}
        )
        prop = LibraryProposal.objects.filter(message=msg).first()
        assert prop is not None
        self.assertIsNone(prop.folder)


@override_settings(**TEST_CREDS)
class FolderArchiveTests(TestCase):
    """A folder is put away, never deleted — and it takes its contents with it."""

    def setUp(self):
        self.c = Client()
        self.c.post(
            "/api/auth/login",
            json.dumps({"username": "tester", "password": "test-password-123"}),
            content_type="application/json",
        )
        self.parent = Folder.objects.create(name="项目", position=0)
        self.child = Folder.objects.create(name="子", parent=self.parent, position=0)
        self.conv = Conversation.objects.create(title="里面的对话", folder=self.child)
        self.loose = Conversation.objects.create(title="外面的对话")

    def _tree(self, scope=""):
        url = "/api/projects/tree" + (f"?scope={scope}" if scope else "")
        return self.c.get(url).json()

    def test_archiving_hides_the_whole_subtree(self):
        self.c.post(
            f"/api/folders/{self.parent.id}/archive",
            json.dumps({"archived": True}),
            content_type="application/json",
        )
        tree = self._tree()
        self.assertEqual(tree["folders"], [])
        # the conversation inside went with it; the one outside did not
        titles = [c["title"] for c in tree["conversations"]]
        self.assertNotIn("里面的对话", titles)
        self.assertIn("外面的对话", titles)

    def test_the_archive_lists_only_what_was_archived_itself(self):
        self.c.post(
            f"/api/folders/{self.parent.id}/archive",
            json.dumps({"archived": True}),
            content_type="application/json",
        )
        names = [f["name"] for f in self._tree("archived")["folders"]]
        # the child is hidden because its parent is, not in its own right
        self.assertEqual(names, ["项目"])

    def test_coming_back_under_an_archived_parent_goes_to_the_root(self):
        self.c.post(
            f"/api/folders/{self.parent.id}/archive",
            json.dumps({"archived": True}),
            content_type="application/json",
        )
        self.c.post(
            f"/api/folders/{self.child.id}/archive",
            json.dumps({"archived": False}),
            content_type="application/json",
        )
        self.child.refresh_from_db()
        # it would be invisible under a parent that is still put away
        self.assertIsNone(self.child.parent_id)

    def test_archive_endpoint_returns_both_kinds(self):
        self.c.post(
            f"/api/folders/{self.parent.id}/archive",
            json.dumps({"archived": True}),
            content_type="application/json",
        )
        self.c.post(
            f"/api/conversations/{self.loose.id}/archive",
            json.dumps({"archived": True}),
            content_type="application/json",
        )
        data = self.c.get("/api/archive").json()
        self.assertEqual([f["name"] for f in data["folders"]], ["项目"])
        self.assertEqual([c["title"] for c in data["conversations"]], ["外面的对话"])

    def test_unarchived_folder_comes_back_into_the_tree(self):
        for archived in (True, False):
            self.c.post(
                f"/api/folders/{self.parent.id}/archive",
                json.dumps({"archived": archived}),
                content_type="application/json",
            )
        names = sorted(f["name"] for f in self._tree()["folders"])
        self.assertEqual(names, ["子", "项目"])


@override_settings(**TEST_CREDS)
class VariantDeletionTests(TestCase):
    """
    A costume can be removed on its own.

    "Only ever edited, never deleted" is a rule about the model — _apply_proposal
    has no way to take anything out of the library. It had been written onto the
    endpoint as though it bound the user too, so the only way to drop one
    costume was to delete the character wearing it.
    """

    def setUp(self):
        self.c = Client()
        self.c.force_login(
            get_user_model().objects.create_user(username="dresser", password="x")
        )
        self.folder = Folder.objects.create(name="夜航")
        self.character = Character.objects.create(folder=self.folder, name="林", position=1)
        self.a = CharacterVariant.objects.create(
            character=self.character, title="夜航制服", content="深藏青", position=1
        )
        self.b = CharacterVariant.objects.create(
            character=self.character, title="便服", content="米色针织衫", position=2
        )

    def test_one_costume_goes_without_taking_the_character(self):
        r = self.c.delete(f"/api/variants/{self.b.id}")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(CharacterVariant.objects.filter(id=self.b.id).exists())
        self.assertTrue(Character.objects.filter(id=self.character.id).exists())
        self.assertTrue(CharacterVariant.objects.filter(id=self.a.id).exists())

    def test_a_conversation_that_had_it_selected_simply_stops_having_it(self):
        conv = Conversation.objects.create(title="c", folder=self.folder)
        conv.selected_variants.set([self.a, self.b])
        self.c.delete(f"/api/variants/{self.b.id}")
        self.assertEqual([v.id for v in conv.selected_variants.all()], [self.a.id])

    def test_what_a_past_turn_was_drawn_from_is_untouched(self):
        """The snapshot is a copy; that is what makes deleting safe."""
        conv = Conversation.objects.create(title="c", folder=self.folder)
        msg = Message.objects.create(
            conversation=conv, seq=1, role="assistant", status="done", content="好",
            selection_snapshot={
                "settings": [],
                "characters": [{"name": "林", "variants": [{"title": "便服", "content": "米色针织衫"}]}],
            },
        )
        self.c.delete(f"/api/variants/{self.b.id}")
        msg.refresh_from_db()
        self.assertEqual(
            (msg.selection_snapshot or {})["characters"][0]["variants"][0]["title"], "便服"
        )

    def test_editing_still_works(self):
        r = self.c.patch(
            f"/api/variants/{self.a.id}",
            json.dumps({"title": "夜航制服（旧）"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.a.refresh_from_db()
        self.assertEqual(self.a.title, "夜航制服（旧）")

    def test_the_model_still_cannot_delete_anything(self):
        """
        The rule that actually exists: a proposal adds and updates, and has no
        shape for removal at all.
        """
        from core.services.proposals import apply as _apply_proposal

        proposal = LibraryProposal.objects.create(
            conversation=Conversation.objects.create(title="c", folder=self.folder),
            message=Message.objects.create(
                conversation=Conversation.objects.create(title="c2", folder=self.folder),
                seq=1, role="assistant", status="done",
            ),
            folder=self.folder,
            payload={"characters": [{"name": "林", "variants": []}]},
        )
        _apply_proposal(proposal)
        self.assertEqual(self.character.variants.count(), 2)
