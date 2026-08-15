"""
The local kiosk server.

It is the loopback boundary: the browser asks it what to play and fetches
media from it, and nothing sensitive crosses. These pin that /state
reflects the live schedule, that media is only served when present and
checksum-valid, and that a path-traversal attempt is a clean 404.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from adnova_player.cache import MediaCache
from adnova_player.manifest import Manifest
from adnova_player.schedule import Schedule
from adnova_player.server import build_app

NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def make_manifest(checksum: str) -> Manifest:
    return Manifest.parse({
        "contract_version": "player_manifest.v1",
        "stand_id": 3,
        "schedule_version": 9,
        "server_time": NOW.isoformat(),
        "window": {"from": NOW.isoformat(), "to": (NOW + timedelta(hours=1)).isoformat()},
        "slots": [{
            "slot_id": 1,
            "ad_id": 10,
            "starts_at": (NOW - timedelta(minutes=1)).isoformat(),
            "ends_at": (NOW + timedelta(minutes=1)).isoformat(),
            "duration_seconds": 120,
            "priority": "manual",
            "media": {"url": "https://x/1", "type": "image", "checksum_sha256": checksum},
        }],
    })


def client_for(tmp_path, present: dict[str, bytes], schedule: Schedule):
    cache = MediaCache(tmp_path / "media")
    for checksum, body in present.items():
        cache.path_for(checksum).write_bytes(body)
    app = build_app(cache, current_schedule=lambda: schedule, now=lambda: NOW)
    return TestClient(app), cache


def test_the_kiosk_page_is_self_contained(tmp_path):
    client, _ = client_for(tmp_path, {}, Schedule(None, MediaCache(tmp_path / "m")))
    html = client.get("/").text
    # No external resources — it must work with no network.
    assert "http://" not in html.replace("http://127.0.0.1", "")
    assert "https://" not in html
    assert "<video" in html or "makeVideo" in html


def test_state_reports_a_cached_slot(tmp_path):
    body = b"an image"
    csum = sha(body)
    client, _ = client_for(tmp_path, {csum: body}, make_schedule(tmp_path, {csum: body}))

    state = client.get("/state").json()

    assert state["slot_id"] == 1
    assert state["src"] == f"/media/{csum}"
    assert state["is_fallback"] is False
    assert state["schedule_version"] == 9


def test_state_falls_back_when_nothing_is_cached(tmp_path):
    body = b"missing"
    csum = sha(body)
    # Manifest references the file, but the cache does not have it.
    schedule = Schedule(make_manifest(csum), MediaCache(tmp_path / "empty"))
    app = build_app(MediaCache(tmp_path / "empty"), lambda: schedule, now=lambda: NOW)
    state = TestClient(app).get("/state").json()

    assert state["is_fallback"] is True


def test_media_is_served_when_present(tmp_path):
    body = b"pixels"
    csum = sha(body)
    client, _ = client_for(tmp_path, {csum: body}, Schedule(None, MediaCache(tmp_path / "m")))

    res = client.get(f"/media/{csum}")

    assert res.status_code == 200
    assert res.content == body


def test_an_unknown_checksum_is_404(tmp_path):
    client, _ = client_for(tmp_path, {}, Schedule(None, MediaCache(tmp_path / "m")))
    assert client.get(f"/media/{'a' * 64}").status_code == 404


def test_a_path_traversal_attempt_is_404(tmp_path):
    client, _ = client_for(tmp_path, {}, Schedule(None, MediaCache(tmp_path / "m")))
    # Not 64 hex chars → rejected before any filesystem lookup.
    assert client.get("/media/..%2f..%2f..%2fetc%2fpasswd").status_code == 404
    assert client.get("/media/notahexchecksum").status_code == 404


def test_a_tampered_cache_file_is_not_served(tmp_path):
    csum = sha(b"genuine")
    cache = MediaCache(tmp_path / "media")
    cache.path_for(csum).write_bytes(b"tampered")  # bytes no longer match the name
    app = build_app(cache, lambda: Schedule(None, cache), now=lambda: NOW)

    assert TestClient(app).get(f"/media/{csum}").status_code == 404


# ── helper ───────────────────────────────────────────────────────────────


def make_schedule(tmp_path, present: dict[str, bytes]) -> Schedule:
    cache = MediaCache(tmp_path / "media")
    for checksum, body in present.items():
        cache.path_for(checksum).write_bytes(body)
    csum = next(iter(present))
    return Schedule(make_manifest(csum), cache)
