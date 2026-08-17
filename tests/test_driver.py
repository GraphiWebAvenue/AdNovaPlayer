"""
The mpv driver's freeze detector.

The driver (ops/adnova-mpv-driver.py) runs under the system python with no
venv, so it is standard-library only and lives outside the package. It loads
cleanly here by path, which lets the pure decision core — FreezeDetector — be
tested without a Pi, mpv, or a display. The IPC read and the mpv relaunch
around it are thin I/O that only a real device exercises.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_DRIVER = Path(__file__).resolve().parents[1] / "ops" / "adnova-mpv-driver.py"


def _load_driver():
    spec = importlib.util.spec_from_file_location("adnova_mpv_driver", _DRIVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


driver = _load_driver()
FreezeDetector = driver.FreezeDetector


def test_display_record_shapes_the_snapshot():
    rec = driver.display_record(
        {"src": "/media/abc", "slot_id": 7, "kind": "video"},
        time_pos=12.3456, playing=True, freezes=2,
    )
    assert rec["src"] == "/media/abc"
    assert rec["slot_id"] == 7
    assert rec["kind"] == "video"
    assert rec["time_pos"] == 12.346          # rounded, not the raw float
    assert rec["playing"] is True
    assert rec["freeze_recoveries"] == 2


def test_display_record_tolerates_no_state_and_no_clock():
    rec = driver.display_record(None, time_pos=None, playing=False, freezes=0)
    assert rec["src"] is None
    assert rec["slot_id"] is None
    assert rec["time_pos"] is None
    assert rec["playing"] is False
    assert rec["freeze_recoveries"] == 0


def test_an_advancing_video_never_trips():
    d = FreezeDetector(freeze_seconds=10)
    # The clock moves a second per reading; nothing is ever flagged.
    for i in range(30):
        assert d.observe(now=float(i), is_video=True, playing=True, time_pos=i * 0.9) is False


def test_a_frozen_video_trips_after_the_window():
    d = FreezeDetector(freeze_seconds=10)
    # First reading anchors the position at t=0.
    assert d.observe(now=0.0, is_video=True, playing=True, time_pos=5.0) is False
    # Same position all the way to just before the window: still patient.
    assert d.observe(now=9.9, is_video=True, playing=True, time_pos=5.0) is False
    # Past the window on the stuck clock: recover.
    assert d.observe(now=10.0, is_video=True, playing=True, time_pos=5.0) is True


def test_an_image_is_exempt():
    d = FreezeDetector(freeze_seconds=5)
    # A still legitimately never advances; it must never be called frozen,
    # even long past the window and with no clock at all.
    for t in range(0, 60, 3):
        assert d.observe(now=float(t), is_video=False, playing=True, time_pos=None) is False


def test_a_paused_or_idle_video_is_exempt_and_resets():
    d = FreezeDetector(freeze_seconds=10)
    d.observe(now=0.0, is_video=True, playing=True, time_pos=2.0)   # anchor
    # mpv goes idle/paused: not "playing", so the freeze clock is abandoned.
    assert d.observe(now=20.0, is_video=True, playing=False, time_pos=2.0) is False
    # Coming back, the stall must be timed afresh — one reading does not trip.
    assert d.observe(now=21.0, is_video=True, playing=True, time_pos=2.0) is False
    assert d.observe(now=25.0, is_video=True, playing=True, time_pos=2.0) is False


def test_an_unreadable_clock_never_trips():
    d = FreezeDetector(freeze_seconds=3)
    # time_pos is None (a flaky IPC read); that is "cannot judge", not frozen.
    for t in range(0, 20):
        assert d.observe(now=float(t), is_video=True, playing=True, time_pos=None) is False


def test_a_loop_wrap_counts_as_movement():
    d = FreezeDetector(freeze_seconds=5)
    assert d.observe(now=0.0, is_video=True, playing=True, time_pos=9.8) is False
    # The clip looped: the clock jumped back to near zero — that is progress,
    # so the freeze timer restarts rather than seeing a stuck position.
    assert d.observe(now=1.0, is_video=True, playing=True, time_pos=0.1) is False
    assert d.observe(now=9.0, is_video=True, playing=True, time_pos=0.9) is False


def test_recovery_resets_so_the_next_stall_is_timed_afresh():
    d = FreezeDetector(freeze_seconds=5)
    d.observe(now=0.0, is_video=True, playing=True, time_pos=1.0)   # anchor
    assert d.observe(now=5.0, is_video=True, playing=True, time_pos=1.0) is True
    # Immediately after firing, one more stuck reading must not fire again —
    # the caller has just recovered and the clock is being judged from scratch.
    assert d.observe(now=5.1, is_video=True, playing=True, time_pos=1.0) is False
    assert d.observe(now=6.0, is_video=True, playing=True, time_pos=1.0) is False
