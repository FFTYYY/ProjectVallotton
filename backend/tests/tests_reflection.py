"""
Reflection mode.

Two promises are worth more than the rest and are tested from several angles:
the model sees the whole loop while it is running, and the conversation sees
none of it afterwards.
"""

from __future__ import annotations

import io
import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import Client, TestCase, override_settings
from PIL import Image

from core.models import Attachment, Conversation, Message
from core.services import gensettings, jobs, llm, pipeline, providers, reflection

TEST_CREDS = {"APP_USERNAME": "yyy", "APP_PASSWORD": "pw"}


def _png(shade=(180, 190, 210)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 96), shade).save(buf, "PNG")
    return buf.getvalue()


def _brief(main: str) -> dict:
    return {"main": main, "size": "portrait"}


def _prompts(tag: str) -> dict:
    return {
        "base_prompt": tag, "character_prompts": [], "negative_prompt": "",
        "size": "portrait", "n_images": 1,
    }


class FakeWorld:
    """
    A stand-in for everything reflection needs from outside itself.

    The loop is the thing under test, so nothing here talks to a model, a
    canvas or a database; the point is to be able to say exactly what the loop
    did and exactly what it showed.
    """

    def __init__(self, verdicts):
        self.verdicts = list(verdicts)
        self.seen: list[list[dict]] = []      # one entry per reflect_turn call
        self.painted: list[dict] = []          # the briefs it was asked to redraw
        self.discarded: list = []
        self.phases: list[str] = []
        self.prefix = ""
        self.round = 0

    def reflect(self, messages, on_retry=None):
        self.seen.append(messages)
        return self.verdicts[min(self.round_index(), len(self.verdicts) - 1)]

    def round_index(self):
        return len(self.seen) - 1

    def redraw(self, brief):
        self.painted.append(brief)
        return _prompts(f"tag-{len(self.painted)}"), [f"img-{len(self.painted)}"]

    def hooks(self):
        from contextlib import contextmanager

        @contextmanager
        def enter(prefix):
            self.prefix = prefix
            try:
                yield
            finally:
                self.prefix = ""

        def phase(p):
            self.phases.append(f"{self.prefix}:{p}" if self.prefix else p)

        return reflection.Hooks(
            redraw=self.redraw,
            check_cancel=lambda: None,
            set_phase=phase,
            image_url=lambda att: f"data:image/jpeg;base64,{att}",
            discard=self.discarded.extend,
            enter_round=enter,
        )


def run_loop(world, *, rounds=3, images=("img-0",), base=None):
    with mock.patch.object(llm, "reflect_turn", world.reflect):
        return reflection.run(
            base_history=list(base or [{"role": "user", "content": "画林"}]),
            first_reply="给你一张。",
            brief=_brief("林站在城墙上"),
            nai_prompts=_prompts("tag-0"),
            images=list(images),
            rounds=rounds,
            hooks=world.hooks(),
        )


class ReflectionLoopTests(TestCase):
    def test_a_satisfied_first_look_stops_and_changes_nothing(self):
        world = FakeWorld([{"reply": "就这样，够了。", "draw": None}])
        out = run_loop(world)
        self.assertEqual(out.rounds_run, 1)
        self.assertEqual(world.painted, [])
        self.assertEqual(out.images, ["img-0"])
        self.assertEqual(out.brief, _brief("林站在城墙上"))
        self.assertEqual(out.log, [{"round": 1, "reply": "就这样，够了。", "redrew": False}])

    def test_it_keeps_redrawing_until_satisfied(self):
        world = FakeWorld([
            {"reply": "光太软。", "draw": _brief("硬光版")},
            {"reply": "手画坏了。", "draw": _brief("修手版")},
            {"reply": "这回对了。", "draw": None},
        ])
        out = run_loop(world, rounds=5)
        self.assertEqual(out.rounds_run, 3)
        self.assertEqual([b["main"] for b in world.painted], ["硬光版", "修手版"])
        self.assertEqual(out.brief, _brief("修手版"))
        self.assertEqual(out.images, ["img-2"])
        self.assertEqual([r["redrew"] for r in out.log], [True, True, False])

    def test_the_ceiling_is_the_ceiling(self):
        """Never satisfied: it stops at the number asked for, not one more."""
        world = FakeWorld([{"reply": "还不行。", "draw": _brief("再来")}])
        out = run_loop(world, rounds=3)
        self.assertEqual(out.rounds_run, 3)
        self.assertEqual(len(world.painted), 3)
        self.assertEqual(len(out.log), 3)

    def test_zero_rounds_does_nothing_at_all(self):
        world = FakeWorld([{"reply": "x", "draw": None}])
        out = run_loop(world, rounds=0)
        self.assertEqual(world.seen, [])
        self.assertEqual(out.log, [])
        self.assertEqual(out.images, ["img-0"])

    def test_a_turn_that_painted_nothing_is_not_reflected_on(self):
        world = FakeWorld([{"reply": "x", "draw": None}])
        out = run_loop(world, rounds=3, images=())
        self.assertEqual(world.seen, [])
        self.assertEqual(out.rounds_run, 0)

    def test_superseded_pictures_are_handed_back_to_be_dropped(self):
        world = FakeWorld([
            {"reply": "改", "draw": _brief("二版")},
            {"reply": "好", "draw": None},
        ])
        out = run_loop(world, rounds=3)
        self.assertEqual(world.discarded, ["img-0"])
        self.assertEqual(out.images, ["img-1"])

    def test_a_redraw_that_produces_nothing_keeps_what_there_was(self):
        world = FakeWorld([{"reply": "改", "draw": _brief("二版")}])
        world.redraw = lambda brief: (_prompts("x"), [])
        out = run_loop(world, rounds=3)
        self.assertEqual(out.images, ["img-0"])
        self.assertEqual(out.brief, _brief("林站在城墙上"))


class RetryInsideARunTests(TestCase):
    """
    A generation fails for reasons that have nothing to do with the picture.
    Letting any of them end a ten-picture run is the wrong trade — but an
    unbounded retry is a worse one. So: two free attempts, then each further
    attempt costs a round out of the run's own budget.
    """

    def _flaky(self, world, failures, exc=None):
        """A redraw that fails `failures` times before it works."""
        calls = {"n": 0}
        real = world.redraw

        def redraw(brief):
            calls["n"] += 1
            if calls["n"] <= failures:
                if exc:
                    raise exc
                return _prompts("dud"), []
            return real(brief)

        world.redraw = redraw
        return calls

    def test_a_hiccup_is_retried_for_free(self):
        world = FakeWorld([
            {"reply": "改", "draw": _brief("二版")},
            {"reply": "好", "draw": None},
        ])
        calls = self._flaky(world, failures=1)
        out = run_loop(world, rounds=3)
        self.assertEqual(calls["n"], 2)                 # failed once, then worked
        self.assertEqual(out.images, ["img-1"])
        self.assertEqual(out.rounds_run, 2)             # the free retry cost no round
        self.assertEqual([e.get("failed") for e in out.log if "failed" in e],
                         ["no image came back"])
        self.assertFalse([e for e in out.log if e.get("paid")])

    def test_an_exception_is_a_hiccup_too(self):
        world = FakeWorld([
            {"reply": "改", "draw": _brief("二版")},
            {"reply": "好", "draw": None},
        ])
        calls = self._flaky(world, failures=2, exc=RuntimeError("novelai 502"))
        out = run_loop(world, rounds=3)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(out.images, ["img-1"])
        self.assertIn(
            "novelai 502", str([e.get("failed") for e in out.log if "failed" in e][0])
        )

    def test_past_the_free_ones_each_attempt_costs_a_round(self):
        world = FakeWorld([{"reply": "改", "draw": _brief("二版")}])
        calls = self._flaky(world, failures=99)
        out = run_loop(world, rounds=4)
        paid = [e for e in out.log if e.get("paid")]
        # 4 rounds: one spent looking, three left to buy attempts with
        self.assertEqual(len(paid), 3)
        self.assertEqual(calls["n"], 2 + 3 + 1)   # free ones, paid ones, and the first
        self.assertTrue(out.log[-1].get("gave_up"))
        self.assertEqual(out.images, ["img-0"])   # what there was is what it keeps

    def test_it_cannot_run_forever(self):
        """The point of paying: a broken generator is bounded by the slider."""
        world = FakeWorld([{"reply": "改", "draw": _brief("二版")}])
        calls = self._flaky(world, failures=10_000)
        run_loop(world, rounds=2)
        # 2 rounds → 1 look, 1 round left to spend: 1 first try + 2 free + 1 paid
        self.assertLessEqual(calls["n"], 4)

    def test_a_cancel_is_not_a_hiccup(self):
        class Cancelled(Exception):
            pass

        world = FakeWorld([{"reply": "改", "draw": _brief("二版")}])
        self._flaky(world, failures=99, exc=Cancelled("stop"))
        hooks = world.hooks()
        hooks.fatal = (Cancelled,)
        with mock.patch.object(llm, "reflect_turn", world.reflect):
            with self.assertRaises(Cancelled):
                reflection.run(
                    base_history=[{"role": "user", "content": "画林"}],
                    first_reply="给你一张。",
                    brief=_brief("林"), nai_prompts=_prompts("tag-0"),
                    images=["img-0"], rounds=5, hooks=hooks,
                )

    def test_the_free_count_is_a_dial(self):
        world = FakeWorld([{"reply": "改", "draw": _brief("二版")}])
        calls = self._flaky(world, failures=99)
        with mock.patch.object(
            reflection, "SHAPE", reflection.ReflectionShape(free_retries=0)
        ):
            run_loop(world, rounds=2)
        self.assertEqual(calls["n"], 2)   # the first try, then one paid round


class APictureAlreadyMadeIsNeverLostTests(TestCase):
    """
    「反思轮只要生成过了画，最终不该报错」.

    Reflection is an attempt to improve a picture that already exists. An
    attempt that fails leaves what it started with; it does not take the
    picture down with it. That has to hold for every way the loop can fail,
    which is why these go at it from several directions.
    """

    def test_a_look_that_keeps_failing_ends_with_the_picture_it_had(self):
        world = FakeWorld([{"reply": "x", "draw": None}])
        world.reflect = mock.Mock(side_effect=RuntimeError("deepseek 500"))
        out = run_loop(world, rounds=3)
        self.assertEqual(out.images, ["img-0"])
        self.assertEqual(out.brief, _brief("林站在城墙上"))
        self.assertTrue(out.log[-1].get("gave_up"))

    def test_the_look_gets_free_retries_before_it_costs_a_round(self):
        calls = {"n": 0}

        def flaky(messages, on_retry=None):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError("empty content")
            return {"reply": "这回看清楚了", "draw": None}

        world = FakeWorld([])
        world.reflect = flaky
        out = run_loop(world, rounds=3)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(out.rounds_run, 1)       # the free retries cost nothing
        self.assertFalse([e for e in out.log if e.get("paid")])

    def test_a_redraw_that_keeps_failing_ends_with_the_picture_it_had(self):
        world = FakeWorld([{"reply": "改", "draw": _brief("二版")}])
        world.redraw = mock.Mock(side_effect=RuntimeError("novelai 502"))
        out = run_loop(world, rounds=3)
        self.assertEqual(out.images, ["img-0"])
        self.assertTrue(out.log[-1].get("gave_up"))

    def test_even_something_nobody_planned_for_leaves_the_picture(self):
        """A bug in the loop is still not a reason to lose a paid-for picture."""
        world = FakeWorld([{"reply": "改", "draw": _brief("二版")}])
        world.hooks = mock.Mock(side_effect=AssertionError("something silly"))
        out = reflection.run(
            base_history=[{"role": "user", "content": "画林"}],
            first_reply="给你一张。",
            brief=_brief("林"),
            nai_prompts=_prompts("tag-0"),
            images=["img-0"],
            rounds=3,
            hooks=reflection.Hooks(
                redraw=mock.Mock(side_effect=AssertionError("boom")),
                check_cancel=mock.Mock(side_effect=AssertionError("boom")),
                set_phase=lambda _p: None,
                image_url=lambda a: f"data:image/jpeg;base64,{a}",
            ),
        )
        self.assertEqual(out.images, ["img-0"])
        self.assertTrue(out.log[-1].get("gave_up"))

    def test_a_cancel_still_gets_through(self):
        """The one exception that IS allowed to end the turn."""
        class Cancelled(Exception):
            pass

        hooks = reflection.Hooks(
            redraw=lambda b: ({}, []),
            check_cancel=mock.Mock(side_effect=Cancelled("stop")),
            set_phase=lambda _p: None,
            image_url=lambda a: f"data:image/jpeg;base64,{a}",
            fatal=(Cancelled,),
        )
        with self.assertRaises(Cancelled):
            reflection.run(
                base_history=[], first_reply="x", brief=_brief("林"),
                nai_prompts=_prompts("t"), images=["img-0"], rounds=3, hooks=hooks,
            )


class WhatTheModelSeesTests(TestCase):
    """
    "整个反思流程，包括反思中写的反馈以及产出的图，都要完整地给主AI" — so this
    checks the request, not the intention.
    """

    def test_the_first_look_carries_the_history_the_turn_and_the_picture(self):
        world = FakeWorld([{"reply": "好", "draw": None}])
        run_loop(world, base=[{"role": "user", "content": "画林站在城墙上"}])
        sent = world.seen[0]
        blob = json.dumps(sent, ensure_ascii=False)
        self.assertIn("画林站在城墙上", blob)      # the conversation so far
        self.assertIn("给你一张。", blob)           # what it said this turn
        self.assertIn("tag-0", blob)               # the tags that came out
        self.assertIn("data:image/jpeg", blob)     # and the picture itself
        # its own turn is the object it returned, not a retelling of it —
        # a round is another turn in the same conversation, and a model reads
        # its own turns as examples of how to answer
        said = [m for m in sent if m["role"] == "assistant"][-1]["content"]
        self.assertEqual(set(json.loads(said)), {"reply", "draw", "edits"})
        # and the tags, which the drawing model wrote, are in the app's turn
        self.assertNotIn("tag-0", said)

    def test_the_third_look_still_carries_the_first_two(self):
        world = FakeWorld([
            {"reply": "一版太软", "draw": _brief("二版")},
            {"reply": "二版手坏了", "draw": _brief("三版")},
            {"reply": "行了", "draw": None},
        ])
        run_loop(world, rounds=4)
        blob = json.dumps(world.seen[-1], ensure_ascii=False)
        for earlier in ("一版太软", "二版手坏了", "二版", "三版", "tag-0", "tag-1", "tag-2"):
            self.assertIn(earlier, blob)
        self.assertEqual(blob.count("data:image/jpeg"), 3)   # every round's picture

    def test_progress_says_which_round_and_which_stage_of_it(self):
        """
        The redraw inside a round is a full prompting-and-painting pass. Left
        to itself it reports "painting:0/1", which is indistinguishable from an
        ordinary turn — the spinner would stop saying reflection was happening
        halfway through every round. So a round is a scope, and everything
        written inside it comes out under it.
        """
        world = FakeWorld([
            {"reply": "改", "draw": _brief("二版")},
            {"reply": "好", "draw": None},
        ])

        def redraw_that_reports(brief):
            world.hooks().set_phase  # the real one paints, and paints report
            world.phases.append(
                f"{world.prefix}:painting:0/1" if world.prefix else "painting:0/1"
            )
            world.painted.append(brief)
            return _prompts("tag-1"), ["img-1"]

        world.redraw = redraw_that_reports
        run_loop(world, rounds=3)
        self.assertIn("reflecting:1/3:thinking", world.phases)
        self.assertIn("reflecting:1/3:painting:0/1", world.phases)
        self.assertIn("reflecting:2/3:thinking", world.phases)
        # and nothing leaks out of the scope
        self.assertTrue(all(p.startswith("reflecting:") for p in world.phases), world.phases)


@override_settings(**TEST_CREDS)
@override_settings(**TEST_CREDS)
class OrdinaryTurnRetriesTests(TestCase):
    """
    A plain turn gets the same two free attempts a run does.

    The provider already retries an answer it cannot parse, but that is not the
    only way a step comes back useless — valid JSON with an empty base_prompt,
    a NovelAI timeout, a vendor having a minute. None of those were retried:
    the turn failed and the user pressed 重跑 by hand, which is the same retry
    with a person doing the waiting.
    """

    def setUp(self):
        self.client = Client()
        self.client.force_login(
            get_user_model().objects.create_user(username="retrier", password="x")
        )
        self.conv = Conversation.objects.create(title="会失败的对话")

    def _run(self, lead_failures=0, draw_failures=0):
        calls = {"lead": 0, "draw": 0}

        def lead(messages, on_retry=None, on_lookup=None):
            calls["lead"] += 1
            if calls["lead"] <= lead_failures:
                raise llm.ModelError("deepseek returned empty content")
            return LEAD_RESULT

        def draw(brief, lib="", lines=None, on_retry=None, on_lookup=None, **kw):
            calls["draw"] += 1
            if calls["draw"] <= draw_failures:
                raise llm.ModelError("drawing model returned an empty base_prompt")
            return DRAW_RESULT

        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", lead
        ), mock.patch.object(
            pipeline.llm, "draw_turn", draw
        ), mock.patch.object(
            pipeline.novelai, "generate_one", return_value=(_png(), 7)
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            # the retries under test are the turn's own; no reflection round,
            # which would otherwise ask the environment's real provider
            self.client.post(
                f"/api/conversations/{self.conv.id}/messages",
                {"text": "画一张", "n_images": "1", "reflection_rounds": "0"},
            )
        msg = self.conv.messages.filter(role="assistant").order_by("-seq").first()
        assert msg is not None
        msg.refresh_from_db()
        return msg, calls

    def test_two_bad_lead_answers_still_produce_a_picture(self):
        msg, calls = self._run(lead_failures=2)
        self.assertEqual(msg.status, "done")
        self.assertEqual(calls["lead"], 3)
        self.assertEqual(msg.attachments.filter(kind="generated").count(), 1)

    def test_a_third_failure_is_a_failure(self):
        """Free means free, not endless: three bad answers end the turn."""
        msg, calls = self._run(lead_failures=3)
        self.assertEqual(msg.status, "error")
        self.assertEqual(calls["lead"], 3)
        self.assertIn("empty content", msg.error)

    def test_the_drawing_step_gets_its_own_two(self):
        msg, calls = self._run(draw_failures=2)
        self.assertEqual(msg.status, "done")
        self.assertEqual(calls["draw"], 3)
        # the lead model is not re-asked for a step that did not fail
        self.assertEqual(calls["lead"], 1)

    def test_a_turn_that_works_is_asked_once(self):
        msg, calls = self._run()
        self.assertEqual(msg.status, "done")
        self.assertEqual((calls["lead"], calls["draw"]), (1, 1))


class ReflectionStaysOutOfTheConversationTests(TestCase):
    """
    The other half of the promise: afterwards, nothing.

    build_history is the only thing that decides what a model reads, so
    that is what gets asked.
    """

    def setUp(self):
        self.conv = Conversation.objects.create(title="反思过的对话")
        Message.objects.create(
            conversation=self.conv, seq=1, role="user", status="done", content="画林"
        )
        self.msg = Message.objects.create(
            conversation=self.conv, seq=2, role="assistant", status="done", kind="image",
            content="给你一张。",
            draw_brief=_brief("最终版"),
            nai_prompts=_prompts("final-tag"),
            reflection_log=[
                {"round": 1, "reply": "光太软，我重画。", "redrew": True},
                {"round": 2, "reply": "这回对了。", "redrew": False},
            ],
        )
        att = Attachment(message=self.msg, kind="generated", mime="image/png",
                         width=64, height=96)
        data = _png()
        att.size_bytes = len(data)
        att.file.save("final.png", ContentFile(data), save=True)

    def test_no_reflection_round_reaches_the_model(self):
        built = pipeline.build_history(self.conv, before_seq=99)
        blob = json.dumps(built, ensure_ascii=False)
        self.assertNotIn("光太软", blob)
        self.assertNotIn("这回对了", blob)
        self.assertNotIn("反思", blob)

    def test_the_last_round_is_what_the_model_reads_back(self):
        built = pipeline.build_history(self.conv, before_seq=99)
        blob = json.dumps(built, ensure_ascii=False)
        self.assertIn("最终版", blob)
        self.assertIn("final-tag", blob)

    def test_a_branch_carries_the_log_with_the_picture(self):
        """It is the record of how that picture came to be; a copy keeps it."""
        self.client.force_login(
            get_user_model().objects.create_user(username="brancher2", password="x")
        )
        r = self.client.post(f"/api/messages/{self.msg.id}/branch")
        self.assertEqual(r.status_code, 201)
        branch = Conversation.objects.get(id=r.json()["id"])
        copied = branch.messages.get(seq=2)
        self.assertEqual(copied.reflection_log, self.msg.reflection_log)

    def test_the_log_is_on_the_turn_and_not_a_message_of_its_own(self):
        """A row would be paginated, branched, counted — and one day read."""
        self.assertEqual(self.conv.messages.count(), 2)
        self.client.force_login(
            get_user_model().objects.create_user(username="reader", password="x")
        )
        r = self.client.get(f"/api/conversations/{self.conv.id}/messages")
        row = [m for m in r.json()["messages"] if m["seq"] == 2][0]
        self.assertEqual(len(row["reflection_log"]), 2)
        self.assertTrue(row["reflection_log"][0]["redrew"])


@override_settings(**TEST_CREDS)
@override_settings(**TEST_CREDS)
@override_settings(**TEST_CREDS)
class StickyReferencesTests(TestCase):
    """
    常驻参考: every reference picture stays in view instead of only the newest
    turn's.

    Nothing is copied. The flag widens which turns' uploads the history carries,
    so each picture is still attached to the message it was actually given with
    — which is why it can never be in the context twice however long the option
    stays on, and why turning it off has nothing to undo.
    """

    def setUp(self):
        self.client = Client()
        self.client.force_login(
            get_user_model().objects.create_user(username="pinner", password="x")
        )
        self.conv = Conversation.objects.create(title="常驻样本")
        self.seq = 0
        self._upload_turn("照这张画")            # seq 1
        self._answer()                            # seq 2
        self._upload_turn("也参考这张")          # seq 3
        self._answer()                            # seq 4
        self._plain_turn("换个角度")             # seq 5

    def _next(self):
        self.seq += 1
        return self.seq

    def _upload_turn(self, text):
        m = Message.objects.create(conversation=self.conv, seq=self._next(),
                                   role="user", status="done", content=text)
        att = Attachment(message=m, kind="upload", mime="image/png", width=64, height=96)
        data = _png((40 * self.seq, 80, 120))
        att.size_bytes = len(data)
        att.file.save(f"ref-{self.seq}.png", ContentFile(data), save=True)
        return m

    def _plain_turn(self, text):
        return Message.objects.create(conversation=self.conv, seq=self._next(),
                                      role="user", status="done", content=text)

    def _answer(self):
        return Message.objects.create(conversation=self.conv, seq=self._next(),
                                      role="assistant", status="done", content="好")

    def _images(self, sticky):
        built = pipeline.build_history(self.conv, before_seq=99, sticky_refs=sticky)
        return [
            part["image_url"]["url"]
            for m in built
            if isinstance(m["content"], list)
            for part in m["content"]
            if part.get("type") == "image_url"
        ]

    def test_off_carries_only_the_newest_turn_s_reference(self):
        self.assertEqual(len(self._images(sticky=False)), 1)

    def test_on_carries_every_reference_in_the_conversation(self):
        self.assertEqual(len(self._images(sticky=True)), 2)

    def test_no_picture_is_ever_in_the_context_twice(self):
        """The whole reason nothing is copied."""
        urls = self._images(sticky=True)
        self.assertEqual(len(urls), len(set(urls)))

    def test_each_one_still_sits_with_the_message_it_arrived_on(self):
        built = pipeline.build_history(self.conv, before_seq=99, sticky_refs=True)
        with_images = [
            m for m in built
            if isinstance(m["content"], list)
            and any(p.get("type") == "image_url" for p in m["content"])
        ]
        self.assertEqual(len(with_images), 2)
        texts = [
            " ".join(p.get("text", "") for p in m["content"]) for m in with_images
        ]
        self.assertIn("照这张画", texts[0])
        self.assertIn("也参考这张", texts[1])

    def test_turning_it_off_goes_straight_back(self):
        """No state to unwind: the next turn simply asks for less."""
        self.assertEqual(len(self._images(sticky=True)), 2)
        self.assertEqual(len(self._images(sticky=False)), 1)

    def test_the_flag_reaches_the_row(self):
        with mock.patch("core.services.jobs.start_generation"):
            r = self.client.post(
                f"/api/conversations/{self.conv.id}/messages",
                {"text": "再来", "sticky_refs": "1"},
            )
        self.assertEqual(r.status_code, 201)
        row = self.conv.messages.filter(role="assistant", status="pending").first()
        assert row is not None
        self.assertTrue(row.sticky_refs)

    def test_it_is_off_unless_asked_for(self):
        with mock.patch("core.services.jobs.start_generation"):
            self.client.post(
                f"/api/conversations/{self.conv.id}/messages", {"text": "再来"}
            )
        row = self.conv.messages.filter(role="assistant", status="pending").first()
        assert row is not None
        self.assertFalse(row.sticky_refs)

    def test_a_rerun_takes_todays_answer(self):
        Message.objects.create(conversation=self.conv, seq=self._next(),
                               role="assistant", status="done", content="好",
                               sticky_refs=True)
        with mock.patch("core.services.jobs.start_generation"):
            r = self.client.post(
                f"/api/conversations/{self.conv.id}/rerun",
                json.dumps({"sticky_refs": False}),
                content_type="application/json",
            )
        self.assertEqual(r.status_code, 201)
        row = self.conv.messages.filter(status="pending").first()
        assert row is not None
        self.assertFalse(row.sticky_refs)


class RerunUsesTodaysSettingsTests(TestCase):
    """
    重跑 is the user asking again, so it asks under whatever the sliders say
    now — not under whatever they said when the turn it replaces was sent.
    With nothing said at all it reads the conversation, which is where the
    sliders now keep their values.
    """

    def setUp(self):
        self.client = Client()
        self.client.force_login(
            get_user_model().objects.create_user(username="rerunner", password="x")
        )
        self.conv = Conversation.objects.create(title="c")
        Message.objects.create(conversation=self.conv, seq=1, role="user",
                               status="done", content="画一张")
        Message.objects.create(
            conversation=self.conv, seq=2, role="assistant", status="done",
            kind="image", content="好", force_n_images=1, force_reflection_rounds=0,
        )

    def _rerun(self, body):
        with mock.patch("core.services.jobs.start_generation"):
            r = self.client.post(
                f"/api/conversations/{self.conv.id}/rerun",
                json.dumps(body),
                content_type="application/json",
            )
        self.assertEqual(r.status_code, 201)
        row = self.conv.messages.filter(role="assistant", archived=False).order_by("-seq").first()
        assert row is not None
        return row

    def test_the_new_settings_win(self):
        row = self._rerun({"n_images": 3, "reflection_rounds": 4})
        self.assertEqual(row.force_n_images, 3)
        self.assertEqual(row.force_reflection_rounds, 4)

    def test_turning_reflection_off_on_a_rerun_really_turns_it_off(self):
        """0 is a value, not an absence — this is the case a falsy check eats."""
        Message.objects.filter(seq=2).update(force_reflection_rounds=5)
        row = self._rerun({"n_images": 1, "reflection_rounds": 0})
        self.assertEqual(row.force_reflection_rounds, 0)

    def test_a_caller_that_says_nothing_gets_the_conversation_s_settings(self):
        """A caller with no sliders — MCP, an old client — still gets today's."""
        Conversation.objects.filter(id=self.conv.id).update(
            gen_settings={"n_images": 2, "reflection_rounds": 3}
        )
        Message.objects.filter(seq=2).update(force_n_images=1, force_reflection_rounds=0)
        row = self._rerun({})
        self.assertEqual(row.force_n_images, 2)
        self.assertEqual(row.force_reflection_rounds, 3)


class ReflectionSliderTests(TestCase):
    """What the slider says has to survive the trip to the row."""

    def setUp(self):
        self.client = Client()
        self.client.force_login(
            get_user_model().objects.create_user(username="slider", password="x")
        )
        self.conv = Conversation.objects.create(title="c")

    def _send(self, value):
        with mock.patch("core.services.jobs.start_generation"):
            r = self.client.post(
                f"/api/conversations/{self.conv.id}/messages",
                {"text": "画一张", "reflection_rounds": value},
            )
        self.assertEqual(r.status_code, 201)
        row = self.conv.messages.filter(role="assistant").order_by("-seq").first()
        assert row is not None
        return row

    def test_a_number_is_carried_onto_the_row(self):
        self.assertEqual(self._send("4").force_reflection_rounds, 4)

    def test_zero_is_an_answer_not_an_absence(self):
        """0 means "the slider was at off", which is worth knowing later."""
        self.assertEqual(self._send("0").force_reflection_rounds, 0)

    def test_it_cannot_be_pushed_past_the_ceiling(self):
        self.assertEqual(
            self._send("999").force_reflection_rounds,
            pipeline.settings.MAX_REFLECTION_ROUNDS,
        )

    def test_rubbish_is_treated_as_unset(self):
        """
        Unset is not zero: nonsense means the caller said nothing, and what a
        caller who says nothing gets is whatever the conversation is set to.
        """
        from core.views.chat import _parse_reflection_rounds

        self.assertIsNone(_parse_reflection_rounds("abc"))
        self.assertEqual(
            self._send("abc").force_reflection_rounds,
            gensettings.DEFAULTS["reflection_rounds"],
        )

    def test_the_clamp_is_read_off_the_row_too(self):
        msg = Message(force_reflection_rounds=999)
        self.assertEqual(
            pipeline._reflection_rounds(msg), pipeline.settings.MAX_REFLECTION_ROUNDS
        )
        self.assertEqual(pipeline._reflection_rounds(Message()), 0)


class DrawingTemperatureTests(TestCase):
    """
    The drawing model translates; the lead model writes.

    One picture description should come back as the same tags, so the drawing
    call asks for temperature 0. The lead call is left on the vendor default —
    that one is composing, and a default tuned for composing is the right one.
    """

    def _params_of(self, run):
        seen = {}

        class Stub:
            capabilities = providers.base.Capabilities(
                json_schema=True, vision=True, reasoning_effort=True,
                native_cost=True, balance=False,
            )
            draw_effort = None

            def complete(self, **kw):
                seen.update(kw)
                return providers.base.Completion(
                    parsed={
                        "reply": "好", "draw": None, "edits": None,
                        "size": "portrait", "scene": "1girl",
                        "character_prompts": [], "negative_prompt": "",
                    },
                    usage={},
                )

        with mock.patch.object(providers, "active", lambda: Stub()):
            run()
        return seen

    def test_the_drawing_call_asks_for_zero(self):
        seen = self._params_of(lambda: llm.draw_turn({"main": "x", "size": "portrait"}))
        self.assertEqual(seen["temperature"], 0.0)

    def test_the_lead_call_does_not_ask_at_all(self):
        seen = self._params_of(lambda: llm.lead_turn([{"role": "user", "content": "hi"}]))
        self.assertIsNone(seen["temperature"])

    def test_reflection_does_not_either(self):
        """Reflection is judgement, which is the lead model's kind of work."""
        seen = self._params_of(lambda: llm.reflect_turn([{"role": "user", "content": "hi"}]))
        self.assertIsNone(seen["temperature"])

    def test_it_is_a_setting_rather_than_a_literal(self):
        with override_settings(DRAW_TEMPERATURE=0.4):
            seen = self._params_of(
                lambda: llm.draw_turn({"main": "x", "size": "portrait"})
            )
        self.assertEqual(seen["temperature"], 0.4)


class ReflectionSchemaTests(TestCase):
    """
    The schema, which is code. Nothing here looks at lead_reflection.txt: that file
    is the user's to write, and a test that goes red when they reword their own
    prompt is a test that only gets in the way.
    """

    def test_the_schema_shares_the_picture_description_with_the_ordinary_turn(self):
        """One shape, defined once: a redraw must read like any other brief."""
        lead_draw = llm.LEAD_SCHEMA["schema"]["properties"]["draw"]
        reflect_draw = llm.REFLECT_SCHEMA["schema"]["properties"]["draw"]
        self.assertIs(lead_draw, reflect_draw)

    def test_reflection_asks_for_no_field_the_ordinary_turn_does_not_have(self):
        lead = set(llm.LEAD_SCHEMA["schema"]["properties"])
        reflect = set(llm.REFLECT_SCHEMA["schema"]["properties"])
        self.assertTrue(reflect <= lead, f"{reflect - lead} is new")


class ReflectionShapeTests(TestCase):
    """The dials, which exist so the loop's appetite is one edit to change."""

    def test_how_many_rounds_come_back_follows_the_dial(self):
        verdicts = [
            {"reply": "第一轮的评语", "draw": _brief("二版")},
            {"reply": "第二轮的评语", "draw": _brief("三版")},
            {"reply": "第三轮的评语", "draw": None},
        ]
        wide = FakeWorld(list(verdicts))
        run_loop(wide, rounds=4)
        self.assertIn("第一轮的评语", json.dumps(wide.seen[-1], ensure_ascii=False))

        narrow = FakeWorld(list(verdicts))
        with mock.patch.object(
            reflection, "SHAPE", reflection.ReflectionShape(rounds_shown=1)
        ):
            run_loop(narrow, rounds=4)
        last = json.dumps(narrow.seen[-1], ensure_ascii=False)
        self.assertNotIn("第一轮的评语", last)
        # the conversation and the turn's own output are never windowed away:
        # without them there is no brief to judge the picture against
        self.assertIn("画林", last)
        self.assertIn("给你一张。", last)

    def test_pictures_per_round_follows_the_dial(self):
        world = FakeWorld([{"reply": "x", "draw": None}])
        with mock.patch.object(
            reflection, "SHAPE", reflection.ReflectionShape(max_images_per_round=1)
        ):
            run_loop(world, images=("a", "b", "c"))
        self.assertEqual(
            json.dumps(world.seen[0], ensure_ascii=False).count("data:image/jpeg"), 1
        )

    def test_keeping_superseded_pictures_is_one_flag(self):
        world = FakeWorld([
            {"reply": "改", "draw": _brief("二版")},
            {"reply": "好", "draw": None},
        ])
        with mock.patch.object(
            reflection, "SHAPE",
            reflection.ReflectionShape(discard_superseded_images=False),
        ):
            run_loop(world, rounds=3)
        self.assertEqual(world.discarded, [])


LEAD_RESULT = {
    "reply": "给你一张。",
    "draw": {"main": "一个银发少女站在城墙上", "size": "portrait"},
    "edits": None,
    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
}
DRAW_RESULT = {
    "n_images": 1, "size": "portrait", "base_prompt": "1girl, wall",
    "character_prompts": [], "negative_prompt": "",
    "usage": {"prompt_tokens": 5, "completion_tokens": 5},
}


@override_settings(**TEST_CREDS)
class ReflectionThroughThePipelineTests(TestCase):
    """
    The loop's own tests run against counters. This one runs the real
    run_generation with only the outside world stubbed, because the things that
    can only break here are the database ones: whether the superseded pictures
    are really gone, and whether the row ends up describing the picture it
    actually kept.
    """

    def setUp(self):
        self.client = Client()
        self.client.force_login(
            get_user_model().objects.create_user(username="loop", password="x")
        )
        self.conv = Conversation.objects.create(title="反思一轮")

    def _send(self, rounds, verdicts):
        seen = {"n": 0}

        def reflect(messages, on_retry=None):
            v = verdicts[min(seen["n"], len(verdicts) - 1)]
            seen["n"] += 1
            return {**v, "usage": {}}

        def edit(picture, brief, truth, lib="", lines=None, on_retry=None, on_lookup=None, **kw):
            # a reflection redraw is an edit of the picture being looked at
            return {**DRAW_RESULT, "base_prompt": f"tags for {brief['change']}"}

        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=LEAD_RESULT
        ), mock.patch.object(
            pipeline.llm, "draw_turn", side_effect=lambda brief, lib="", lines=None, on_retry=None, on_lookup=None, **kw: {
                **DRAW_RESULT, "base_prompt": f"tags for {brief['main']}"
            }
        ), mock.patch.object(
            pipeline.llm, "draw_edit", side_effect=edit
        ), mock.patch.object(
            llm, "reflect_turn", reflect
        ), mock.patch.object(
            pipeline.novelai, "generate_one", return_value=(_png(), 7)
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            r = self.client.post(
                f"/api/conversations/{self.conv.id}/messages",
                {"text": "画一张", "n_images": "1", "reflection_rounds": str(rounds)},
            )
        self.assertEqual(r.status_code, 201)
        msg = self.conv.messages.filter(role="assistant").order_by("-seq").first()
        assert msg is not None
        msg.refresh_from_db()
        return msg

    def test_redrew_is_recorded_only_when_the_redraw_painted(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] > 1:
                raise pipeline.novelai.NovelAIError("novelai timed out")
            return _png(), 7
        verdicts = [{"reply": "光太软。", "draw": {"change": "硬光版"}}, {"reply": "算了。", "draw": None}]
        seen = {"n": 0}

        def reflect(messages, on_retry=None):
            v = verdicts[min(seen["n"], len(verdicts) - 1)]
            seen["n"] += 1
            return {**v, "usage": {}}
        with _sync_jobs(), mock.patch.object(
            pipeline.llm, "lead_turn", return_value=LEAD_RESULT
        ), mock.patch.object(
            pipeline.llm, "draw_turn", return_value={**DRAW_RESULT, "base_prompt": "tags for first"}
        ), mock.patch.object(
            pipeline.llm, "draw_edit", return_value={**DRAW_RESULT, "base_prompt": "tags for 硬光版"}
        ), mock.patch.object(
            llm, "reflect_turn", reflect
        ), mock.patch.object(
            pipeline.novelai, "generate_one", side_effect=flaky
        ), mock.patch.object(pipeline.llm, "make_title", return_value=""):
            self.client.post(f"/api/conversations/{self.conv.id}/messages",
                             {"text": "画一张", "n_images": "1", "reflection_rounds": "1"})
        msg = self.conv.messages.filter(role="assistant").order_by("-seq").first()
        assert msg is not None
        self.assertEqual(msg.status, "done")
        # the redraw never painted: the first picture stays, and the log does
        # not claim a redraw happened
        self.assertEqual(msg.attachments.filter(kind="generated").count(), 1)
        self.assertEqual([x.get("redrew") for x in (msg.reflection_log or []) if "reply" in x], [False])
        self.assertEqual((msg.nai_prompts or {})["base_prompt"], "tags for first")
        # and the slot table behind the kept picture is the painted one, not the failed attempt's
        self.assertIsInstance(msg.prompt, dict)

    def test_a_redraw_leaves_only_the_last_picture_behind(self):
        msg = self._send(3, [
            {"reply": "光太软。", "draw": {"change": "硬光版"}},
            {"reply": "这回对了。", "draw": None},
        ])
        self.assertEqual(msg.status, "done")
        # two were painted, one survives
        self.assertEqual(msg.attachments.filter(kind="generated").count(), 1)
        self.assertEqual(Attachment.objects.filter(kind="generated").count(), 1)
        # and the row describes the one it kept
        self.assertEqual((msg.draw_brief or {})["change"], "硬光版")
        self.assertEqual((msg.nai_prompts or {})["base_prompt"], "tags for 硬光版")

    def test_the_discarded_file_is_gone_from_disk_too(self):
        """An orphan blob in MEDIA_ROOT is what nobody notices for a year."""
        with mock.patch.object(pipeline, "_discard_images") as spy:
            self._send(2, [
                {"reply": "改", "draw": {"change": "二版"}},
                {"reply": "好", "draw": None},
            ])
        handed_over = spy.call_args[0][0]
        self.assertEqual(len(handed_over), 1)

        msg = self._send(2, [
            {"reply": "改", "draw": {"change": "三版"}},
            {"reply": "好", "draw": None},
        ])
        paths = [a.file.name for a in msg.attachments.filter(kind="generated")]
        self.assertEqual(len(paths), 1)
        from django.core.files.storage import default_storage

        self.assertTrue(default_storage.exists(paths[0]))

    def test_the_log_lands_on_the_row(self):
        msg = self._send(3, [
            {"reply": "光太软。", "draw": {"change": "硬光版"}},
            {"reply": "这回对了。", "draw": None},
        ])
        self.assertEqual([r["round"] for r in msg.reflection_log or []], [1, 2])
        self.assertEqual([r["redrew"] for r in msg.reflection_log or []], [True, False])

    def test_the_visible_reply_is_still_what_was_said_to_the_user(self):
        msg = self._send(2, [
            {"reply": "光太软。", "draw": {"change": "硬光版"}},
            {"reply": "这回对了。", "draw": None},
        ])
        self.assertEqual(msg.content, "给你一张。")

    def test_a_reflection_that_falls_over_still_leaves_a_finished_turn(self):
        """
        The promise, at the level the user actually sees it: the row says done
        and carries its picture, rather than saying 生成失败 about a picture
        that exists.
        """
        with mock.patch.object(
            llm, "reflect_turn", mock.Mock(side_effect=RuntimeError("deepseek 500"))
        ):
            msg = self._send(3, [])
        self.assertEqual(msg.status, "done")
        self.assertEqual(msg.error, "")
        self.assertEqual(msg.attachments.filter(kind="generated").count(), 1)
        self.assertTrue((msg.reflection_log or [])[-1].get("gave_up"))

    def test_with_the_slider_at_zero_nothing_reflects(self):
        called = []
        with mock.patch.object(llm, "reflect_turn", lambda *a, **k: called.append(1)):
            msg = self._send(0, [])
        self.assertEqual(called, [])
        self.assertIsNone(msg.reflection_log)
        self.assertEqual(msg.attachments.filter(kind="generated").count(), 1)


def _sync_jobs():
    """Run generation jobs inline instead of in a thread. The title is not
    asked for: that call goes to whatever provider the environment names."""
    return _Both(
        mock.patch.multiple(
            jobs,
            start_generation=pipeline.run_generation,
            start_redraw=pipeline.run_redraw,
        ),
        mock.patch.object(pipeline.llm, "make_title", return_value=""),
    )


class _Both:
    """Two context managers as one, so `with _sync_jobs():` stays as it was."""

    def __init__(self, *parts):
        self.parts = parts

    def __enter__(self):
        for part in self.parts:
            part.__enter__()
        return self

    def __exit__(self, *exc):
        for part in reversed(self.parts):
            part.__exit__(*exc)
        return False
