"""
The roads endpoint, over HTTP.

Why this file exists at all: the switch shipped broken. Every rule about it was
tested — stepping over a road, the probe putting it back, nothing being
stored — and all of it passed, because all of it was tested against the
provider object. The URL was not wired up, so the button in the settings page
got a 404 and did nothing, and no test noticed.

A view that cannot be reached is a view that does not exist. These go through
the client, so the route is part of what is being asserted.
"""

from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from core.services.providers import base, deepseek

CREDS = {"APP_USERNAME": "yyy", "APP_PASSWORD": "pw"}


@override_settings(
    LLM_PROVIDER="deepseek",
    DEEPSEEK_API_KEY="k",
    OPENROUTER_API_KEY="or",
    **CREDS,
)
class TheRoadSwitchIsReachableTests(TestCase):
    def setUp(self):
        get_user_model().objects.create_user(username="yyy", password="pw")
        self.c = Client()
        self.c.force_login(get_user_model().objects.get(username="yyy"))
        deepseek._known = base.Roads(blocked_for=1800.0)

    def _roads(self):
        return {r["name"]: r for r in self.c.get("/api/app-settings").json()["roads"]}

    def _say(self, name, over):
        return self.c.post(
            f"/api/roads/{name}",
            json.dumps({"stepped_over": over}),
            content_type="application/json",
        )

    def test_the_page_can_see_the_roads(self):
        roads = self._roads()
        self.assertEqual(set(roads), {"deepseek", "openrouter"})
        self.assertTrue(roads["deepseek"]["in_use"])
        self.assertFalse(roads["openrouter"]["in_use"])

    def test_saying_a_road_is_out_moves_the_app_onto_the_other_one(self):
        """The whole point, end to end, through the URL the button posts to."""
        resp = self._say("deepseek", True)
        self.assertEqual(resp.status_code, 200)

        after = {r["name"]: r for r in resp.json()["roads"]}
        self.assertTrue(after["deepseek"]["stepped_over"])
        self.assertTrue(after["openrouter"]["in_use"])
        # and it is still true on the next page load, not just in the reply
        self.assertTrue(self._roads()["deepseek"]["stepped_over"])

    def test_saying_it_is_back_undoes_that(self):
        self._say("deepseek", True)
        resp = self._say("deepseek", False)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(self._roads()["deepseek"]["stepped_over"])
        self.assertTrue(self._roads()["deepseek"]["in_use"])

    def test_a_road_nobody_has(self):
        self.assertEqual(self._say("nonesuch", True).status_code, 404)

    def test_a_body_that_says_nothing(self):
        for body in ({}, {"stepped_over": "yes"}, {"stepped_over": 1}):
            with self.subTest(body=body):
                resp = self.c.post(
                    "/api/roads/deepseek",
                    json.dumps(body),
                    content_type="application/json",
                )
                self.assertEqual(resp.status_code, 400)

    def test_it_needs_a_login(self):
        self.assertEqual(Client().post("/api/roads/deepseek").status_code, 401)

    @override_settings(OPENROUTER_API_KEY="")
    def test_with_one_way_in_the_page_is_told_there_is_no_choice(self):
        """The section hides itself on this, so it has to be honest."""
        self.assertEqual(len(self.c.get("/api/app-settings").json()["roads"]), 1)
