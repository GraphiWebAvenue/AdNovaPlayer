"""
Verified-capture proof hashing. capture_proof turns a grabbed frame into a
tiny billing-grade record, and never raises into the playback loop.
"""

from __future__ import annotations

import hashlib

from adnova_player.capture import capture_proof


class _FakeCapture:
    def __init__(self, frame: bytes | None) -> None:
        self._frame = frame

    def grab(self) -> bytes | None:
        return self._frame


def test_proof_is_the_sha256_of_the_frame():
    proof = capture_proof(_FakeCapture(b"pixels"))
    assert proof is not None
    assert proof["kind"] == "screenshot"
    assert proof["hash"] == hashlib.sha256(b"pixels").hexdigest()
    assert "captured_at" in proof


def test_no_proof_when_no_frame():
    assert capture_proof(_FakeCapture(None)) is None


def test_a_grab_error_never_raises():
    class _Boom:
        def grab(self):
            raise RuntimeError("no grabber on this box")

    assert capture_proof(_Boom()) is None
