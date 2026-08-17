"""
What plays now.

The one rule under test everywhere here: never a gap. Whatever is wrong —
no manifest, no slot, uncached media — `now_playing` returns the fallback,
never None, so the screen is never black.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from adnova_player.cache import MediaCache
from adnova_player.manifest import Manifest
from adnova_player.schedule import Schedule

NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def manifest_with(slots: list[dict]) -> Manifest:
    raw = {
        "contract_version": "player_manifest.v1",
        "stand_id": 3,
        "schedule_version": 7,
        "server_time": iso(NOW),
        "window": {"from": iso(NOW), "to": iso(NOW + timedelta(hours=72))},
        "slots": slots,
    }
    return Manifest.parse(raw)


def slot(sid, start, end, checksum, kind="image", muted=True):
    return {
        "slot_id": sid,
        "ad_id": sid * 10,
        "starts_at": iso(start),
        "ends_at": iso(end),
        "duration_seconds": int((end - start).total_seconds()),
        "priority": "manual",
        "muted": muted,
        "media": {"url": f"https://x/{sid}", "type": kind, "checksum_sha256": checksum},
    }


class FakeCache(MediaCache):
    """A cache that reports a fixed set of checksums as present."""

    def __init__(self, present: set[str]):
        self._present = present

    def has(self, checksum: str) -> bool:
        return checksum in self._present

    def is_playable(self, checksum: str) -> bool:
        # A stub has no decode probe; present means playable.
        return checksum in self._present

    def local_url_path(self, checksum: str) -> str:
        return f"/media/{checksum}"


def test_no_manifest_yields_the_fallback():
    schedule = Schedule(None, FakeCache(set()))

    item = schedule.now_playing(NOW)

    assert item.is_fallback
    assert item.slot_id == -1


def test_a_covered_cached_slot_plays():
    csum = "a" * 64
    m = manifest_with([slot(1, NOW - timedelta(minutes=1), NOW + timedelta(minutes=1), csum)])
    schedule = Schedule(m, FakeCache({csum}))

    item = schedule.now_playing(NOW)

    assert not item.is_fallback
    assert item.slot_id == 1
    assert item.local_src == f"/media/{csum}"


def test_a_covered_but_uncached_slot_falls_back():
    # The plan says play it, but its bytes have not arrived. The fallback
    # covers the gap rather than the screen going black.
    csum = "b" * 64
    m = manifest_with([slot(1, NOW - timedelta(minutes=1), NOW + timedelta(minutes=1), csum)])
    schedule = Schedule(m, FakeCache(set()))  # nothing cached

    assert schedule.now_playing(NOW).is_fallback


def test_a_gap_between_slots_falls_back():
    csum = "c" * 64
    m = manifest_with([slot(1, NOW + timedelta(hours=1), NOW + timedelta(hours=2), csum)])
    schedule = Schedule(m, FakeCache({csum}))

    # Now is before the only slot starts.
    assert schedule.now_playing(NOW).is_fallback


def test_video_carries_sound_only_when_not_muted():
    csum = "d" * 64
    m = manifest_with([
        slot(1, NOW - timedelta(minutes=1), NOW + timedelta(minutes=1), csum,
             kind="video", muted=False)
    ])
    schedule = Schedule(m, FakeCache({csum}))

    item = schedule.now_playing(NOW)
    assert item.kind == "video"
    assert item.muted is False


def test_an_image_is_always_silent_even_if_marked_unmuted():
    csum = "e" * 64
    m = manifest_with([
        slot(1, NOW - timedelta(minutes=1), NOW + timedelta(minutes=1), csum,
             kind="image", muted=False)
    ])
    schedule = Schedule(m, FakeCache({csum}))

    assert schedule.now_playing(NOW).muted is True


def test_next_change_is_the_current_slot_ending():
    csum = "f" * 64
    end = NOW + timedelta(minutes=5)
    m = manifest_with([slot(1, NOW - timedelta(minutes=1), end, csum)])
    schedule = Schedule(m, FakeCache({csum}))

    assert schedule.next_change_after(NOW) == end


def test_preload_checksums_lists_the_upcoming_media():
    a, b = "a" * 64, "b" * 64
    m = manifest_with([
        slot(1, NOW, NOW + timedelta(minutes=5), a),
        slot(2, NOW + timedelta(minutes=5), NOW + timedelta(minutes=10), b),
    ])
    schedule = Schedule(m, FakeCache({a, b}))

    assert schedule.preload_checksums(NOW) == {a, b}
