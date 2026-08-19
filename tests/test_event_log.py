"""The bounded, disk-backed event ring shipped to Dashboard."""

from adnova_player.event_log import EventLog


def test_records_and_survives_a_reboot(tmp_path):
    log = EventLog(tmp_path / "events.json")
    log.record("manifest.refused", "security", "bad signature")

    assert log.pending() == 1
    reborn = EventLog(tmp_path / "events.json")
    e = reborn.take_batch()[0]
    assert e.code == "manifest.refused"
    assert e.sev == "security"
    assert e.detail == "bad signature"


def test_ack_removes_only_the_uploaded_events(tmp_path):
    log = EventLog(tmp_path / "events.json")
    log.record("a")
    log.record("b")

    log.ack(log.take_batch()[:1])

    assert log.pending() == 1
    assert log.take_batch()[0].code == "b"


def test_the_ring_is_bounded(tmp_path):
    log = EventLog(tmp_path / "e.json", max_events=3)
    for i in range(5):
        log.record(f"c{i}")

    assert log.pending() == 3  # oldest dropped
