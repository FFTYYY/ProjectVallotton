"""A model answer is cut at the wall-clock cap however the socket behaves."""

import threading
import time
from unittest import mock

from django.test import SimpleTestCase

from core.services.providers import base


class _Stream:
    """A response whose body never arrives: the reader blocks in iter_bytes."""

    status_code = 200

    def __init__(self, gate: threading.Event) -> None:
        self.gate = gate

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self):
        self.gate.wait(10)
        yield b"{}"


class ReadCapTests(SimpleTestCase):
    def test_a_body_that_never_arrives_is_cut_at_the_cap(self):
        gate = threading.Event()
        client = mock.Mock()
        client.stream.return_value = _Stream(gate)
        t0 = time.monotonic()
        with self.assertRaises(base._AnswerTooLong):
            base._read_in_time(client, "https://model/x", {}, {}, answer_seconds=0.3)
        self.assertLess(time.monotonic() - t0, 2.0)
        client.close.assert_called_once()
        gate.set()

    def test_a_body_that_arrives_in_time_is_returned_whole(self):
        gate = threading.Event()
        gate.set()
        client = mock.Mock()
        client.stream.return_value = _Stream(gate)
        status, body = base._read_in_time(client, "https://model/x", {}, {}, answer_seconds=2)
        self.assertEqual((status, body), (200, b"{}"))

    def test_the_readers_error_reaches_the_caller(self):
        client = mock.Mock()
        client.stream.side_effect = RuntimeError("down")
        with self.assertRaises(RuntimeError):
            base._read_in_time(client, "https://model/x", {}, {}, answer_seconds=2)
