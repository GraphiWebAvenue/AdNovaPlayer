"""
The on-site admin page as a security surface.

Before this, the only authenticated page a person standing in the shop could
reach accepted unlimited password guesses and left no trace of any of them.
These tests pin the two halves of the fix: the guessing stops, and every
attempt — successful or not — reaches the forensic trail.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from adnova_player import event_log
from adnova_player.admin import LOCKOUT_THRESHOLD, attach_admin, hash_password
from adnova_player.config import load

PASSWORD = "correct horse battery staple"


def app_with(tmp_path):
    config = load({
        "ADNOVA_STAND_ID": "3",
        "ADNOVA_STAND_KEY": "a" * 64,
        "ADNOVA_ADMIN_USER": "tech",
        "ADNOVA_ADMIN_PASSWORD_HASH": hash_password(PASSWORD),
        "ADNOVA_CACHE_DIR": str(tmp_path),
    })
    log = event_log.init(tmp_path / "events.json")

    app = FastAPI()
    attach_admin(
        app,
        config,
        status=lambda: {"stand_id": 3, "online": True, "warnings": []},
        on_action=lambda name: name == "refetch",
    )
    return TestClient(app), log


def codes(log):
    return [e.code for e in log.take_batch(limit=500)]


def test_a_failed_login_is_recorded_as_a_security_event(tmp_path):
    client, log = app_with(tmp_path)

    client.get("/admin", auth=("tech", "guess"))

    events = [e for e in log.take_batch() if e.code == "admin.auth.failed"]
    assert len(events) == 1
    assert events[0].sev == "security"
    # The username guessed is useful; the password guessed must never be kept.
    assert "tech" in events[0].detail
    assert "guess" not in events[0].detail


def test_a_successful_login_is_recorded_too(tmp_path):
    """After an incident the question is who got in, not only who failed."""
    client, log = app_with(tmp_path)

    assert client.get("/admin", auth=("tech", PASSWORD)).status_code == 200
    assert "admin.auth.ok" in codes(log)


def test_repeated_guessing_locks_the_address_out(tmp_path):
    client, log = app_with(tmp_path)

    for _ in range(LOCKOUT_THRESHOLD):
        assert client.get("/admin", auth=("tech", "guess")).status_code == 401

    # Locked: even the right password is refused while the lock stands, which
    # is the point — the attacker must not learn that they found it.
    response = client.get("/admin", auth=("tech", PASSWORD))
    assert response.status_code == 429
    assert "Retry-After" in response.headers

    recorded = codes(log)
    assert "admin.auth.lockout" in recorded
    assert "admin.auth.locked" in recorded


def test_a_successful_login_clears_the_failure_count(tmp_path):
    """A technician who mistypes twice must not be locked out later."""
    client, _ = app_with(tmp_path)

    for _ in range(LOCKOUT_THRESHOLD - 1):
        client.get("/admin", auth=("tech", "typo"))
    assert client.get("/admin", auth=("tech", PASSWORD)).status_code == 200

    for _ in range(LOCKOUT_THRESHOLD - 1):
        client.get("/admin", auth=("tech", "typo"))
    assert client.get("/admin", auth=("tech", PASSWORD)).status_code == 200


def test_an_action_is_recorded_with_its_outcome(tmp_path):
    client, log = app_with(tmp_path)

    client.post("/admin/action/refetch", auth=("tech", PASSWORD))
    client.post("/admin/action/rm-rf", auth=("tech", PASSWORD))

    recorded = codes(log)
    assert "admin.action" in recorded
    assert "admin.action.rejected" in recorded


def test_the_events_endpoint_reports_the_chain(tmp_path):
    client, log = app_with(tmp_path)
    log.record("service.started", "warn", "Player up")

    body = client.get("/admin/events", auth=("tech", PASSWORD)).json()
    assert body["chain_ok"] is True
    assert body["sequence"] >= 1
    assert any(e["code"] == "service.started" for e in body["events"])


def test_the_events_endpoint_needs_the_login(tmp_path):
    client, _ = app_with(tmp_path)
    assert client.get("/admin/events").status_code == 401
