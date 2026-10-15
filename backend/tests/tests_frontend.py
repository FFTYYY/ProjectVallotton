"""
Django serving the built frontend -- only when told to.

The local form runs one process and Django hands out frontend/dist itself;
the server form has nginx do it and Django must not: a stray switch there
would mean Django serving files it never meant to. So the default is off and
off means 404, and on means every app route gets index.html while the API
keeps answering as itself.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from django.test import Client, TestCase, override_settings


class ServingTheFrontendTests(TestCase):
    def setUp(self):
        self.c = Client()

    def test_off_by_default_nothing_but_the_api_is_served(self):
        for url in ("/", "/c/abc", "/f/x", "/settings", "/login", "/index.html"):
            self.assertEqual(self.c.get(url).status_code, 404, url)
        self.assertEqual(self.c.get("/api/auth/me").status_code, 200)

    def test_on_every_app_route_gets_index_and_the_api_still_answers_as_itself(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "index.html").write_text('<!doctype html><div id="root">vallotton</div>', encoding="utf-8")
            with override_settings(SERVE_FRONTEND=True, FRONTEND_DIST=Path(d)):
                for url in ("/", "/c/abc", "/f/x/y", "/settings", "/login"):
                    r = self.c.get(url)
                    self.assertEqual(r.status_code, 200, url)
                    self.assertIn(b"vallotton", r.content)
                    # a build's index names that build's hashed files: never held on to
                    self.assertIn("no-cache", r["Cache-Control"])
                r = self.c.get("/api/auth/me")
                self.assertEqual(r.status_code, 200)
                self.assertNotIn(b"vallotton", r.content)
                self.assertEqual(self.c.get("/api/no-such-thing").status_code, 404)
                self.assertEqual(self.c.post("/").status_code, 405)

    def test_on_without_a_build_is_a_404_not_a_crash(self):
        with tempfile.TemporaryDirectory() as d:
            with override_settings(SERVE_FRONTEND=True, FRONTEND_DIST=Path(d, "missing")):
                self.assertEqual(self.c.get("/c/abc").status_code, 404)
