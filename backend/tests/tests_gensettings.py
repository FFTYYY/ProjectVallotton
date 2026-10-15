"""
The generation settings belong to a conversation; the defaults belong to the app.

出图数量, 反思轮数, 连续生成, 常驻参考 used to be one set of values in the
browser's localStorage, shared by every conversation at once — so a run of ten
armed in one chat was still armed when you opened another. What each of these
pins is one half of the fix: a conversation owns its own, and a new one is
poured from the defaults the settings page holds.
"""

from __future__ import annotations

import json

from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from core.models import AppSettings, Conversation, Message
from core.services import gensettings

TEST_CREDS = {"APP_USERNAME": "yyy", "APP_PASSWORD": "pw"}


class DefaultsAreCompleteTests(TestCase):
    def test_a_conversation_older_than_the_column_reads_as_the_defaults(self):
        conv = Conversation.objects.create(title="c")
        self.assertIsNone(conv.gen_settings)
        self.assertEqual(gensettings.of(conv), gensettings.DEFAULTS)

    def test_rubbish_is_replaced_rather_than_returned(self):
        conv = Conversation.objects.create(
            title="c",
            gen_settings={"n_images": "many", "reflection_rounds": None,
                          "continuous_rounds": -4},
        )
        got = gensettings.of(conv)
        self.assertEqual(got["n_images"], gensettings.DEFAULTS["n_images"])
        self.assertEqual(
            got["reflection_rounds"], gensettings.DEFAULTS["reflection_rounds"]
        )
        self.assertEqual(got["continuous_rounds"], 1)

    def test_every_value_is_clamped_to_its_own_ceiling(self):
        from django.conf import settings as dj

        conv = Conversation.objects.create(
            title="c",
            gen_settings={"n_images": 99, "reflection_rounds": 999,
                          "continuous_rounds": 999},
        )
        got = gensettings.of(conv)
        self.assertEqual(got["n_images"], dj.MAX_IMAGES_PER_REQUEST)
        self.assertEqual(got["reflection_rounds"], dj.MAX_REFLECTION_ROUNDS)
        # its own ceiling, not the reflection one: both used to go through the
        # same parser, so asking for fifteen pictures quietly got you ten
        self.assertEqual(got["continuous_rounds"], dj.MAX_CONTINUOUS_ROUNDS)
        self.assertGreater(dj.MAX_CONTINUOUS_ROUNDS, dj.MAX_REFLECTION_ROUNDS)

    def test_a_partial_change_leaves_the_rest_alone(self):
        conv = Conversation.objects.create(
            title="c", gen_settings={"n_images": 3, "reflection_rounds": 4}
        )
        gensettings.merge(conv, {"sticky_refs": True})
        got = gensettings.of(conv)
        self.assertEqual(got["n_images"], 3)
        self.assertEqual(got["reflection_rounds"], 4)
        self.assertTrue(got["sticky_refs"])


@override_settings(**TEST_CREDS)
class SettingsBelongToOneConversationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.force_login(
            get_user_model().objects.create_user(username="s", password="x")
        )

    def _new(self):
        r = self.client.post("/api/conversations", "{}",
                             content_type="application/json")
        self.assertEqual(r.status_code, 201)
        return r.json()

    def test_changing_one_conversation_leaves_the_others_alone(self):
        first, second = self._new(), self._new()
        r = self.client.patch(
            f"/api/conversations/{first['id']}",
            json.dumps({"gen_settings": {"reflection_rounds": 6}}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["gen_settings"]["reflection_rounds"], 6)

        other = self.client.get(f"/api/conversations/{second['id']}/messages").json()
        self.assertEqual(
            other["conversation"]["gen_settings"]["reflection_rounds"],
            gensettings.DEFAULTS["reflection_rounds"],
        )

    def test_a_new_conversation_is_poured_from_the_defaults(self):
        gensettings.set_defaults({"n_images": 4, "reflection_rounds": 0})
        conv = self._new()
        self.assertEqual(conv["gen_settings"]["n_images"], 4)
        self.assertEqual(conv["gen_settings"]["reflection_rounds"], 0)

    def test_a_new_conversation_takes_what_the_page_was_showing(self):
        # The new-chat page has the same four controls, and the first turn is
        # sent with whatever they were set to. Without this the row said one
        # thing and that turn did another — and the next turn in the same
        # conversation quietly went back to the defaults.
        gensettings.set_defaults({"n_images": 1, "reflection_rounds": 1})
        r = self.client.post(
            "/api/conversations",
            json.dumps({"gen_settings": {"n_images": 3}}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 201)
        conv = r.json()
        self.assertEqual(conv["gen_settings"]["n_images"], 3)
        # and the ones it said nothing about are still the defaults
        self.assertEqual(conv["gen_settings"]["reflection_rounds"], 1)

    def test_what_the_page_sends_is_clamped_like_anything_else(self):
        r = self.client.post(
            "/api/conversations",
            json.dumps({"gen_settings": {"n_images": 99, "reflection_rounds": -3}}),
            content_type="application/json",
        )
        conv = r.json()
        self.assertEqual(
            conv["gen_settings"]["n_images"], django_settings.MAX_IMAGES_PER_REQUEST
        )
        self.assertEqual(conv["gen_settings"]["reflection_rounds"], 0)

    def test_rubbish_where_the_settings_go_is_ignored_not_fatal(self):
        r = self.client.post(
            "/api/conversations",
            json.dumps({"gen_settings": "四张"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["gen_settings"], gensettings.defaults())

    def test_changing_the_defaults_does_not_reach_into_existing_conversations(self):
        conv = self._new()
        before = conv["gen_settings"]["n_images"]
        gensettings.set_defaults({"n_images": 4})
        again = self.client.get(f"/api/conversations/{conv['id']}/messages").json()
        self.assertEqual(again["conversation"]["gen_settings"]["n_images"], before)

    def test_a_branch_carries_on_under_the_same_settings(self):
        conv = self._new()
        self.client.patch(
            f"/api/conversations/{conv['id']}",
            json.dumps({"gen_settings": {"n_images": 3, "sticky_refs": True}}),
            content_type="application/json",
        )
        src = Conversation.objects.get(id=conv["id"])
        msg = Message.objects.create(conversation=src, seq=1, role="user",
                                     status="done", content="画")
        r = self.client.post(f"/api/messages/{msg.id}/branch")
        self.assertEqual(r.status_code, 201)
        branch = Conversation.objects.get(id=r.json()["id"])
        self.assertEqual(gensettings.of(branch)["n_images"], 3)
        self.assertTrue(gensettings.of(branch)["sticky_refs"])

    def test_a_send_that_says_nothing_uses_the_conversation_s(self):
        conv = self._new()
        self.client.patch(
            f"/api/conversations/{conv['id']}",
            json.dumps({"gen_settings": {"n_images": 4, "reflection_rounds": 2,
                                         "sticky_refs": True}}),
            content_type="application/json",
        )
        from unittest import mock

        with mock.patch("core.services.jobs.start_generation"):
            r = self.client.post(f"/api/conversations/{conv['id']}/messages",
                                 {"text": "画一张"})
        self.assertEqual(r.status_code, 201)
        row = Message.objects.filter(
            conversation_id=conv["id"], role="assistant"
        ).order_by("-seq").first()
        assert row is not None
        self.assertEqual(row.force_n_images, 4)
        self.assertEqual(row.force_reflection_rounds, 2)
        self.assertTrue(row.sticky_refs)


@override_settings(**TEST_CREDS)
class AppSettingsEndpointTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.force_login(
            get_user_model().objects.create_user(username="a", password="x")
        )

    def test_it_reads_back_complete_before_anything_is_ever_saved(self):
        self.assertFalse(AppSettings.objects.exists())
        got = self.client.get("/api/app-settings").json()
        self.assertEqual(got["gen_defaults"], gensettings.DEFAULTS)

    def test_saving_a_default_keeps_the_others(self):
        self.client.patch(
            "/api/app-settings",
            json.dumps({"gen_defaults": {"continuous_rounds": 5}}),
            content_type="application/json",
        )
        got = self.client.get("/api/app-settings").json()["gen_defaults"]
        self.assertEqual(got["continuous_rounds"], 5)
        self.assertEqual(got["n_images"], gensettings.DEFAULTS["n_images"])

    def test_it_says_what_the_backend_is_busy_with(self):
        conv = Conversation.objects.create(title="c")
        Message.objects.create(conversation=conv, seq=1, role="assistant",
                               status="pending")
        Conversation.objects.filter(id=conv.id).update(
            continuous_run={"left": 3, "of": 4, "text": "继续", "free": 2,
                            "done": 1, "failed": 0}
        )
        running = self.client.get("/api/app-settings").json()["running"]
        self.assertEqual(running["turns"], 1)
        self.assertEqual(running["conversations"], 1)
        self.assertEqual(running["runs"], 1)

    def test_an_archived_turn_is_not_something_it_is_busy_with(self):
        conv = Conversation.objects.create(title="c")
        Message.objects.create(conversation=conv, seq=1, role="assistant",
                               status="pending", archived=True)
        running = self.client.get("/api/app-settings").json()["running"]
        self.assertEqual(running["turns"], 0)

    def test_rubbish_is_refused_rather_than_stored(self):
        r = self.client.patch(
            "/api/app-settings",
            json.dumps({"gen_defaults": "everything"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)
