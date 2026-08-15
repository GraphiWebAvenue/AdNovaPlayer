"""
Manifest parsing and the what-plays-now decision.

These run on x86 in CI, not on a Pi, because none of this touches
hardware — which is the point of keeping the schedule logic separate from
the playback driver. The rules pinned here are the ones that decide
whether a shop window shows the right ad or a black screen.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from adnova_player.manifest import Manifest

NOW = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)


def media(checksum: str = "a" * 64) -> dict:
    return {
        "url": "https://dashboard.example/api/v1/media/1?signature=x",
        "type": "image",
        "checksum_sha256": checksum,
        "bytes": 1024,
    }


def slot(slot_id: int, offset_min: int, duration_s: int = 30, **overrides) -> dict:
    start = NOW + timedelta(minutes=offset_min)
    base = {
        "slot_id": slot_id,
        "ad_id": slot_id * 10,
        "starts_at": start.isoformat(),
        "ends_at": (start + timedelta(seconds=duration_s)).isoformat(),
        "duration_seconds": duration_s,
        "priority": "ai",
        "media": media(f"{slot_id:064d}"),
    }
    base.update(overrides)
    return base


def manifest(slots: list[dict], **overrides) -> dict:
    base = {
        "contract_version": "player_manifest.v1",
        "stand_id": 1,
        "schedule_version": 7,
        "server_time": NOW.isoformat(),
        "window": {
            "from": NOW.isoformat(),
            "to": (NOW + timedelta(hours=72)).isoformat(),
        },
        "slots": slots,
        "preload": {"hours_ahead": 6},
        "poll": {"manifest_seconds": 600, "heartbeat_seconds": 60},
    }
    base.update(overrides)
    return base


# ─── Parsing ────────────────────────────────────────────────────────────────


def test_parses_a_well_formed_manifest():
    m = Manifest.parse(manifest([slot(1, 0), slot(2, 5)]))

    assert m.stand_id == 1
    assert m.schedule_version == 7
    assert len(m.slots) == 2


def test_rejects_a_manifest_from_a_contract_it_does_not_speak():
    """A v2 manifest must not be interpreted as v1 and half-understood."""
    with pytest.raises(ValueError, match="player_manifest.v1"):
        Manifest.parse(manifest([], contract_version="player_manifest.v2"))


def test_one_broken_slot_does_not_lose_the_manifest():
    """Losing a placement is a gap. Losing the manifest is a dark screen."""
    payload = manifest([slot(1, 0), {"slot_id": 2, "broken": True}, slot(3, 10)])

    m = Manifest.parse(payload)

    assert [s.slot_id for s in m.slots] == [1, 3]


def test_slots_are_sorted_even_when_the_server_sends_them_out_of_order():
    m = Manifest.parse(manifest([slot(3, 10), slot(1, 0), slot(2, 5)]))

    assert [s.slot_id for s in m.slots] == [1, 2, 3]


def test_a_naive_timestamp_is_assumed_utc_rather_than_crashing():
    payload = manifest([])
    payload["server_time"] = "2026-08-20T10:00:00"

    m = Manifest.parse(payload)

    assert m.server_time.tzinfo is not None


# ─── What plays now ─────────────────────────────────────────────────────────


def test_finds_the_slot_covering_this_moment():
    m = Manifest.parse(manifest([slot(1, 0, 60), slot(2, 5, 60)]))

    current = m.slot_at(NOW + timedelta(seconds=30))

    assert current is not None
    assert current.slot_id == 1


def test_returns_nothing_in_a_gap():
    m = Manifest.parse(manifest([slot(1, 0, 30), slot(2, 10, 30)]))

    assert m.slot_at(NOW + timedelta(minutes=5)) is None


def test_higher_priority_wins_an_overlap():
    """
    Overlaps should not happen. When a clock skew or a hand-edit race
    produces one, the answer must be deterministic rather than whichever
    slot the loop reached first.
    """
    m = Manifest.parse(
        manifest(
            [
                slot(1, 0, 300, priority="ai"),
                slot(2, 0, 300, priority="urgent"),
            ]
        )
    )

    current = m.slot_at(NOW + timedelta(seconds=10))

    assert current is not None
    assert current.priority == "urgent"


def test_knows_what_comes_next():
    m = Manifest.parse(manifest([slot(1, 0, 30), slot(2, 10, 30)]))

    nxt = m.next_slot_after(NOW + timedelta(seconds=5))

    assert nxt is not None and nxt.slot_id == 2


# ─── Preloading ─────────────────────────────────────────────────────────────


def test_preloads_only_inside_the_horizon():
    m = Manifest.parse(manifest([slot(1, 0), slot(2, 60), slot(3, 60 * 24)]))

    upcoming = m.slots_to_preload(NOW)

    assert {s.slot_id for s in upcoming} == {1, 2}, "the 24h-away slot can wait"


def test_the_same_file_is_only_downloaded_once():
    shared = media("f" * 64)
    m = Manifest.parse(
        manifest([slot(1, 0, media=shared), slot(2, 5, media=shared)])
    )

    assert len(m.slots_to_preload(NOW)) == 1


def test_knows_when_the_plan_has_run_out():
    m = Manifest.parse(manifest([slot(1, 0, 30)]))

    assert m.is_exhausted(NOW + timedelta(hours=1)) is True
    assert m.is_exhausted(NOW) is False


def test_an_empty_manifest_is_exhausted():
    """No slots means nothing to play — fall back rather than wait."""
    assert Manifest.parse(manifest([])).is_exhausted(NOW) is True


# ─── Persistence ────────────────────────────────────────────────────────────


def test_survives_a_save_and_load_round_trip(tmp_path):
    original = Manifest.parse(manifest([slot(1, 0), slot(2, 5)]))
    path = tmp_path / "cache" / "manifest.json"

    original.save(path)
    restored = Manifest.load(path)

    assert restored is not None
    assert restored.schedule_version == original.schedule_version
    assert [s.slot_id for s in restored.slots] == [1, 2]


def test_a_truncated_cache_file_is_ignored_not_fatal(tmp_path):
    """
    A power cut mid-write leaves a partial file, and boot is exactly when
    the device most needs to keep going.
    """
    path = tmp_path / "manifest.json"
    path.write_text('{"contract_version": "player_manif', encoding="utf-8")

    assert Manifest.load(path) is None


def test_a_missing_cache_file_is_not_an_error(tmp_path):
    assert Manifest.load(tmp_path / "never-written.json") is None


def test_the_write_is_atomic(tmp_path):
    """The temp file must not survive; a stray .tmp means a partial write."""
    path = tmp_path / "manifest.json"
    Manifest.parse(manifest([slot(1, 0)])).save(path)

    assert path.exists()
    assert not list(tmp_path.glob("*.tmp"))
    json.loads(path.read_text(encoding="utf-8"))
