"""
自动修改: the app approving the model's library edits on the user's behalf.

The feature is one optional judgement — may the app press approve? — and the
tests are shaped around the two promises that make it safe rather than around
the three modes:

  * it either presses the same button the user would, or does nothing at all;
  * nothing downstream can tell which of the two happened.

A test per mode would pass just as well if the automatic path had grown its
own way of writing to the library, which is the failure that would actually
hurt.
"""

from __future__ import annotations

import json

from django.test import TestCase, override_settings

from core import ui_text
from core.models import (
    Character,
    CharacterVariant,
    Conversation,
    Folder,
    LibraryProposal,
    Message,
    Setting,
)
from core.services import gensettings, pipeline, proposals

from .tests import _project

TEST_CREDS = {"APP_USERNAME": "yyy", "APP_PASSWORD": "pw"}


class AutoLibraryTests(TestCase):
    def setUp(self):
        self.folder = Folder.objects.create(name="项目")
        Setting.objects.create(folder=self.folder, title="黄昏之城", content="旧的内容")
        ch = Character.objects.create(folder=self.folder, name="莉拉", description="旧的")
        CharacterVariant.objects.create(character=ch, title="旅装", content="旧的")
        self.conv = Conversation.objects.create(title="对话", folder=self.folder)
        self.msg = Message.objects.create(
            conversation=self.conv, seq=1, role="assistant", status="done", content=""
        )

    def _propose(self, edits: dict) -> LibraryProposal:
        return LibraryProposal.objects.create(
            conversation=self.conv, message=self.msg, folder=self.folder,
            payload=_project(edits),
        )

    EDIT_ONLY = _project({"settings": [{"title": "黄昏之城", "content": "新的内容"}],
                          "characters": []})
    HAS_NEW = _project({"settings": [{"title": "黄昏之城", "content": "新的内容"},
                                     {"title": "从没有过的设定", "content": "x"}],
                        "characters": []})

    # ------------------------------------------------------------ the promises

    def test_it_presses_the_same_button_the_user_would(self):
        """
        Not "it writes the same thing" — the same function. A second way to
        write a proposal into a library is the bug this is shaped to prevent,
        and it would pass every behavioural test on the day it was written.
        """
        import inspect

        src = inspect.getsource(proposals.auto_approve)
        self.assertIn("resolve(", src)
        self.assertNotIn("Setting.objects.create", src)
        self.assertNotIn("Character.objects.create", src)

    def test_what_it_leaves_behind_is_indistinguishable(self):
        """
        Downstream must not be able to tell. Two identical proposals, one
        settled by hand and one automatically, have to end up the same.
        """
        by_hand = self._propose(self.EDIT_ONLY)
        proposals.resolve(by_hand.id, approve=True)
        by_hand.refresh_from_db()

        Setting.objects.filter(title="黄昏之城").update(content="旧的内容")
        by_app = self._propose(self.EDIT_ONLY)
        proposals.auto_approve(by_app, "all")
        by_app.refresh_from_db()

        self.assertEqual(by_hand.status, by_app.status)
        self.assertEqual(by_hand.applied_summary, by_app.applied_summary)

    def test_when_it_will_not_act_the_proposal_is_exactly_as_it_was(self):
        """
        Doing nothing is the ordinary outcome, not a failure — the proposal
        stays pending, on the path that existed before this feature.
        """
        p = self._propose(self.HAS_NEW)
        self.assertFalse(proposals.auto_approve(p, "edits"))
        p.refresh_from_db()
        self.assertEqual(p.status, "pending")
        self.assertIsNone(p.resolved_at)
        self.assertFalse(Setting.objects.filter(title="从没有过的设定").exists())
        self.assertEqual(Setting.objects.get(title="黄昏之城").content, "旧的内容")

    # ------------------------------------------------------------ the judgement

    def test_off_never_acts(self):
        p = self._propose(self.EDIT_ONLY)
        self.assertFalse(proposals.auto_approve(p, "off"))
        p.refresh_from_db()
        self.assertEqual(p.status, "pending")

    def test_edits_only_takes_a_change_to_something_that_exists(self):
        p = self._propose(self.EDIT_ONLY)
        self.assertTrue(proposals.auto_approve(p, "edits"))
        self.assertEqual(Setting.objects.get(title="黄昏之城").content, "新的内容")

    def test_edits_only_refuses_the_whole_proposal_when_any_of_it_is_new(self):
        """
        The whole thing waits, not the new parts. Splitting it would mean the
        user approves a leftover they never saw whole.
        """
        p = self._propose(self.HAS_NEW)
        self.assertFalse(proposals.auto_approve(p, "edits"))
        self.assertEqual(Setting.objects.get(title="黄昏之城").content, "旧的内容")

    def test_all_takes_it(self):
        p = self._propose(self.HAS_NEW)
        self.assertTrue(proposals.auto_approve(p, "all"))
        self.assertTrue(Setting.objects.filter(title="从没有过的设定").exists())

    def test_edits_only_may_put_a_new_costume_on_a_character_that_exists(self):
        """
        A costume is a detail of an entry that is already there, and someone
        already decided that entry belongs. It cannot become the
        duplicate-under-another-name that waiting for a person prevents.
        """
        p = self._propose({
            "settings": [],
            "characters": [{"name": "莉拉", "variants": [
                {"title": "新的变体", "content": "x"}]}],
        })
        self.assertTrue(proposals.auto_approve(p, "edits"))
        self.assertTrue(CharacterVariant.objects.filter(title="新的变体").exists())

    def test_edits_only_still_refuses_a_costume_on_a_character_that_is_not(self):
        """The character itself is the new entry; its costume comes with it."""
        p = self._propose({
            "settings": [],
            "characters": [{"name": "没有这个人", "variants": [
                {"title": "常服", "content": "x"}]}],
        })
        self.assertFalse(proposals.auto_approve(p, "edits"))
        self.assertFalse(Character.objects.filter(name="没有这个人").exists())

    def test_an_entry_inherited_from_a_parent_folder_is_not_new(self):
        """
        「已有」 means anywhere the conversation can see, not just its own
        folder — matching only the folder is how an update becomes a duplicate
        one level down.
        """
        child = Folder.objects.create(name="子项目", parent=self.folder)
        conv = Conversation.objects.create(title="子对话", folder=child)
        msg = Message.objects.create(conversation=conv, seq=1, role="assistant",
                                     status="done", content="")
        p = LibraryProposal.objects.create(
            conversation=conv, message=msg, folder=child, payload=self.EDIT_ONLY
        )
        self.assertFalse(proposals.creates_an_entry(p))

    def test_a_conversation_in_no_folder_is_never_acted_on(self):
        loose = Conversation.objects.create(title="没有文件夹")
        msg = Message.objects.create(conversation=loose, seq=1, role="assistant",
                                     status="done", content="")
        p = LibraryProposal.objects.create(
            conversation=loose, message=msg, folder=None, payload=self.EDIT_ONLY
        )
        self.assertFalse(proposals.auto_approve(p, "all"))
        p.refresh_from_db()
        self.assertEqual(p.status, "pending")

    def test_a_mode_nobody_defined_does_nothing(self):
        p = self._propose(self.EDIT_ONLY)
        self.assertFalse(proposals.auto_approve(p, "somethingelse"))
        p.refresh_from_db()
        self.assertEqual(p.status, "pending")

    # ------------------------------------------------------------ the setting

    def test_a_conversation_from_before_this_existed_is_off(self):
        old = Conversation.objects.create(title="老对话", gen_settings=None)
        self.assertEqual(gensettings.of(old)["auto_library"], "off")

    def test_the_setting_follows_the_conversation(self):
        gensettings.merge(self.conv, {"auto_library": "all"})
        other = Conversation.objects.create(title="另一个", folder=self.folder)
        self.assertEqual(gensettings.of(self.conv)["auto_library"], "all")
        self.assertEqual(gensettings.of(other)["auto_library"], "off")

    def test_a_mode_that_is_not_one_of_the_three_reads_back_as_off(self):
        gensettings.merge(self.conv, {"auto_library": "yes please"})
        self.assertEqual(gensettings.of(self.conv)["auto_library"], "off")

    def test_the_names_the_app_offers_are_the_ones_the_service_knows(self):
        self.assertEqual(set(gensettings.AUTO_LIBRARY_MODES), set(proposals.MODES))
        self.assertIn(gensettings.DEFAULTS["auto_library"], proposals.MODES)

    # ------------------------------------------------------------ end to end

    def test_a_turn_with_the_setting_on_leaves_nothing_to_approve(self):
        gensettings.merge(self.conv, {"auto_library": "edits"})
        self.conv.refresh_from_db()
        pipeline._record_proposal(
            self.conv, self.msg, _project({"settings": [{"title": "黄昏之城", "content": "新的"}]})
        )
        p = LibraryProposal.objects.filter(conversation=self.conv).first()
        assert p is not None
        self.assertEqual(p.status, "applied")
        self.assertEqual(Setting.objects.get(title="黄昏之城").content, "新的")

    def test_a_turn_with_it_off_leaves_the_proposal_waiting(self):
        pipeline._record_proposal(
            self.conv, self.msg, _project({"settings": [{"title": "黄昏之城", "content": "新的"}]})
        )
        p = LibraryProposal.objects.filter(conversation=self.conv).first()
        assert p is not None
        self.assertEqual(p.status, "pending")
        self.assertEqual(Setting.objects.get(title="黄昏之城").content, "旧的内容")

    def test_the_proposal_is_written_down_before_anything_is_decided(self):
        """
        Order matters: parked first, approved second. A failure in the
        judgement can then only cost a click, never the edit itself.
        """
        import inspect

        from core.services import proposals

        src = inspect.getsource(pipeline._record_proposal)
        self.assertLess(src.index("proposals.record"), src.index("auto_approve"))
        # and inside record(): the row exists before any layer is written
        src = inspect.getsource(proposals.record)
        self.assertLess(src.index("LibraryProposal.objects.create"), src.index("apply_layer"))


class WhatIsCreatedUnderAllIsInPlayTests(TestCase):
    """
    An entry written into the library and left switched off is one the model
    cannot see on the next turn — a strange thing to do with something it just
    asked to write down. Under 「全部」 the app is doing this unattended, so the
    new entry starts in play.

    Only what is created. An entry that was merely updated is already in the
    library, and whether it is in play was the user's decision, made earlier.
    """

    def setUp(self):
        self.folder = Folder.objects.create(name="项目")
        self.old_setting = Setting.objects.create(
            folder=self.folder, title="已有设定", content="旧")
        self.ch = Character.objects.create(
            folder=self.folder, name="已有角色", description="旧")
        self.old_variant = CharacterVariant.objects.create(
            character=self.ch, title="已有变体", content="旧")
        self.conv = Conversation.objects.create(title="对话", folder=self.folder)
        self.msg = Message.objects.create(conversation=self.conv, seq=1,
                                          role="assistant", status="done", content="")

    def _p(self, payload):
        return LibraryProposal.objects.create(
            conversation=self.conv, message=self.msg, folder=self.folder,
            payload=_project(payload),
        )

    def _chosen(self):
        return (
            set(self.conv.selected_settings.values_list("title", flat=True)),
            set(self.conv.selected_variants.values_list("title", flat=True)),
        )

    def test_a_new_setting_is_switched_on(self):
        proposals.auto_approve(self._p(
            {"settings": [{"title": "新设定", "content": "x"}]}), "all")
        settings, _ = self._chosen()
        self.assertIn("新设定", settings)

    def test_a_new_costume_is_switched_on(self):
        proposals.auto_approve(self._p({"characters": [
            {"name": "已有角色", "variants": [{"title": "新变体", "content": "x"}]}]}),
            "all")
        _, variants = self._chosen()
        self.assertIn("新变体", variants)

    def test_a_whole_new_character_arrives_wearing_something(self):
        proposals.auto_approve(self._p({"characters": [
            {"name": "新角色", "description": "x",
             "variants": [{"title": "常服", "content": "x"}]}]}), "all")
        _, variants = self._chosen()
        self.assertIn("常服", variants)

    def test_updating_something_does_not_switch_it_on(self):
        """
        It is already in the library. Whether it is in play is a decision the
        user made, and rewriting its text is not a reason to take it back.
        """
        proposals.auto_approve(self._p(
            {"settings": [{"title": "已有设定", "content": "新"}]}), "all")
        settings, _ = self._chosen()
        self.assertEqual(settings, set())
        self.assertEqual(Setting.objects.get(title="已有设定").content, "新")

    def test_what_was_already_chosen_stays_chosen(self):
        self.conv.selected_settings.add(self.old_setting)
        proposals.auto_approve(self._p(
            {"settings": [{"title": "新设定", "content": "x"}]}), "all")
        settings, _ = self._chosen()
        self.assertEqual(settings, {"已有设定", "新设定"})

    def test_edits_only_switches_on_the_costume_it_just_made(self):
        """It creates one kind of thing, and that kind is switched on too."""
        proposals.auto_approve(self._p({"characters": [
            {"name": "已有角色", "variants": [{"title": "新变体", "content": "x"}]}]}),
            "edits")
        _, variants = self._chosen()
        self.assertIn("新变体", variants)

    def test_edits_only_switches_nothing_on_when_it_refuses(self):
        p = self._p({"settings": [{"title": "新设定", "content": "x"}]})
        self.assertFalse(proposals.auto_approve(p, "edits"))
        self.assertEqual(self._chosen(), (set(), set()))

    def test_approving_by_hand_still_only_writes_the_entry(self):
        """
        The button is unchanged: a person approving an entry is also deciding,
        separately, whether to use it. Switching it on for them would be this
        feature leaking into the path it is supposed to leave alone.
        """
        p = self._p({"settings": [{"title": "新设定", "content": "x"}]})
        proposals.resolve(p.id, approve=True)
        self.assertTrue(Setting.objects.filter(title="新设定").exists())
        self.assertEqual(self._chosen(), (set(), set()))

    def test_a_new_character_with_no_costume_gets_the_default_one_and_is_in_play(self):
        """A character is in play through its costumes, so one written with
        none (a cat, a monster) is given the default costume rather than left
        on paper where no picture could be drawn from it."""
        proposals.auto_approve(self._p({"characters": [
            {"name": "只有名字的角色", "description": "x"}]}), "all")
        self.assertEqual(self._chosen(), (set(), {ui_text.DEFAULT_VARIANT_TITLE}))
        self.assertTrue(Character.objects.filter(name="只有名字的角色").exists())

    def test_the_selection_is_this_conversations_alone(self):
        other = Conversation.objects.create(title="另一个", folder=self.folder)
        proposals.auto_approve(self._p(
            {"settings": [{"title": "新设定", "content": "x"}]}), "all")
        self.assertEqual(other.selected_settings.count(), 0)


class TheTwoJudgementsMustAgreeTests(TestCase):
    """
    `creates_an_entry` and `apply` walk the same payload separately — one to
    decide, one to write. If they ever disagree about what counts as already
    there, 「仅修改」 waves through something `apply` then creates, which is the
    single thing that mode promises not to do.

    So this asks them both, about the same payload, and compares. Two
    independent walks of the same data is exactly the shape that drifts, and
    the drift would be silent.
    """

    def setUp(self):
        self.folder = Folder.objects.create(name="项目")
        self.conv = Conversation.objects.create(title="c", folder=self.folder)
        self.msg = Message.objects.create(conversation=self.conv, seq=1,
                                          role="assistant", status="done", content="")

    def _p(self, payload):
        return LibraryProposal.objects.create(
            conversation=self.conv, message=self.msg, folder=self.folder,
            payload=_project(payload),
        )

    def _entries(self):
        """Only the two kinds `creates_an_entry` speaks for."""
        return (Setting.objects.count(), Character.objects.count())

    def _counts(self):
        return (Setting.objects.count(), Character.objects.count(),
                CharacterVariant.objects.count())

    PAYLOADS = {
        "一个新设定": {"settings": [{"title": "新", "content": "x"}]},
        "一个新角色": {"characters": [{"name": "新人", "description": "x"}]},
        "新角色带变体": {"characters": [{"name": "新人", "variants": [
            {"title": "常服", "content": "x"}]}]},
        "空标题的设定": {"settings": [{"title": "  ", "content": "x"}]},
        "空名字的角色": {"characters": [{"name": "", "description": "x"}]},
        "空变体标题": {"characters": [{"name": "新人2", "variants": [
            {"title": "", "content": "x"}]}]},
        "什么都没有": {},
        "空列表": {"settings": [], "characters": []},
        "变体没有 title 键": {"characters": [{"name": "新人3", "variants": [{}]}]},
    }

    def _fresh(self):
        Setting.objects.all().delete()
        Character.objects.all().delete()

    def _seeded(self):
        """A library with one of everything, so an edit can be an edit."""
        self._fresh()
        Setting.objects.create(folder=self.folder, title="已有设定", content="旧")
        ch = Character.objects.create(folder=self.folder, name="已有角色", description="旧")
        CharacterVariant.objects.create(character=ch, title="已有变体", content="旧")

    #: Payloads aimed at a library that already has 已有设定 / 已有角色 / 已有变体.
    #: The pair that matters most is the last two: an existing character is the
    #: only way to reach the variant branch of `creates_an_entry` at all, and
    #: the first version of this test never did — every character in it was new,
    #: so the branch that decides whether a new costume counts was untested and
    #: the agreement it is supposed to guarantee was never actually checked.
    AGAINST_A_LIBRARY = {
        "改一个已有设定": {"settings": [{"title": "已有设定", "content": "新"}]},
        "改一个已有角色的描述": {"characters": [
            {"name": "已有角色", "description": "新"}]},
        "改已有角色的已有变体": {"characters": [
            {"name": "已有角色", "variants": [{"title": "已有变体", "content": "新"}]}]},
        "已有角色 + 新变体": {"characters": [
            {"name": "已有角色", "variants": [{"title": "新变体", "content": "x"}]}]},
        "已有设定 + 一个新设定": {"settings": [
            {"title": "已有设定", "content": "新"}, {"title": "新设定", "content": "x"}]},
    }

    def test_saying_no_new_entry_means_no_new_entry(self):
        for name, payload in self.PAYLOADS.items():
            with self.subTest(payload=name):
                self._fresh()
                p = self._p(payload)
                said = proposals.creates_an_entry(p)
                before = self._entries()
                proposals.apply(p)
                actually = self._entries() != before
                self.assertEqual(
                    said, actually,
                    f"{name}: creates_an_entry 说 {said}，apply 实际 {actually}",
                )

    def test_they_still_agree_against_a_library_that_has_things_in_it(self):
        """
        The half that matters: against an empty library everything is new, so
        agreeing there is easy. The judgement earns its keep when some of the
        payload is an edit and some of it is not.
        """
        for name, payload in self.AGAINST_A_LIBRARY.items():
            with self.subTest(payload=name):
                self._seeded()
                p = self._p(payload)
                said = proposals.creates_an_entry(p)
                before = self._entries()
                proposals.apply(p)
                actually = self._entries() != before
                self.assertEqual(
                    said, actually,
                    f"{name}: creates_an_entry 说 {said}，apply 实际 {actually}",
                )

    def test_a_costume_is_not_an_entry(self):
        """
        The line this mode is drawn on. A new costume on a character that is
        already there changes the variant count and nothing else — so it is
        not something `creates_an_entry` speaks for.
        """
        self._seeded()
        p = self._p({"characters": [
            {"name": "已有角色", "variants": [{"title": "新变体", "content": "x"}]}]})
        self.assertFalse(proposals.creates_an_entry(p))

        before_entries, before_all = self._entries(), self._counts()
        proposals.apply(p)
        self.assertEqual(self._entries(), before_entries, "no entry should appear")
        self.assertNotEqual(self._counts(), before_all, "but the costume should")

    def test_edits_only_is_exactly_as_strict_as_creates_an_entry(self):
        """
        The mode is defined in terms of that judgement, so the two must never
        part company: whatever 「仅修改」 lets through must leave the number of
        entries where it found it. Costumes may appear; entries may not.
        """
        for name, payload in {**self.PAYLOADS, **self.AGAINST_A_LIBRARY}.items():
            with self.subTest(payload=name):
                self._seeded()
                p = self._p(payload)
                before = self._entries()
                acted = proposals.auto_approve(p, "edits")
                if acted:
                    self.assertEqual(
                        self._entries(), before,
                        f"{name}: 「仅修改」放行了，但库里多了条目",
                    )

    def test_the_same_payload_twice_is_new_only_once(self):
        payload = {"settings": [{"title": "两次", "content": "x"}],
                   "characters": [{"name": "人", "variants": [
                       {"title": "常服", "content": "x"}]}]}
        first = self._p(payload)
        self.assertTrue(proposals.creates_an_entry(first))
        proposals.apply(first)

        second = self._p(payload)
        self.assertFalse(proposals.creates_an_entry(second))
        before = self._counts()
        proposals.apply(second)
        self.assertEqual(self._counts(), before)


class SettlingTwiceTests(TestCase):
    """
    A proposal is settled once. Two things can reach for it — the button and
    the automatic path — and after this feature they can do so at the same
    moment, which they could not before.
    """

    def setUp(self):
        self.folder = Folder.objects.create(name="项目")
        Setting.objects.create(folder=self.folder, title="设定", content="旧")
        self.conv = Conversation.objects.create(title="c", folder=self.folder)
        self.msg = Message.objects.create(conversation=self.conv, seq=1,
                                          role="assistant", status="done", content="")
        self.p = LibraryProposal.objects.create(
            conversation=self.conv, message=self.msg, folder=self.folder,
            payload=_project({"settings": [{"title": "设定", "content": "新"}]}),
        )

    def test_approving_an_applied_one_writes_nothing_a_second_time(self):
        proposals.resolve(self.p.id, approve=True)
        Setting.objects.filter(title="设定").update(content="有人后来又改了")

        again = proposals.resolve(self.p.id, approve=True)
        assert again is not None
        self.assertEqual(again.status, "applied")
        self.assertEqual(Setting.objects.get(title="设定").content, "有人后来又改了")

    def test_the_automatic_path_will_not_revive_one_the_user_rejected(self):
        """The user said no; the app arriving later must not overrule them."""
        proposals.resolve(self.p.id, approve=False)
        self.p.refresh_from_db()

        self.assertFalse(proposals.auto_approve(self.p, "all"))
        self.p.refresh_from_db()
        self.assertEqual(self.p.status, "rejected")
        self.assertEqual(Setting.objects.get(title="设定").content, "旧")

    def test_a_proposal_that_is_gone_is_not_an_error(self):
        pid = self.p.id
        self.p.delete()
        self.assertIsNone(proposals.resolve(pid, approve=True))

    def test_auto_approve_reports_what_actually_happened(self):
        """
        It returns whether it applied, and the caller has only that to go on —
        so it must not say yes about a row it did not settle.
        """
        proposals.resolve(self.p.id, approve=True)
        self.p.refresh_from_db()
        self.assertFalse(proposals.auto_approve(self.p, "all"))


class ATurnSurvivesThisGoingWrongTests(TestCase):
    """
    The proposal is written down first and judged second, so a failure in the
    judgement costs a click — never the edit, and never the turn.
    """

    def setUp(self):
        self.folder = Folder.objects.create(name="项目")
        Setting.objects.create(folder=self.folder, title="设定", content="旧")
        self.conv = Conversation.objects.create(title="c", folder=self.folder)
        gensettings.merge(self.conv, {"auto_library": "all"})
        self.conv.refresh_from_db()
        self.msg = Message.objects.create(conversation=self.conv, seq=1,
                                          role="assistant", status="done", content="")

    def test_the_edit_survives_the_judgement_blowing_up(self):
        from unittest import mock

        with mock.patch.object(
            proposals, "auto_approve", side_effect=RuntimeError("boom")
        ):
            pipeline._record_proposal(
                self.conv, self.msg,
                _project({"settings": [{"title": "设定", "content": "新"}]}),
            )
        p = LibraryProposal.objects.filter(conversation=self.conv).first()
        assert p is not None, "the edit must not be lost along with the judgement"
        self.assertEqual(p.status, "pending")


@override_settings(**TEST_CREDS)
class TheSettingTravelsOverHttpTests(TestCase):
    """Set in one browser, read back in another. The rail writes through this."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from django.test import Client

        get_user_model().objects.create_user(username="yyy", password="pw")
        self.c = Client()
        self.c.force_login(get_user_model().objects.get(username="yyy"))
        self.conv = Conversation.objects.create(title="c")

    def _patch(self, value):
        import json

        # the same request the right rail makes — see store/settings.ts
        return self.c.patch(
            f"/api/conversations/{self.conv.id}",
            json.dumps({"gen_settings": {"auto_library": value}}),
            content_type="application/json",
        )

    def test_it_goes_there_and_comes_back(self):
        self.assertEqual(self._patch("edits").status_code, 200)
        self.conv.refresh_from_db()
        self.assertEqual(gensettings.of(self.conv)["auto_library"], "edits")

    def test_a_value_nobody_defined_does_not_get_stored(self):
        self._patch("whatever")
        self.conv.refresh_from_db()
        self.assertEqual(gensettings.of(self.conv)["auto_library"], "off")

    def test_the_default_a_new_conversation_starts_from(self):
        gensettings.set_defaults({"auto_library": "all"})
        self.assertEqual(gensettings.defaults()["auto_library"], "all")

    def test_what_the_browser_is_shown_is_the_same_either_way(self):
        """
        The last link in 「其他部分不可知」: the transcript renders a proposal
        from its status and summary alone, so an automatic approval has to
        arrive over the wire looking exactly like one the user made.
        """
        folder = Folder.objects.create(name="项目")
        Setting.objects.create(folder=folder, title="设定", content="旧")
        self.conv.folder = folder
        self.conv.save(update_fields=["folder"])
        msg = Message.objects.create(conversation=self.conv, seq=1, role="assistant",
                                     status="done", content="")
        payload = _project({"settings": [{"title": "设定", "content": "新"}]})

        by_hand = LibraryProposal.objects.create(
            conversation=self.conv, message=msg, folder=folder, payload=payload)
        self.c.post(f"/api/proposals/{by_hand.id}/resolve",
                    json.dumps({"approve": True}), content_type="application/json")

        Setting.objects.filter(title="设定").update(content="旧")
        by_app = LibraryProposal.objects.create(
            conversation=self.conv, message=msg, folder=folder, payload=payload)
        proposals.auto_approve(by_app, "all")

        shown = {
            p["id"]: p for p in
            self.c.get(f"/api/conversations/{self.conv.id}/proposals").json()["proposals"]
        }
        a, b = shown[str(by_hand.id)], shown[str(by_app.id)]
        for field in ("status", "applied_summary"):
            self.assertEqual(a[field], b[field], field)
        self.assertEqual(a["status"], "applied")
        self.assertTrue(a["applied_summary"], "the browser prints this")


@override_settings(**TEST_CREDS)
class ApprovingByHandStillGoesThroughOnePlaceTests(TestCase):
    """The view kept its own copy of this until the automatic path needed it."""

    def test_the_view_does_not_write_the_library_itself(self):
        import inspect

        from core.views import projects

        src = inspect.getsource(projects.resolve_proposal)
        self.assertIn("proposals.resolve", src)
        for forbidden in ("Setting.objects.create", "applied_summary ="):
            self.assertNotIn(forbidden, src)


class TheCardSaysWhichLayerAnEntryWasMadeInTests(TestCase):
    """
    「新建设定「黄昏」」 read the same whether the entry went into the project
    or into this conversation alone. The two are not the same thing -- one
    every conversation of the project can use, the other this one only -- so
    the card says （对话级） for the conversation's own layer.
    """

    def setUp(self):
        self.folder = Folder.objects.create(name="项目")
        self.conv = Conversation.objects.create(title="t", folder=self.folder)
        self.msg = Message.objects.create(conversation=self.conv, seq=1, role="assistant", content="")

    @staticmethod
    def _payload(scope: str) -> dict:
        return {
            "settings": [{"title": "黄昏", "content": "落日", "scope": scope,
                          "tags": [{"part": "天色", "tags": "sunset"}]}],
            "characters": [{"name": "小满", "description": "黑发的女孩", "scope": scope,
                            "tags": [{"part": "身份", "tags": "1girl, black hair"}],
                            "variants": [{"title": "校服", "content": "水手服",
                                          "tags": [{"part": "衣服", "tags": "serafuku"}]}]}],
        }

    def test_an_entry_made_for_this_conversation_says_so(self):
        p = proposals.record(self.conv, self.msg, self._payload("conversation"))
        p.refresh_from_db()
        self.assertEqual(p.status, "applied")
        self.assertIn("新建设定（对话级）「黄昏」", p.applied_summary)
        self.assertIn("新建角色（对话级）「小满」", p.applied_summary)
        self.assertIn("新建造型（对话级）「小满·校服」", p.applied_summary)

    def test_an_entry_made_for_the_project_does_not(self):
        p = proposals.record(self.conv, self.msg, self._payload("project"))
        proposals.resolve(p.id, approve=True)
        p.refresh_from_db()
        self.assertEqual(p.status, "applied")
        self.assertIn("新建设定「黄昏」", p.applied_summary)
        self.assertIn("新建角色「小满」", p.applied_summary)
        self.assertIn("新建造型「小满·校服」", p.applied_summary)
        self.assertNotIn("对话级", p.applied_summary)
