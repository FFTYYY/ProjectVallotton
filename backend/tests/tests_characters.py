"""
A character is reachable only through a variant, so it always has one.

Held up as a repair rather than a guard. A guard would have to sit at every
entrance — the API, the model's library edits, MCP, the admin — and would make
the caller responsible for knowing the rule, which in a browser means keeping a
count of the variants to decide whether to offer the delete button. That count
goes stale the moment a second tab deletes one.

So these are mostly about the ways in that a guard would have missed, and about
two callers arriving in an order nobody planned.
"""

from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from core.models import Character, CharacterVariant, Conversation, Folder
from core.services import characters as characters_service, library

TEST_CREDS = {"APP_USERNAME": "yyy", "APP_PASSWORD": "pw"}


@override_settings(**TEST_CREDS)
class ACharacterAlwaysHasAVariantTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.force_login(
            get_user_model().objects.create_user(username="lib", password="x")
        )
        self.folder = Folder.objects.create(name="项目")

    def _character(self, name="林", variants=1):
        c = Character.objects.create(folder=self.folder, name=name, position=1)
        for i in range(variants):
            CharacterVariant.objects.create(
                character=c, title=f"变体{i + 1}", content="x", position=i + 1
            )
        return c

    def _variants(self, c):
        return list(c.variants.order_by("position").values_list("title", flat=True))

    def _library(self):
        return self.client.get(f"/api/folders/{self.folder.id}/library").json()

    # ---------------------------------------------------------- the rule

    def test_deleting_the_last_variant_is_allowed_and_leaves_one_behind(self):
        c = self._character(variants=1)
        only = c.variants.first()
        assert only is not None
        r = self.client.delete(
            f"/api/variants/{only.id}"
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._variants(c), [characters_service.default_title()])

    def test_the_reply_already_carries_it(self):
        """Run at once rather than left to the next read, so the list the
        browser gets back is not missing a row it will be shown later."""
        c = self._character(variants=1)
        only = c.variants.first()
        assert only is not None
        self.client.delete(
            f"/api/variants/{only.id}"
        )
        got = self._library()
        row = next(x for x in got["characters"] if x["id"] == str(c.id))
        self.assertEqual([v["title"] for v in row["variants"]],
                         [characters_service.default_title()])

    def test_deleting_one_of_several_leaves_the_rest_alone(self):
        c = self._character(variants=3)
        first = c.variants.order_by("position").first()
        assert first is not None
        self.client.delete(
            f"/api/variants/{first.id}"
        )
        self.assertEqual(self._variants(c), ["变体2", "变体3"])

    # ------------------------------------------- the ways in a guard misses

    def test_a_character_made_without_variants_is_repaired_when_read(self):
        """
        The model's library edits create characters directly, which is why the
        prompt had to start asking for a variant. Asking is not a mechanism.
        """
        c = Character.objects.create(folder=self.folder, name="无变体", position=2)
        self.assertEqual(self._variants(c), [])
        self._library()
        self.assertEqual(self._variants(c), [characters_service.default_title()])

    def test_it_is_repaired_for_a_conversation_that_inherits_it(self):
        """The other read path: a child folder looking at an ancestor's."""
        child = Folder.objects.create(name="子", parent=self.folder)
        c = Character.objects.create(folder=self.folder, name="继承来的", position=3)
        self.client.get(f"/api/folders/{child.id}/library")
        self.assertEqual(self._variants(c), [characters_service.default_title()])

    def test_a_library_proposal_cannot_leave_one_empty_either(self):
        conv = self.client.post(
            "/api/conversations", json.dumps({"folder_id": str(self.folder.id)}),
            content_type="application/json",
        ).json()
        from core.models import LibraryProposal, Message

        msg = Message.objects.create(conversation_id=conv["id"], seq=1,
                                     role="assistant", status="done")
        p = LibraryProposal.objects.create(
            conversation_id=conv["id"], message=msg, folder=self.folder,
            payload={"settings": [],
                     "characters": [{"key": library.PROJECT_PREFIX + "提案来的", "note": "d",
                                     "positive": "", "negative": "", "prose": "", "variants": []}]},
        )
        r = self.client.post(f"/api/proposals/{p.id}/resolve",
                             data='{"approve": true}',
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)
        made = Character.objects.get(folder=self.folder, name="提案来的")
        self._library()
        self.assertEqual(self._variants(made), [characters_service.default_title()])

    def test_the_leads_own_write_cannot_leave_one_empty_either(self):
        """The conversation's layer is written without a proposal; the write
        itself gives a costume-less character the default one, and switches
        it on, since a character is in play through its costumes."""
        conv = Conversation.objects.create(title="t", folder=self.folder)
        library.write_conversation_entries(conv, 2, [], [
            {"key": "猫", "note": "一只猫", "positive": "cat", "negative": "", "prose": "", "variants": []}])
        made = Character.objects.get(conversation=conv, name="猫")
        self.assertEqual(self._variants(made), [characters_service.default_title()])
        self.assertEqual([v.title for v in conv.selected_variants.all()], [characters_service.default_title()])

    # ------------------------------------------------- order and repetition

    def test_two_tabs_deleting_at_once_still_leave_one(self):
        """
        The case a count in the browser gets wrong. Both tabs saw two variants;
        both delete. Neither request has to know what the other did.
        """
        c = self._character(variants=2)
        a, b = list(c.variants.order_by("position"))
        r1 = self.client.delete(
            f"/api/variants/{a.id}"
        )
        r2 = self.client.delete(
            f"/api/variants/{b.id}"
        )
        self.assertEqual((r1.status_code, r2.status_code), (200, 200),
                         "a delete that looked available was refused")
        self.assertEqual(self._variants(c), [characters_service.default_title()])

    def test_repairing_twice_does_not_make_two(self):
        c = Character.objects.create(folder=self.folder, name="两次", position=4)
        characters_service.repair(c)
        characters_service.repair(c)
        self.assertEqual(self._variants(c), [characters_service.default_title()])

    def test_repairing_leaves_a_character_that_is_fine_alone(self):
        c = self._character(variants=2)
        self.assertEqual(characters_service.repair(c), 0)
        self.assertEqual(self._variants(c), ["变体1", "变体2"])

    def test_it_takes_whatever_names_some_characters(self):
        one = Character.objects.create(folder=self.folder, name="甲", position=5)
        two = Character.objects.create(folder=self.folder, name="乙", position=6)
        self.assertEqual(
            characters_service.repair(Character.objects.filter(folder=self.folder)), 2
        )
        for c in (one, two):
            self.assertEqual(self._variants(c), [characters_service.default_title()])
