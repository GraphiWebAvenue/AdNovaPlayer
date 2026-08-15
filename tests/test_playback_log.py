"""
The billing buffer.

Two properties matter: a play is never lost while the network is down
(it survives on disk across a reboot), and a play is never billed twice
(ack is idempotent, keyed the same way the server dedupes). The ring is
bounded so a long outage cannot fill the card.
"""

from __future__ import annotations

from adnova_player.playback_log import Entry, PlaybackLog


def entry(slot: int, at: str = "2026-08-15T12:00:00+00:00") -> Entry:
    return Entry(
        slot_id=slot,
        ad_id=slot * 10,
        started_at=at,
        ended_at=None,
        outcome="played",
    )


def test_a_recorded_play_survives_a_reboot(tmp_path):
    path = tmp_path / "playback.json"
    PlaybackLog(path).record(entry(1))

    # A fresh instance, as if the process restarted.
    reborn = PlaybackLog(path)
    assert reborn.pending() == 1
    assert reborn.take_batch()[0].slot_id == 1


def test_take_does_not_remove_so_a_failed_upload_is_safe(tmp_path):
    log = PlaybackLog(tmp_path / "p.json")
    log.record(entry(1))

    batch = log.take_batch()
    # Upload "failed": we never acked.
    assert log.pending() == 1
    assert batch[0].slot_id == 1


def test_ack_removes_only_the_acknowledged(tmp_path):
    log = PlaybackLog(tmp_path / "p.json")
    log.record(entry(1))
    log.record(entry(2))

    log.ack([entry(1)])

    remaining = log.take_batch()
    assert len(remaining) == 1
    assert remaining[0].slot_id == 2


def test_ack_is_idempotent(tmp_path):
    log = PlaybackLog(tmp_path / "p.json")
    log.record(entry(1))

    log.ack([entry(1)])
    log.ack([entry(1)])  # a duplicate ack, e.g. a retried response

    assert log.pending() == 0


def test_the_ring_is_bounded(tmp_path):
    log = PlaybackLog(tmp_path / "p.json", max_entries=3)
    for i in range(10):
        log.record(entry(i, at=f"2026-08-15T12:00:{i:02d}+00:00"))

    # Only the newest three survive; the oldest were dropped.
    kept = {e.slot_id for e in log.take_batch()}
    assert kept == {7, 8, 9}


def test_an_unreadable_log_is_ignored_not_fatal(tmp_path):
    path = tmp_path / "p.json"
    path.write_text("this is not json")

    # A corrupt log must not stop the player; it starts empty.
    log = PlaybackLog(path)
    assert log.pending() == 0
