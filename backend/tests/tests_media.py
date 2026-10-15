"""
Serving pictures: which copy, and what keeps it cached.

Two separate problems that both showed up as "the images load again every
time I open the app":

  * the transcript was being sent the full picture — 1.3 MB at p95 — to draw
    at 410px at the widest, so the service worker's store came to hundreds of
    megabytes, which a phone will not keep and evicts wholesale;
  * the response carried ``Vary: Cookie`` (Django adds it to anything whose
    handler touched the session, which is everything behind a login) and no
    ETag, so the browser's own cache was keyed on the entire cookie string and
    had no way to revalidate.

Neither fix is worth anything without the other, which is why they are tested
together.
"""

from __future__ import annotations

import io
import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import Client, TestCase, override_settings

from core.models import Attachment, Conversation, Message
from core.services import imaging

TEST_CREDS = {"APP_USERNAME": "yyy", "APP_PASSWORD": "pw"}


def _png(w: int = 1600, h: int = 1200) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (w, h))
    # noise, so the PNG does not compress to nothing and the sizes mean something
    px = img.load()
    assert px is not None
    for y in range(0, h, 7):
        for x in range(0, w, 7):
            px[x, y] = ((x * 7) % 256, (y * 13) % 256, ((x + y) * 3) % 256)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@override_settings(**TEST_CREDS)
class WhichCopyGetsSentTests(TestCase):
    def setUp(self):
        get_user_model().objects.create_user(username="yyy", password="pw")
        self.c = Client()
        self.c.force_login(get_user_model().objects.get(username="yyy"))
        conv = Conversation.objects.create(title="图")
        msg = Message.objects.create(
            conversation=conv, seq=1, role="assistant", status="done", content=""
        )
        self.att = Attachment.objects.create(
            message=msg, kind="generated", mime="image/png"
        )
        self.att.file.save("big.png", ContentFile(_png()), save=True)
        self.full = f"/api/media/{self.att.id}/{os.path.basename(self.att.file.name)}"
        self.thumb = f"/api/media/{self.att.id}/thumb"

    @staticmethod
    def _body(resp) -> bytes:
        if not resp.streaming:
            return resp.content
        data = b"".join(resp.streaming_content)
        resp.close()           # the file handle, not just the bytes
        return data

    def test_the_grid_is_told_to_ask_for_the_small_one(self):
        """
        The rule lives in the serializer, so nothing that draws a picture has
        to remember it. A component reaching for `url` to draw a thumbnail is
        the bug this exists to make hard.
        """
        from core.http import attachment_to_dict

        d = attachment_to_dict(self.att)
        self.assertEqual(d["thumb_url"], self.thumb)
        self.assertNotEqual(d["thumb_url"], d["url"])

    def test_the_small_one_is_actually_small(self):
        """
        Asserted as pixels rather than as a ratio of bytes: how well a PNG
        compresses depends on what is in it, and a synthetic test image is not
        a drawing. The pixel count is the thing under this module's control,
        and it is what decides whether the bytes are a hundredth or a tenth.

        For scale, from real generated pictures on disk: 3,641 KB of PNG comes
        to 81 KB at this edge.
        """
        from PIL import Image

        small = Image.open(io.BytesIO(self._body(self.c.get(self.thumb))))
        self.assertLessEqual(max(small.size), imaging.THUMB_EDGE)
        big = Image.open(io.BytesIO(self._body(self.c.get(self.full))))
        self.assertGreater(max(big.size), max(small.size))

    def test_the_small_one_is_a_jpeg_and_the_full_one_is_untouched(self):
        thumb = self.c.get(self.thumb)
        self.assertEqual(thumb["Content-Type"], "image/jpeg")
        full = self.c.get(self.full)
        self.assertEqual(full["Content-Type"], "image/png")
        self.assertEqual(
            self._body(full), self.att.file.read(),
            "opening a picture has to give the picture, not a copy of it",
        )

    def test_it_is_made_once(self):
        self.assertFalse(self.att.thumb)
        self.c.get(self.thumb)
        self.att.refresh_from_db()
        self.assertTrue(self.att.thumb, "the second request should not redo the work")
        name = self.att.thumb.name
        self.c.get(self.thumb)
        self.att.refresh_from_db()
        self.assertEqual(self.att.thumb.name, name)

    def test_it_is_made_for_pictures_that_already_exist(self):
        """
        Made on request, not at generation time. Every picture already on disk
        predates this, and a turn should never wait to paint one.
        """
        self.assertFalse(self.att.thumb)
        self.assertEqual(self.c.get(self.thumb).status_code, 200)

    def test_a_picture_with_no_thumbnail_to_be_had_still_shows(self):
        """A slow grid beats a grid with a hole in it."""
        bad = Attachment.objects.create(
            message=self.att.message, kind="generated", mime="image/png"
        )
        bad.file.save("notreally.png", ContentFile(b"this is not a png"), save=True)
        resp = self.c.get(f"/api/media/{bad.id}/thumb")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._body(resp), b"this is not a png")

    def test_a_thumbnail_whose_file_went_missing_is_made_again(self):
        first = self.c.get(self.thumb)
        self._body(first)
        first.close()          # Windows will not delete a file still open
        self.att.refresh_from_db()
        self.att.thumb.storage.delete(self.att.thumb.name)
        resp = self.c.get(self.thumb)
        self.assertEqual(resp.status_code, 200, "a picture that exists must still show")

    def test_changing_the_size_remakes_the_ones_already_stored(self):
        """
        The size lives in the file name, so a stored thumbnail whose name does
        not match was made at some other size and re-makes itself.

        Without this, raising THUMB_EDGE would improve nothing for anyone who
        already has pictures: every one of them already has a thumbnail, and
        `if att.thumb: return` would keep handing back the old blurry one
        forever.
        """
        from PIL import Image

        self._body(self.c.get(self.thumb))
        self.att.refresh_from_db()
        was = self.att.thumb.name
        self.assertIn(str(imaging.THUMB_EDGE), was)

        with mock.patch.object(imaging, "THUMB_EDGE", 256):
            resp = self.c.get(self.thumb)
            small = Image.open(io.BytesIO(self._body(resp)))
        self.assertEqual(max(small.size), 256, "it should have been made again")

        self.att.refresh_from_db()
        self.assertNotEqual(self.att.thumb.name, was)

    def test_the_size_that_is_no_longer_wanted_does_not_stay_on_disk(self):
        """A thumbnail nothing will ask for again is just a file taking room."""
        from django.core.files.storage import default_storage

        self._body(self.c.get(self.thumb))   # and close it: Windows will not
        self.att.refresh_from_db()           # delete a file still open
        was = self.att.thumb.name
        self.assertTrue(default_storage.exists(was))

        with mock.patch.object(imaging, "THUMB_EDGE", 256):
            self._body(self.c.get(self.thumb))
        self.assertFalse(default_storage.exists(was))

    def test_the_thumbnail_is_beside_the_picture_not_in_the_database(self):
        """Images live in MEDIA_ROOT. The database holds text."""
        self.c.get(self.thumb)
        self.att.refresh_from_db()
        self.assertTrue(self.att.thumb.name.endswith(".jpg"))
        self.assertIn(str(self.att.message.conversation_id), self.att.thumb.name)


@override_settings(**TEST_CREDS)
class WhatKeepsAPictureCachedTests(TestCase):
    def setUp(self):
        get_user_model().objects.create_user(username="yyy", password="pw")
        self.c = Client()
        self.c.force_login(get_user_model().objects.get(username="yyy"))
        conv = Conversation.objects.create(title="图")
        msg = Message.objects.create(
            conversation=conv, seq=1, role="assistant", status="done", content=""
        )
        self.att = Attachment.objects.create(
            message=msg, kind="generated", mime="image/png"
        )
        self.att.file.save("p.png", ContentFile(_png(320, 240)), save=True)
        self.full = f"/api/media/{self.att.id}/{os.path.basename(self.att.file.name)}"
        self.thumb = f"/api/media/{self.att.id}/thumb"

    def test_a_picture_does_not_vary_by_who_asked(self):
        """
        Measured before this existed: ``Vary: Cookie``, added by the session
        middleware because the handler checked who was asking. For JSON that is
        right. For a PNG it means the cache key is the whole cookie string, so
        one rotated CSRF token throws away every picture the browser had.
        """
        for url in (self.full, self.thumb):
            with self.subTest(url=url):
                self.assertEqual(self.c.get(url).get("Vary", ""), "")

    def test_something_that_does_depend_on_who_asked_still_says_so(self):
        """
        The header is dropped per response, by the view that knows its answer
        is the same for everyone. Anything that did not say so keeps it.
        """
        resp = self.c.get("/api/conversations")
        self.assertIn("Cookie", resp.get("Vary", ""))

    def test_it_is_never_stored_anywhere_shared(self):
        """What makes dropping Vary safe. These two travel together."""
        for url in (self.full, self.thumb):
            with self.subTest(url=url):
                self.assertIn("private", self.c.get(url)["Cache-Control"])

    def test_asking_again_costs_a_few_bytes_rather_than_the_picture(self):
        for url in (self.full, self.thumb):
            with self.subTest(url=url):
                first = self.c.get(url)
                tag = first["ETag"]
                self.assertTrue(tag)
                again = self.c.get(url, headers={"if-none-match": tag})
                self.assertEqual(again.status_code, 304)
                self.assertEqual(again.content, b"")

    def test_the_two_copies_are_not_each_other(self):
        """One etag for both would serve the thumbnail as the picture."""
        self.assertNotEqual(self.c.get(self.full)["ETag"], self.c.get(self.thumb)["ETag"])

    def test_a_304_still_says_how_long_it_stays_fresh(self):
        """Otherwise it buys this one round trip and the next one asks again."""
        tag = self.c.get(self.full)["ETag"]
        again = self.c.get(self.full, headers={"if-none-match": tag})
        self.assertIn("immutable", again["Cache-Control"])
        self.assertEqual(again["ETag"], tag)


class TheSizeThisIsAllForTests(TestCase):
    def test_the_thumbnail_is_small_enough_that_a_phone_keeps_them(self):
        """
        Not a style rule — the number is the whole point. The service worker
        holds 1200 thumbnails; at 1.3 MB apiece that is 1.5 GB and a phone
        evicts the lot, which is the bug. This keeps the edge honest.
        """
        self.assertLessEqual(imaging.THUMB_EDGE, 1024)
        self.assertGreaterEqual(
            imaging.THUMB_EDGE, 768, "a grid draws 410px wide, so 2x is the floor"
        )
