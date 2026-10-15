"""
The wall-clock cap on one model answer.

A read timeout bounds the silence between two bytes, and a model that keeps
its connection alive with empty lines is never silent, so a runaway answer
used to run until max_tokens. These drive post_with_retry against a stand-in
endpoint that trickles keep-alive bytes, and check that the cap lands, that a
capped answer is asked for once more, and that the second runaway is reported.
"""

from __future__ import annotations

import time
from unittest import mock

import httpx
from django.test import SimpleTestCase

from core.services.providers import base


def _trickle(then: bytes, pieces: int = 20, every: float = 0.03):
    """A body that arrives slowly: keep-alive newlines, then the answer."""
    def body():
        for _ in range(pieces):
            time.sleep(every)
            yield b"\n"
        yield then
    return body()


class AnswerCapTests(SimpleTestCase):
    def _endpoint(self, handler):
        """post_with_retry opens its own httpx.Client; route it to `handler`."""
        real = httpx.Client
        transport = httpx.MockTransport(handler)
        return mock.patch.object(base.httpx, "Client", lambda **kw: real(transport=transport, **kw))

    def test_an_answer_that_outruns_the_cap_is_cut_and_asked_once_more(self):
        calls: list[int] = []

        def handler(request):
            calls.append(1)
            slow = len(calls) == 1
            return httpx.Response(200, content=_trickle(b'{"ok": 1}') if slow else b'{"ok": 1}')

        with self._endpoint(handler):
            got = base.post_with_retry("https://model/x", {}, {}, 5, "x", retries=3, answer_seconds=0.2)
        self.assertEqual(got, {"ok": 1})
        self.assertEqual(len(calls), 2)

    def test_two_runaways_in_a_row_are_reported_not_waited_out(self):
        calls: list[int] = []

        def handler(request):
            calls.append(1)
            return httpx.Response(200, content=_trickle(b'{"ok": 1}'))

        with self._endpoint(handler), self.assertRaises(base.LLMError) as caught:
            base.post_with_retry("https://model/x", {}, {}, 5, "x", retries=3, answer_seconds=0.2)
        self.assertEqual(len(calls), 2)
        # the endpoint answered; it is the request that ran away, so the road
        # stays open
        self.assertEqual(caught.exception.status, 408)
        self.assertFalse(base._impassable(caught.exception))

    def test_an_answer_within_the_cap_is_read_whole(self):
        def handler(request):
            return httpx.Response(200, content=_trickle(b'{"ok": 2}', pieces=3, every=0.01))

        with self._endpoint(handler):
            got = base.post_with_retry("https://model/x", {}, {}, 5, "x", retries=1, answer_seconds=5)
        self.assertEqual(got, {"ok": 2})

    def test_without_a_cap_nothing_changes(self):
        def handler(request):
            return httpx.Response(200, content=_trickle(b'{"ok": 3}', pieces=3, every=0.01))

        with self._endpoint(handler):
            got = base.post_with_retry("https://model/x", {}, {}, 5, "x", retries=1)
        self.assertEqual(got, {"ok": 3})
