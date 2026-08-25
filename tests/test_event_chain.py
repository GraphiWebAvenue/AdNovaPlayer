"""
The properties that make the event log evidence rather than a log file.

Each test here stands for a claim made to an operator after an incident:
that the order is real, that an edit is detectable, that a flood cannot bury
a security event, and that a credential never reached Dashboard.
"""

from __future__ import annotations

import json

from adnova_player import event_log
from adnova_player.event_log import FLOOD_BURST, FLOOD_WINDOW_SECONDS, EventLog


def test_sequence_survives_a_drained_ring(tmp_path):
    """
    Acking every event must not reset the count.

    This is what makes a gap detectable at Dashboard: the numbers keep
    climbing across shipments, restarts and re-installs, so a missing range
    is missing rather than merely unseen.
    """
    log = EventLog(tmp_path / "events.json")
    log.record("a")
    log.record("b")
    log.ack(log.take_batch())
    assert log.pending() == 0

    reborn = EventLog(tmp_path / "events.json")
    reborn.record("c")
    assert reborn.take_batch()[0].seq == 3
    assert reborn.boot_id == log.boot_id


def test_the_chain_links_each_event_to_the_last(tmp_path):
    log = EventLog(tmp_path / "events.json")
    log.record("first")
    log.record("second")
    log.record("third")

    events = log.take_batch()
    assert events[0].prev is None
    assert events[1].prev == events[0].hash
    assert events[2].prev == events[1].hash
    assert log.verify() == (True, None)


def test_an_edited_archive_line_breaks_verification(tmp_path):
    """Editing a recorded event must be detectable on the device itself."""
    log = EventLog(tmp_path / "events.json")
    log.record("admin.auth.failed", "security", "Failed login from 10.0.0.9")
    log.record("admin.auth.ok", "warn", "Admin login from 10.0.0.9")

    archive = tmp_path / "events-archive.jsonl"
    rows = [json.loads(line) for line in archive.read_text(encoding="utf-8").splitlines()]
    rows[0]["detail"] = "nothing happened"
    archive.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )

    ok, broken_at = log.verify()
    assert ok is False
    assert broken_at == "1"


def test_a_removed_archive_line_breaks_verification(tmp_path):
    log = EventLog(tmp_path / "events.json")
    for index in range(3):
        log.record(f"event.{index}")

    archive = tmp_path / "events-archive.jsonl"
    lines = archive.read_text(encoding="utf-8").splitlines()
    archive.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")

    ok, broken_at = log.verify()
    assert ok is False
    assert broken_at == "3"


def test_a_flood_is_collapsed_but_never_silent(tmp_path):
    """A repeated code is capped, and the tally is carried into the next window."""
    clock = iter([0.0] * 400)
    log = EventLog(tmp_path / "events.json", clock=lambda: next(clock, 0.0))

    for _ in range(FLOOD_BURST + 25):
        log.record("media.download_failed", "warn", "timeout")

    codes = [e.code for e in log.take_batch()]
    assert len(codes) == FLOOD_BURST

    # A later window reports what it swallowed rather than losing it.
    later = FLOOD_WINDOW_SECONDS + 1.0
    log._monotonic = lambda: later
    log.record("media.download_failed", "warn", "timeout")
    details = [e.detail for e in log.take_batch()]
    assert any(d and "25 suppressed" in d for d in details)


def test_security_events_are_never_suppressed(tmp_path):
    """The point of a flood may be to bury the one event that matters."""
    log = EventLog(tmp_path / "events.json", clock=lambda: 0.0)

    for _ in range(FLOOD_BURST + 40):
        log.record("admin.auth.failed", "security", "wrong password")

    assert len(log.take_batch(limit=500)) == FLOOD_BURST + 40


def test_credentials_are_redacted_before_they_are_written(tmp_path):
    """
    A detail string is assembled from whatever the caller had to hand, so the
    log itself must be the thing that refuses to write a secret down.
    """
    log = EventLog(tmp_path / "events.json")
    key = "b" * 64
    log.record("api.auth.rejected", "security", f"stand_key={key} token: hunter2secret")

    detail = log.take_batch()[0].detail
    assert key not in detail
    assert "hunter2secret" not in detail
    assert "key=***" in detail


def test_the_process_wide_log_buffers_until_it_exists(tmp_path):
    """
    The most interesting minute of a device's life is the one before its
    config loaded, so records made before init() must not be dropped.
    """
    event_log.reset_for_tests()
    event_log.record("service.boot", "warn", "Process starting")
    event_log.record("service.config_error", "error", "ADNOVA_STAND_KEY is required")

    log = event_log.init(tmp_path / "events.json")
    codes = [e.code for e in log.take_batch()]
    assert codes == ["service.boot", "service.config_error"]


def test_a_legacy_bare_list_file_is_read_and_chained_onward(tmp_path):
    """A device upgrading from v1.6.0 keeps its pending events."""
    path = tmp_path / "events.json"
    path.write_text(
        json.dumps([
            {"event_id": "old1", "at": "2026-08-01T00:00:00+00:00",
             "sev": "security", "code": "manifest.refused", "detail": "bad sig"}
        ]),
        encoding="utf-8",
    )

    log = EventLog(path)
    assert log.pending() == 1
    log.record("service.started", "warn", "Player up")
    assert [e.code for e in log.take_batch()] == ["manifest.refused", "service.started"]
