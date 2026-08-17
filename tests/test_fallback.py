"""
The gap-filler loop.

The screen is never black. These pin the whole chain: the manifest names
a fallback media, the device downloads and verifies it like any ad, and
the schedule loops it whenever nothing else can play — degrading to the
calm built-in filler at every stage where the chosen media is absent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from adnova_player.cache import MediaCache
from adnova_player.manifest import Manifest
from adnova_player.schedule import FALLBACK, Schedule, fallback_item

NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


class FakeCache(MediaCache):
    def __init__(self, present):
        self._present = set(present)

    def has(self, checksum):
        return checksum in self._present

    def is_playable(self, checksum):
        # A stub has no decode probe; present means playable.
        return checksum in self._present

    def local_url_path(self, checksum):
        return f"/media/{checksum}"


def manifest_with_fallback(fallback: dict | None) -> Manifest:
    raw = {
        "contract_version": "player_manifest.v1",
        "stand_id": 3,
        "schedule_version": 4,
        "server_time": NOW.isoformat(),
        "window": {"from": NOW.isoformat(), "to": (NOW + timedelta(hours=1)).isoformat()},
        "slots": [],
    }
    if fallback is not None:
        raw["fallback"] = fallback
    return Manifest.parse(raw)


def test_a_manifest_without_a_fallback_uses_the_builtin_filler():
    m = manifest_with_fallback(None)
    assert m.fallback_media is None
    assert fallback_item(m.fallback_media, FakeCache([])) is FALLBACK


def test_a_bundled_loop_block_with_no_media_is_the_builtin_filler():
    m = manifest_with_fallback({"mode": "bundled_loop", "media_url": None, "checksum_sha256": None})
    assert m.fallback_media is None


def test_a_fallback_media_is_parsed():
    m = manifest_with_fallback({
        "mode": "media_loop",
        "media_url": "https://x/fallback",
        "checksum_sha256": "c" * 64,
        "type": "video",
    })
    assert m.fallback_media is not None
    assert m.fallback_media.type == "video"
    assert m.fallback_media.checksum_sha256 == "c" * 64


def test_the_fallback_loops_when_its_media_is_cached():
    m = manifest_with_fallback({
        "mode": "media_loop",
        "media_url": "https://x/fallback",
        "checksum_sha256": "d" * 64,
        "type": "video",
    })
    cache = FakeCache({"d" * 64})

    item = fallback_item(m.fallback_media, cache)

    assert item.priority == "fallback"
    assert item.is_fallback
    assert item.kind == "video"
    assert item.loop is True
    assert item.local_src == "/media/{}".format("d" * 64)
    assert item.muted is True  # a filler is never an ad


def test_an_uncached_fallback_degrades_to_the_builtin_filler():
    # Named but not yet downloaded — the screen still shows something.
    m = manifest_with_fallback({
        "mode": "media_loop",
        "media_url": "https://x/fallback",
        "checksum_sha256": "e" * 64,
        "type": "image",
    })
    assert fallback_item(m.fallback_media, FakeCache([])) is FALLBACK


def test_a_gap_in_the_schedule_shows_the_fallback_loop():
    m = manifest_with_fallback({
        "mode": "media_loop",
        "media_url": "https://x/fallback",
        "checksum_sha256": "f" * 64,
        "type": "image",
    })
    cache = FakeCache({"f" * 64})
    loop = fallback_item(m.fallback_media, cache)
    schedule = Schedule(m, cache, fallback=loop)

    # No slots at all → the loop plays, not black.
    item = schedule.now_playing(NOW)
    assert item.loop is True
    assert item.local_src == "/media/{}".format("f" * 64)
