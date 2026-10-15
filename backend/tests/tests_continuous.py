"""
Continuous mode, from the backend's side.

There is not much of it here on purpose: the feature is a front-end shortcut,
and the whole point is that the models cannot tell. What the backend owns is
one marker on the user's row — who actually pressed send — and the promise
that the marker never becomes something a model reads.
"""

from __future__ import annotations

import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from core.models import Attachment, Conversation, Message, QuickPhrase
from core.services import pipeline
from core.views.chat import _parse_auto_sent

TEST_CREDS = {"APP_USERNAME": "yyy", "APP_PASSWORD": "pw"}


class AutoSentMarkerTests(TestCase):
    """
    It is written straight onto a row the transcript reads back, so a
    malformed one shows up as a note nobody can explain. Anything that is not
    two sane numbers is treated as absent: a wrong note is worse than none.
    """

    def test_a_good_marker_survives(self):
        self.assertEqual(_parse_auto_sent('{"round": 2, "of": 5}'), {"round": 2, "of": 5})
        self.assertEqual(_parse_auto_sent({"round": 1, "of": 1}), {"round": 1, "of": 1})

    def test_absent_stays_absent(self):
        self.assertIsNone(_parse_auto_sent(None))
        self.assertIsNone(_parse_auto_sent(""))

    def test_nonsense_is_dropped_rather_than_stored(self):
        for raw in (
            "not json",
            "{}",
            '{"round": 0, "of": 3}',       # rounds are 1-based
            '{"round": 4, "of": 3}',       # past the end of its own run
            '{"round": "x", "of": 3}',
            '{"round": 1, "of": 9999}',    # past the ceiling
            "[1, 2]",
        ):
            self.assertIsNone(_parse_auto_sent(raw), raw)


@override_settings(**TEST_CREDS)
class AutoSentRoundTripTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.force_login(
            get_user_model().objects.create_user(username="runner", password="x")
        )
        self.conv = Conversation.objects.create(title="连续样本")

    def _send(self, **extra):
        with mock.patch("core.services.jobs.start_generation"):
            r = self.client.post(
                f"/api/conversations/{self.conv.id}/messages",
                {"text": "继续下一张", **extra},
            )
        self.assertEqual(r.status_code, 201)
        row = self.conv.messages.filter(role="user").order_by("-seq").first()
        assert row is not None
        return row

    def test_the_marker_reaches_the_row_and_comes_back_out(self):
        row = self._send(auto_sent=json.dumps({"round": 3, "of": 4}))
        self.assertEqual(row.auto_sent, {"round": 3, "of": 4})
        listed = self.client.get(f"/api/conversations/{self.conv.id}/messages").json()
        sent = [m for m in listed["messages"] if m["seq"] == row.seq][0]
        self.assertEqual(sent["auto_sent"], {"round": 3, "of": 4})

    def test_a_message_the_user_typed_carries_no_marker(self):
        self.assertIsNone(self._send().auto_sent)

    def test_the_model_is_told_nothing_about_any_of_it(self):
        """
        「连续生成模式对AI来说是不可知的」. The model reads the same words it
        would have read if they had been typed, and no more — so this checks
        the built message array, not the intention.
        """
        self._send(auto_sent=json.dumps({"round": 2, "of": 3}))
        built = pipeline.build_history(self.conv, before_seq=999)
        blob = json.dumps(built, ensure_ascii=False)
        self.assertIn("继续下一张", blob)       # the words, exactly as typed
        self.assertNotIn("auto_sent", blob)
        self.assertNotIn("连续", blob)
        self.assertNotIn("round", blob)

    def test_a_branch_carries_who_sent_it(self):
        row = self._send(auto_sent=json.dumps({"round": 2, "of": 3}))
        r = self.client.post(f"/api/messages/{row.id}/branch")
        self.assertEqual(r.status_code, 201)
        branch = Conversation.objects.get(id=r.json()["id"])
        self.assertEqual(branch.messages.get(seq=row.seq).auto_sent, {"round": 2, "of": 3})

    def test_the_marker_is_not_something_a_client_can_use_to_lie(self):
        """It says which round of which run; it cannot claim to be round 9 of 3."""
        row = self._send(auto_sent=json.dumps({"round": 9, "of": 3}))
        self.assertIsNone(row.auto_sent)


class ContinuousCeilingTests(TestCase):
    def test_there_is_one(self):
        from django.conf import settings

        self.assertGreaterEqual(settings.MAX_CONTINUOUS_ROUNDS, 2)
        over = json.dumps({"round": 1, "of": settings.MAX_CONTINUOUS_ROUNDS + 1})
        self.assertIsNone(_parse_auto_sent(over))
        ok = json.dumps({"round": 1, "of": settings.MAX_CONTINUOUS_ROUNDS})
        self.assertIsNotNone(_parse_auto_sent(ok))


@override_settings(**TEST_CREDS)
class AutoSentDoesNotDisturbTheTurnTests(TestCase):
    """The marker is a note on a row, not an input to anything."""

    def setUp(self):
        self.client = Client()
        self.client.force_login(
            get_user_model().objects.create_user(username="quiet", password="x")
        )

    def test_the_assistant_row_created_alongside_it_is_ordinary(self):
        conv = Conversation.objects.create(title="c")
        with mock.patch("core.services.jobs.start_generation"):
            self.client.post(
                f"/api/conversations/{conv.id}/messages",
                {"text": "继续", "auto_sent": json.dumps({"round": 2, "of": 2})},
            )
        assistant = conv.messages.get(role="assistant")
        self.assertIsNone(assistant.auto_sent)
        self.assertEqual(assistant.status, "pending")
        # and the marker sits on the user's row, where it belongs
        self.assertEqual(
            conv.messages.get(role="user").auto_sent, {"round": 2, "of": 2}
        )


class MessageFieldsTests(TestCase):
    def test_auto_sent_defaults_to_nothing(self):
        conv = Conversation.objects.create(title="c")
        m = Message.objects.create(
            conversation=conv, seq=1, role="user", status="done", content="x"
        )
        self.assertIsNone(m.auto_sent)


@override_settings(**TEST_CREDS)
class TheRunLivesOnTheServerTests(TestCase):
    """
    「几乎不要有任何和任务相关的东西依赖前端」.

    A run used to be a counter in the browser, which meant a refresh ended it
    silently half way through — four asked for, three delivered, nothing said.
    These drive it with no browser at all: the only thing a client does is ask
    for one.
    """

    def setUp(self):
        self.client = Client()
        self.client.force_login(
            get_user_model().objects.create_user(username="runner2", password="x")
        )
        self.conv = Conversation.objects.create(title="c")

    def _send(self, rounds=3, text="继续下一张"):
        # the words are resolved server-side from the chosen phrase; a suite
        # that handed them in would be testing a path the app no longer has
        QuickPhrase.objects.get_or_create(title="再来", defaults={"content": text})
        with mock.patch("core.services.jobs.start_generation"):
            r = self.client.post(
                f"/api/conversations/{self.conv.id}/messages",
                {"text": "画一张", "continuous_rounds": str(rounds),
                 "n_images": "2"},
            )
        self.assertEqual(r.status_code, 201)
        row = self.conv.messages.filter(role="assistant").order_by("-seq").first()
        assert row is not None
        return row

    def _end(self, msg, status="done"):
        """Finish a turn the way the pipeline would, then let the run look."""
        from core.services import continuous as c

        Message.objects.filter(id=msg.id).update(status=status)
        with mock.patch("core.services.jobs.start_generation"):
            c.reconcile(self.conv.id)
        row = self.conv.messages.filter(role="assistant", archived=False).order_by("-seq").first()
        assert row is not None
        return row

    def test_sending_arms_a_run_on_the_conversation(self):
        self._send(rounds=3)
        self.conv.refresh_from_db()
        run = self.conv.continuous_run or {}
        self.assertEqual(run["left"], 2)
        self.assertEqual(run["of"], 3)
        self.assertEqual(run["text"], "继续下一张")

    def test_one_round_is_not_a_run(self):
        self._send(rounds=1)
        self.conv.refresh_from_db()
        self.assertIsNone(self.conv.continuous_run)

    def test_no_phrase_is_not_a_run_either(self):
        self._send(rounds=4, text="")
        self.conv.refresh_from_db()
        self.assertIsNone(self.conv.continuous_run)

    def test_a_finished_turn_sends_the_next_instruction(self):
        first = self._send(rounds=3)
        nxt = self._end(first)
        self.assertNotEqual(nxt.id, first.id)
        self.assertEqual(nxt.status, "pending")
        sent = self.conv.messages.filter(role="user").order_by("-seq").first()
        assert sent is not None
        self.assertEqual(sent.content, "继续下一张")
        self.assertEqual(sent.auto_sent, {"round": 2, "of": 3})

    def test_every_turn_in_the_run_gets_the_settings_it_started_with(self):
        first = self._send(rounds=3)
        self.assertEqual(first.force_n_images, 2)
        nxt = self._end(first)
        self.assertEqual(nxt.force_n_images, 2)

    def test_it_stops_at_the_number_asked_for(self):
        msg = self._send(rounds=3)
        for _ in range(5):
            msg = self._end(msg)
            self.conv.refresh_from_db()
            if self.conv.continuous_run is None:
                break
        self.assertIsNone(self.conv.continuous_run)
        self.assertEqual(self.conv.messages.filter(role="user", auto_sent__isnull=False).count(), 2)

    def test_it_leaves_a_note_saying_what_it_came_to(self):
        msg = self._send(rounds=2)
        msg = self._end(msg)
        self._end(msg)
        note = self.conv.messages.filter(role="system", notice_kind="continuous").first()
        assert note is not None
        self.assertIn("连续生成完成", note.content)
        self.assertIn("共2轮", note.content)

    def test_the_note_is_for_the_reader_only(self):
        """system rows never reach a model."""
        msg = self._send(rounds=2)
        self._end(self._end(msg))
        built = pipeline.build_history(self.conv, before_seq=9999)
        self.assertNotIn("连续生成", json.dumps(built, ensure_ascii=False))

    def test_a_failed_turn_is_retried_without_the_browser(self):
        msg = self._send(rounds=3)
        again = self._end(msg, status="error")
        self.assertEqual(again.status, "pending")
        self.conv.refresh_from_db()
        # a free retry: the run still has its rounds
        self.assertEqual((self.conv.continuous_run or {})["left"], 2)
        msg.refresh_from_db()
        self.assertTrue(msg.archived)

    def test_a_stop_ends_the_run(self):
        msg = self._send(rounds=3)
        r = self.client.post(f"/api/messages/{msg.id}/cancel")
        self.assertEqual(r.status_code, 200)
        self.conv.refresh_from_db()
        self.assertIsNone(self.conv.continuous_run)

    def test_the_chip_can_read_the_run(self):
        self._send(rounds=3)
        listed = self.client.get("/api/conversations").json()
        row = [c for c in listed["conversations"] if c["id"] == str(self.conv.id)][0]
        self.assertEqual(row["continuous"], {"left": 2, "of": 3})
