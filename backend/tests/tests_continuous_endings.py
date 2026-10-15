"""
How a continuous run ends, and the promise that it always says so.

Two rules, both stated in core/services/continuous.py:

  * a run ends for four reasons and no others — the user typed, the user
    stopped it, it ran out of retries, or the backend stopped running;
  * every run is a pair of notes. One when it opens, one when it closes,
    whichever of the four closed it.

The second rule is what these are mostly about. A run that vanished without a
closing note is indistinguishable, to someone who was not watching, from a run
still going — and not watching is the whole point of the feature.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from core.models import (
    Attachment,
    Conversation,
    LibraryProposal,
    Message,
    QuickPhrase,
)
from core.services import continuous, gensettings, jobs

TEST_CREDS = {"APP_USERNAME": "yyy", "APP_PASSWORD": "pw"}


@override_settings(**TEST_CREDS)
class RunEndingTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.force_login(
            get_user_model().objects.create_user(username="ender", password="x")
        )
        self.conv = Conversation.objects.create(title="c")

    # ------------------------------------------------------------- utilities

    def _send(self, rounds=3, text="继续下一张"):
        # The instruction is the server's to resolve, from the phrase chosen on
        # the conversation — see continuous._instruction. A suite that handed
        # it in would be testing a path the app no longer has.
        QuickPhrase.objects.get_or_create(title="再来", defaults={"content": text})
        self._armed_n_images = gensettings.of(self.conv)["n_images"]
        self._armed_reflection = gensettings.of(self.conv)["reflection_rounds"]
        with mock.patch("core.services.jobs.start_generation"):
            r = self.client.post(
                f"/api/conversations/{self.conv.id}/messages",
                {"text": "画一张", "continuous_rounds": str(rounds)},
            )
        self.assertEqual(r.status_code, 201)
        row = self.conv.messages.filter(role="assistant").order_by("-seq").first()
        assert row is not None
        return row

    def _end(self, msg, status="done"):
        """Finish a turn the way the pipeline would, then let the run look."""
        Message.objects.filter(id=msg.id).update(status=status)
        return self._look()

    def _look(self):
        """What the clock does. The nudge at the end of a turn does the same."""
        with mock.patch("core.services.jobs.start_generation"):
            continuous.reconcile(self.conv.id)
        return self.conv.messages.filter(
            role="assistant", archived=False
        ).order_by("-seq").first()

    def _pending(self):
        return self.conv.messages.filter(
            role="assistant", archived=False, status="pending"
        ).order_by("-seq").first()

    def _notes(self):
        """Every bracket this conversation has written, oldest first."""
        return list(
            self.conv.messages.filter(role="system")
            .exclude(notice_kind="")
            .order_by("seq")
            .values_list("notice_kind", flat=True)
        )

    def _run(self):
        self.conv.refresh_from_db()
        return self.conv.continuous_run

    # ------------------------------------------------------------- the pair

    def test_a_run_opens_with_a_note_above_the_message_that_started_it(self):
        first = self._send(rounds=3)
        self.assertEqual(self._notes(), ["continuous_start"])
        opening = self.conv.messages.get(notice_kind="continuous_start")
        user_row = self.conv.messages.filter(role="user").order_by("seq").first()
        assert user_row is not None
        self.assertLess(opening.seq, user_row.seq, "the note landed under the message")
        self.assertLess(opening.seq, first.seq)

    def test_a_run_that_finishes_closes_its_bracket(self):
        msg = self._send(rounds=2)
        self._end(msg, "done")          # round 1 of 2, one more goes out
        nxt = self.conv.messages.filter(
            role="assistant", archived=False, status="pending"
        ).order_by("-seq").first()
        assert nxt is not None
        self._end(nxt, "done")          # round 2 of 2 — the run is spent
        self.assertIsNone(self._run())
        self.assertEqual(self._notes(), ["continuous_start", "continuous"])

    def test_stopping_closes_its_bracket(self):
        msg = self._send(rounds=5)
        r = self.client.post(f"/api/messages/{msg.id}/cancel")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(self._run())
        self.assertEqual(self._notes(), ["continuous_start", "continuous"])

    def test_running_out_of_retries_closes_its_bracket(self):
        """
        Nothing worked, not once. This used to write no closing note at all, on
        the grounds that the failed turn spoke for itself — which is true of
        the failure and silent about the run, whose opening note was still up
        there promising two pictures.
        """
        self._send(rounds=2)
        for _ in range(12):
            if self._run() is None:
                break
            pending = self.conv.messages.filter(
                role="assistant", archived=False, status="pending"
            ).order_by("-seq").first()
            assert pending is not None
            self._end(pending, "error")
        self.assertIsNone(self._run(), "the run never gave up")
        self.assertEqual(self._notes(), ["continuous_start", "continuous"])

    def test_a_message_the_user_typed_closes_the_old_bracket_and_opens_a_new_one(self):
        self._send(rounds=3)
        # a run with nothing pending: the only state in which the app will
        # accept a typed message while a run is on the books
        Message.objects.filter(conversation=self.conv, status="pending").update(
            status="done"
        )
        self._send(rounds=2)
        self.assertEqual(
            self._notes(), ["continuous_start", "continuous", "continuous_start"]
        )
        self.assertEqual((self._run() or {})["of"], 2)

    # ----------------------------------------------- the ending with no path

    def test_a_run_with_nothing_running_simply_starts_the_next_one(self):
        """
        The backend stopped between finishing one turn and queueing the next.
        That used to need machinery of its own — a sweep that hunted for runs
        nobody was working on and closed them, guessing from timestamps whether
        anyone still was, and cutting live runs short when it guessed wrong.

        There is nothing to guess. A run that wants more and has nothing
        running is a run whose next turn has not started yet.
        """
        self._send(rounds=4)
        # the turn finished and nobody ever heard about it
        Message.objects.filter(conversation=self.conv, status="pending").update(
            status="done"
        )
        self._look()
        self.assertIsNotNone(self._run(), "the run was closed instead of carried on")
        self.assertEqual((self._run() or {})["done"], 1)
        self.assertIsNotNone(self._pending(), "no next turn was queued")
        self.assertEqual(self._notes(), ["continuous_start"])

    def test_it_leaves_a_turn_that_is_still_running_alone(self):
        """The one thing it must never do is start a second one."""
        self._send(rounds=4)
        before = self._pending()
        assert before is not None
        for _ in range(5):
            self._look()
        still = self._pending()
        assert still is not None, "the turn that was running disappeared"
        self.assertEqual(still.id, before.id)
        self.assertEqual(
            self.conv.messages.filter(
                role="assistant", archived=False, status="pending"
            ).count(),
            1,
            "a second turn was started while the first was still running",
        )
        self.assertEqual((self._run() or {})["done"], 0)

    def test_looking_again_and_again_changes_nothing(self):
        """
        Safe to call at any time, from anywhere, as often as you like. The old
        design was not — two callers reaching it for the same turn spent two
        rounds and queued two replacements, which then painted side by side.
        """
        msg = self._send(rounds=5)
        self._end(msg, "done")
        after_one = dict(self._run() or {})
        for _ in range(4):
            self._look()
        self.assertEqual(dict(self._run() or {}), after_one)
        self.assertEqual(
            self.conv.messages.filter(
                role="assistant", archived=False, status="pending"
            ).count(),
            1,
        )

    # --------------------------------------------------------- counted once

    def test_a_turn_is_counted_once_however_many_callers_report_it(self):
        """
        The worker that finished a turn and the reaper that gave up on it can
        both reach the accounting for the same one. Both used to be served: the
        run spent two rounds on one turn and queued two replacements, which
        then painted side by side.
        """
        msg = self._send(rounds=5)
        self._end(msg, "done")
        after_one = dict(self._run() or {})
        self._look()                    # the other caller, arriving late
        after_two = dict(self._run() or {})

        self.assertEqual(after_one["done"], after_two["done"])
        self.assertEqual(after_one["left"], after_two["left"])
        self.assertEqual(
            self.conv.messages.filter(
                role="assistant", archived=False, status="pending"
            ).count(),
            1,
            "a second replacement was queued and would have painted in parallel",
        )

    def test_a_reaped_turn_that_already_made_pictures_is_not_a_failure(self):
        """
        Pictures that exist were paid for. Reaping such a turn as an error
        counted a failure, spent a round, and archived the row the pictures
        were hanging on — losing them.
        """
        msg = self._send(rounds=3)
        att = Attachment(message=msg, kind="generated", mime="image/png",
                         width=8, height=8, size_bytes=1)
        att.file.save("x.png", ContentFile(b"x"), save=True)
        Message.objects.filter(id=msg.id).update(
            heartbeat_at=timezone.now() - timedelta(hours=1)
        )
        with mock.patch("core.services.jobs.start_generation"):
            jobs.reap_stale_jobs()
        msg.refresh_from_db()
        self.assertEqual(msg.status, "done")
        self.assertFalse(msg.archived)
        self.assertTrue(msg.attachments.filter(kind="generated").exists())
        self.assertEqual((self._run() or {})["done"], 1)
        self.assertEqual((self._run() or {})["failed"], 0)

    # ------------------------------------------------------- the two ceilings

    def test_a_run_can_be_longer_than_the_reflection_ceiling(self):
        """
        The two sliders have different ceilings and both went through the
        reflection parser, so anything past MAX_REFLECTION_ROUNDS came back
        clamped to it: you asked for fifteen pictures and got ten, from a
        slider that goes to twenty.
        """
        asked = settings.MAX_REFLECTION_ROUNDS + 5
        self.assertLessEqual(asked, settings.MAX_CONTINUOUS_ROUNDS)
        self._send(rounds=asked)
        self.assertEqual((self._run() or {})["of"], asked)

    def test_a_run_still_stops_at_its_own_ceiling(self):
        self._send(rounds=settings.MAX_CONTINUOUS_ROUNDS + 7)
        self.assertEqual(
            (self._run() or {})["of"], settings.MAX_CONTINUOUS_ROUNDS
        )

    # ------------------------------- a run is armed once and not re-read

    def test_moving_the_slider_does_not_reach_into_a_run_already_in_flight(self):
        """
        A run carries the settings it was armed with. The sliders go on being
        editable while it runs — they are what the NEXT send will ask for —
        and a run that changed length under the user because they nudged a
        control would be the worst kind of surprise.
        """
        msg = self._send(rounds=5)
        r = self.client.patch(
            f"/api/conversations/{self.conv.id}",
            json.dumps({"gen_settings": {"continuous_rounds": 1, "n_images": 4,
                                         "reflection_rounds": 9}}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)

        run = self._run() or {}
        self.assertEqual(run["of"], 5, "the run changed length under the user")
        self.assertEqual(run["left"], 4)

        # and the turns it queues carry what it was armed with, not what the
        # sliders say now
        nxt = self._end(msg, "done")
        assert nxt is not None
        self.assertEqual(nxt.force_n_images, self._armed_n_images)
        self.assertEqual(nxt.force_reflection_rounds, self._armed_reflection)
        self.assertEqual((self._run() or {})["of"], 5)

    def test_a_run_still_ends_where_it_was_told_to(self):
        """Five asked for, five run, whatever the sliders said in between."""
        msg = self._send(rounds=3)
        self.client.patch(
            f"/api/conversations/{self.conv.id}",
            json.dumps({"gen_settings": {"continuous_rounds": 20}}),
            content_type="application/json",
        )
        turns = 0
        while self._run() is not None and turns < 12:
            pending = self.conv.messages.filter(
                role="assistant", archived=False, status="pending"
            ).order_by("-seq").first()
            assert pending is not None
            self._end(pending if turns else msg, "done")
            turns += 1
        self.assertEqual(turns, 3, "the run did not stop where it was armed to")
        self.assertEqual(self._notes(), ["continuous_start", "continuous"])

    # --------------------------------------------------------------- 重跑

    def _rerun(self, **body):
        with mock.patch("core.services.jobs.start_generation"):
            return self.client.post(
                f"/api/conversations/{self.conv.id}/rerun",
                json.dumps(body),
                content_type="application/json",
            )

    def test_rerun_is_refused_while_a_turn_is_still_running(self):
        """
        Which is most of a run. It is the same guard that stops a typed
        message: there is a turn in flight, and replacing the answer to a
        question that is still being answered is not a thing to do quietly.
        """
        self._send(rounds=5)
        r = self._rerun()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json().get("code"), "busy")
        self.assertIsNotNone(self._run(), "the run was ended by a refused rerun")
        self.assertEqual(self._notes(), ["continuous_start"])

    def test_rerun_between_turns_ends_the_run_and_says_so(self):
        """
        重跑 is the user asking again, so it starts over — and the run that was
        under way ends. It is one of the four endings, so it gets its closing
        note like any other: a run that simply stopped appearing, with the
        opening note still up there promising five, is the thing this whole
        pair exists to prevent.
        """
        self._send(rounds=5)
        Message.objects.filter(conversation=self.conv, status="pending").update(
            status="done"
        )
        r = self._rerun(continuous_rounds=1)
        self.assertEqual(r.status_code, 201)
        self.assertIsNone(self._run())
        self.assertEqual(self._notes(), ["continuous_start", "continuous"])

    def test_a_rerun_that_asks_for_a_run_opens_a_fresh_one(self):
        self._send(rounds=5)
        Message.objects.filter(conversation=self.conv, status="pending").update(
            status="done"
        )
        r = self._rerun(continuous_rounds=3, continuous_text="再来一张")
        self.assertEqual(r.status_code, 201)
        self.assertEqual((self._run() or {})["of"], 3)
        self.assertEqual(
            self._notes(), ["continuous_start", "continuous", "continuous_start"]
        )

    def test_the_note_a_rerun_writes_is_not_swept_away_with_the_old_turn(self):
        """
        重跑 archives the assistant and system rows after the last user
        message, and then ends the run. Written the other way round, the note
        saying the run ended would be archived a moment after it appeared.
        """
        self._send(rounds=5)
        Message.objects.filter(conversation=self.conv, status="pending").update(
            status="done"
        )
        self._rerun(continuous_rounds=1)
        visible = list(
            self.conv.messages.filter(role="system", archived=False)
            .values_list("notice_kind", flat=True)
        )
        self.assertIn("continuous", visible, "the closing note was archived")

    def test_rerunning_the_last_turn_keeps_the_note_the_run_ended_with(self):
        """
        A finished run leaves its summary after the last turn, so a rerun —
        which clears the tail after the last user message — archived it. The
        opening note stayed, and nothing ever answered it.
        """
        msg = self._send(rounds=2)
        self._end(msg, "done")
        nxt = self.conv.messages.filter(
            role="assistant", archived=False, status="pending"
        ).order_by("-seq").first()
        assert nxt is not None
        self._end(nxt, "done")
        self.assertIsNone(self._run())
        self.assertEqual(self._notes(), ["continuous_start", "continuous"])

        r = self._rerun(continuous_rounds=1)
        self.assertEqual(r.status_code, 201)
        visible = list(
            self.conv.messages.filter(role="system", archived=False)
            .order_by("seq")
            .values_list("notice_kind", flat=True)
        )
        self.assertEqual(
            visible, ["continuous_start", "continuous"],
            "rerunning the last turn swallowed the run's own record",
        )

    def test_a_turn_carrying_pictures_counts_however_its_status_reads(self):
        """
        Pictures are paid for the moment they exist. The retry path archives
        the turn it replaces, so judging a turn by its status alone is how a
        run throws away work somebody was billed for — on the word of whichever
        writer happened to get there last.
        """
        msg = self._send(rounds=4)
        att = Attachment(message=msg, kind="generated", mime="image/png",
                         width=8, height=8, size_bytes=1)
        att.file.save("kept.png", ContentFile(b"x"), save=True)
        Message.objects.filter(id=msg.id).update(status="error", error="boom")

        self._look()
        msg.refresh_from_db()
        self.assertFalse(msg.archived, "a turn with pictures was archived")
        self.assertEqual((self._run() or {})["done"], 1)
        self.assertEqual((self._run() or {})["failed"], 0)
        self.assertTrue(
            self.conv.messages.filter(role="user", auto_sent__isnull=False).exists(),
            "the run treated it as a failure and replayed instead of moving on",
        )

    def test_a_running_turn_still_counts_even_if_something_archived_it(self):
        """
        Being archived is about what the reader sees; it says nothing about
        whether a worker is painting. Reading it as "nothing is running" is how
        a second turn gets started alongside the first — two pictures being
        made at once, and one of them thrown away.

        Nothing in the app archives a running turn today. That is a convention
        held up by a 409 in one view, not a rule, and this is here so the
        convention breaking does not cost pictures.
        """
        self._send(rounds=4)
        pending = self._pending()
        assert pending is not None
        Message.objects.filter(id=pending.id).update(archived=True)

        self._look()
        self.assertEqual(
            self.conv.messages.filter(role="assistant", status="pending").count(),
            1,
            "a second turn was started beside one that was still running",
        )
        self.assertEqual((self._run() or {})["done"], 0)

    def test_archiving_a_finished_turn_does_not_lose_it_from_the_tally(self):
        """
        Archiving is about what the reader sees. A turn that happened,
        happened — whether it is on screen is a separate question from whether
        the run has counted it.
        """
        msg = self._send(rounds=4)
        Message.objects.filter(id=msg.id).update(status="done", archived=True)
        self._look()
        self.assertEqual((self._run() or {})["done"], 1, "an archived turn went uncounted")
        self.assertIsNotNone(self._pending())

    def test_archiving_a_running_turn_does_not_unlock_the_composer(self):
        """
        The same question in a different place: the send and rerun guards ask
        "is a turn running?", and archiving one must not make them answer no —
        that would let a second turn start beside the first.
        """
        self._send(rounds=4)
        pending = self._pending()
        assert pending is not None
        Message.objects.filter(id=pending.id).update(archived=True)

        with mock.patch("core.services.jobs.start_generation"):
            sent = self.client.post(
                f"/api/conversations/{self.conv.id}/messages", {"text": "插一句"}
            )
            rerun = self.client.post(f"/api/conversations/{self.conv.id}/rerun")
        self.assertEqual(sent.status_code, 409)
        self.assertEqual(rerun.status_code, 409)

        listed = self.client.get(
            f"/api/conversations/{self.conv.id}/messages"
        ).json()
        self.assertTrue(listed["has_pending"], "the app reported itself idle")

    # --------------------------------------- the instruction is the server's

    def test_the_run_starts_even_though_the_browser_sent_no_words(self):
        """
        The phone case. The right rail starts closed, the phrase list is only
        fetched there, so the browser had nothing to send — and the run was
        armed with an empty instruction, which is to say not armed at all: one
        picture, no note, no error.

        The chosen phrase is on the conversation. The server reads it.
        """
        phrase = QuickPhrase.objects.create(title="再来", content="再画一张")
        gensettings.merge(self.conv, {"continuous_phrase_id": str(phrase.id)})
        with mock.patch("core.services.jobs.start_generation"):
            r = self.client.post(
                f"/api/conversations/{self.conv.id}/messages",
                {"text": "画一张", "continuous_rounds": "4"},
            )
        self.assertEqual(r.status_code, 201)
        self.assertEqual((self._run() or {})["of"], 4)
        self.assertEqual((self._run() or {})["text"], "再画一张")
        self.assertEqual(self._notes(), ["continuous_start"])

    def test_with_nothing_chosen_it_picks_the_first_and_remembers(self):
        """What the rail would have picked, done where it cannot be skipped."""
        QuickPhrase.objects.create(title="甲", content="第一条")
        QuickPhrase.objects.create(title="乙", content="第二条")
        with mock.patch("core.services.jobs.start_generation"):
            self.client.post(
                f"/api/conversations/{self.conv.id}/messages",
                {"text": "画一张", "continuous_rounds": "3"},
            )
        self.assertEqual((self._run() or {})["text"], "第一条")
        self.conv.refresh_from_db()
        self.assertIsNotNone(
            gensettings.of(self.conv)["continuous_phrase_id"],
            "it picked one and did not write the choice down",
        )

    def test_with_no_phrases_at_all_it_says_so_instead_of_going_quiet(self):
        """
        A silent branch here is indistinguishable from never having asked.
        The user pressed for ten and must not be left thinking they did not.
        """
        self.assertFalse(QuickPhrase.objects.exists())
        with mock.patch("core.services.jobs.start_generation"):
            r = self.client.post(
                f"/api/conversations/{self.conv.id}/messages",
                {"text": "画一张", "continuous_rounds": "6"},
            )
        self.assertEqual(r.status_code, 201)
        self.assertIsNone(self._run())
        self.assertEqual(self._notes(), ["continuous_start"])
        note = self.conv.messages.get(notice_kind="continuous_start")
        self.assertIn("没有开始", note.content)

    # ------------------------------------------------- a restart, structurally

    def test_a_restart_picks_the_run_back_up_without_waiting_on_a_clock(self):
        """
        The backend went down mid-turn. A process that has just started is
        working on nothing, so a row that says "being made" is a row whose
        maker is gone — a fact, not an inference from a timestamp. The run then
        carries on, and none of it waits for a stale window to pass.
        """
        self._send(rounds=4)
        pending = self._pending()
        assert pending is not None
        # heartbeat_at is deliberately left fresh: nothing here reads it
        with mock.patch("core.services.jobs.start_generation"):
            jobs.adopt_orphans()

        pending.refresh_from_db()
        self.assertEqual(pending.status, "error")
        self.assertIsNotNone(self._run(), "the run was abandoned by a restart")
        self.assertIsNotNone(self._pending(), "the run did not carry on")
        self.assertEqual(self._notes(), ["continuous_start"])

    def test_a_restart_keeps_the_pictures_a_turn_had_already_made(self):
        self._send(rounds=4)
        pending = self._pending()
        assert pending is not None
        att = Attachment(message=pending, kind="generated", mime="image/png",
                         width=8, height=8, size_bytes=1)
        att.file.save("half.png", ContentFile(b"x"), save=True)

        with mock.patch("core.services.jobs.start_generation"):
            jobs.adopt_orphans()

        pending.refresh_from_db()
        self.assertEqual(pending.status, "done")
        self.assertFalse(pending.archived)
        self.assertEqual((self._run() or {})["done"], 1)
        self.assertEqual((self._run() or {})["failed"], 0)

    def test_nothing_pending_is_nothing_to_adopt(self):
        self._send(rounds=3)
        Message.objects.filter(conversation=self.conv, status="pending").update(
            status="done"
        )
        with mock.patch("core.services.jobs.start_generation"):
            self.assertEqual(jobs.adopt_orphans(), 0)

    # ------------------------------------------- things that must NOT end it

    def test_a_library_proposal_does_not_end_a_run(self):
        msg = self._send(rounds=4)
        LibraryProposal.objects.create(
            conversation=self.conv, message=msg, folder=None,
            payload={"settings": [{"title": "画风", "content": "厚涂"}]},
        )
        self.assertIsNotNone(self._run())
        self.assertEqual(self._notes(), ["continuous_start"])

    def test_resolving_a_proposal_does_not_end_a_run(self):
        msg = self._send(rounds=4)
        p = LibraryProposal.objects.create(
            conversation=self.conv, message=msg, folder=None, payload={"settings": []},
        )
        r = self.client.post(f"/api/proposals/{p.id}/resolve",
                             data='{"approve": false}',
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(self._run())
        self.assertEqual(self._notes(), ["continuous_start"])

    def test_archiving_the_conversation_does_not_end_a_run(self):
        """Accepted on purpose: 「5是可以接受的」."""
        self._send(rounds=4)
        r = self.client.post(f"/api/conversations/{self.conv.id}/archive",
                             data='{"archived": true}',
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(self._run())


@override_settings(**TEST_CREDS)
class InheritedTurnsAreNotRoundsTests(TestCase):
    """
    A run counts "the newest turn nobody has counted yet". That phrasing is
    only safe if every turn that exists when the run starts has been counted
    off — and rows arrive already uncounted from more than one direction: a
    branch copies its source's turns, and rows written before the column
    existed default to uncounted too.

    Left as they were, the first thing a run did in a fresh branch was count
    an inherited turn as a round of its own: the user asked for three pictures
    and got two.
    """

    def setUp(self):
        self.client = Client()
        self.client.force_login(
            get_user_model().objects.create_user(username="yyy", password="pw")
        )
        QuickPhrase.objects.create(title="再来", content="再画一张")

    def _conversation_with_history(self) -> Conversation:
        conv = Conversation.objects.create(title="有来历的对话")
        Message.objects.create(conversation=conv, seq=1, role="user", status="done",
                               content="画一张")
        Message.objects.create(conversation=conv, seq=2, role="assistant", status="done",
                               kind="image", content="给你",
                               nai_prompts={"base_prompt": "x", "character_prompts": [],
                                            "negative_prompt": "", "size": "portrait",
                                            "n_images": 1},
                               run_settled=False)  # as a copy or an old row arrives
        return conv

    def test_arming_settles_everything_that_is_already_here(self):
        conv = self._conversation_with_history()
        continuous.arm(conv, 3, n_images=None, reflection_rounds=None, sticky_refs=False)
        unsettled = conv.messages.filter(role="assistant", run_settled=False).count()
        self.assertEqual(unsettled, 0)

    def test_an_inherited_turn_is_not_counted_as_a_round(self):
        conv = self._conversation_with_history()
        continuous.arm(conv, 3, n_images=None, reflection_rounds=None, sticky_refs=False)
        run = Conversation.objects.get(id=conv.id).continuous_run
        assert run is not None
        before = dict(run)
        with mock.patch("core.services.jobs.start_generation"):
            continuous.reconcile(conv.id)
        run = Conversation.objects.get(id=conv.id).continuous_run
        assert run is not None, "the run ended before it painted anything"
        self.assertEqual(run["done"], before["done"],
                         "an inherited turn was counted as a round of this run")

    def test_a_branch_carries_its_history_already_counted(self):
        conv = self._conversation_with_history()
        src = conv.messages.order_by("seq").last()
        assert src is not None
        r = self.client.post(f"/api/messages/{src.id}/branch")
        self.assertEqual(r.status_code, 201)
        branch = Conversation.objects.get(id=r.json()["id"])
        self.assertFalse(
            branch.messages.filter(role="assistant", run_settled=False).exists(),
            "a copied turn arrived in the branch looking like work to be counted",
        )
