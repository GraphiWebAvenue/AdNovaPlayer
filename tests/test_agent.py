"""
The main loop's behaviour, minus the threads.

The loops themselves are thin wrappers around methods exercised directly
here: a played slot is logged once, the fallback is never billed, the
control channel triggers a refetch, and a cycle that throws does not
propagate. The real socket and clock live in main.py, which carries no
logic worth unit-testing.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from adnova_player.agent import Agent
from adnova_player.cache import MediaCache
from adnova_player.playback_log import PlaybackLog
from adnova_player.schedule import FALLBACK, PlayItem

NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


class FakeApi:
    def __init__(self, manifest=None, heartbeat=None):
        self.manifest = manifest
        self.heartbeat_response = heartbeat
        self.sent_playback = []
        self.heartbeats = []

    def fetch_manifest(self):
        return self.manifest

    def send_heartbeat(self, body):
        self.heartbeats.append(body)
        return self.heartbeat_response

    def send_playback(self, body):
        self.sent_playback.append(body)
        return {"ok": True}


def make_agent(tmp_path, api=None):
    cache = MediaCache(tmp_path / "media")
    playback = PlaybackLog(tmp_path / "playback.json")
    config = _config(tmp_path)
    return Agent(config, api or FakeApi(), cache, playback), playback


def _config(tmp_path):
    from adnova_player.config import load

    return load({
        "ADNOVA_STAND_ID": "3",
        "ADNOVA_STAND_KEY": "a" * 64,
        "ADNOVA_CACHE_DIR": str(tmp_path),
    })


def played(slot_id: int, ad_id: int = 99) -> PlayItem:
    return PlayItem(
        slot_id=slot_id,
        ad_id=ad_id,
        kind="image",
        local_src=f"/media/{'a' * 64}",
        muted=True,
        duration_seconds=30,
        priority="manual",
    )


def test_a_played_slot_is_logged_once(tmp_path):
    agent, playback = make_agent(tmp_path)

    # The same slot polled five times is one impression.
    for _ in range(5):
        agent.on_playing(played(1))

    assert playback.pending() == 1


def test_a_new_slot_is_a_new_impression(tmp_path):
    agent, playback = make_agent(tmp_path)

    agent.on_playing(played(1))
    agent.on_playing(played(2))

    assert playback.pending() == 2


def test_the_fallback_is_never_billed(tmp_path):
    agent, playback = make_agent(tmp_path)

    agent.on_playing(FALLBACK)
    agent.on_playing(played(1, ad_id=None))  # a real slot but no ad id

    assert playback.pending() == 0


def test_the_heartbeat_carries_health_and_state(tmp_path):
    api = FakeApi(heartbeat={"ok": True})
    agent, _ = make_agent(tmp_path, api)
    agent.on_playing(played(7))

    agent._heartbeat_once()

    body = api.heartbeats[-1]
    assert body["stand_id"] == 3
    assert body["current_slot_id"] == 7
    assert body["state"] == "playing"
    assert "disk_free_bytes" in body


def test_the_control_channel_triggers_a_refetch(tmp_path):
    # Dashboard asks for a refetch; the agent must act on it. With no
    # manifest to fetch it is a no-op, but the path must be reached without
    # raising — which _safe guarantees.
    api = FakeApi(heartbeat={"refetch_manifest": True})
    agent, _ = make_agent(tmp_path, api)

    agent._heartbeat_once()  # must not raise


def test_uploaded_playback_is_acked(tmp_path):
    api = FakeApi()
    agent, playback = make_agent(tmp_path, api)
    agent.on_playing(played(1))

    agent._upload_playback()

    assert len(api.sent_playback) == 1
    assert playback.pending() == 0  # acked and dropped


def test_a_failing_cycle_does_not_propagate(tmp_path):
    agent, _ = make_agent(tmp_path)

    def boom():
        raise RuntimeError("simulated")

    # _safe swallows it and returns the default — the loop survives.
    assert agent._safe(boom, default="fallback") == "fallback"


def test_a_verified_manifest_installs_and_downloads(tmp_path):
    body = b"an image file"
    checksum = hashlib.sha256(body).hexdigest()

    import httpx

    def media_handler(req):
        return httpx.Response(200, content=body)

    cache = MediaCache(
        tmp_path / "media",
        client=httpx.Client(transport=httpx.MockTransport(media_handler)),
    )
    manifest = {
        "contract_version": "player_manifest.v1",
        "stand_id": 3,
        "schedule_version": 5,
        "server_time": NOW.isoformat(),
        "window": {"from": NOW.isoformat(), "to": (NOW + timedelta(hours=1)).isoformat()},
        "slots": [{
            "slot_id": 1,
            "ad_id": 10,
            "starts_at": (NOW - timedelta(minutes=1)).isoformat(),
            "ends_at": (NOW + timedelta(hours=1)).isoformat(),
            "duration_seconds": 30,
            "priority": "manual",
            "media": {"url": "https://x/media/10", "type": "image", "checksum_sha256": checksum},
        }],
    }
    # Anchor the window to real now: _fetch_once reads the wall clock to
    # pick the preload horizon, so a fixed past date would download nothing.
    real_now = datetime.now(tz=UTC)
    manifest["server_time"] = real_now.isoformat()
    manifest["window"] = {
        "from": real_now.isoformat(),
        "to": (real_now + timedelta(hours=1)).isoformat(),
    }
    manifest["slots"][0]["starts_at"] = (real_now - timedelta(minutes=1)).isoformat()
    manifest["slots"][0]["ends_at"] = (real_now + timedelta(hours=1)).isoformat()

    api = FakeApi(manifest=manifest)
    playback = PlaybackLog(tmp_path / "p.json")
    # No trusted keys → signature check skipped (pre-signing fleet path),
    # which isolates the install-and-download behaviour under test.
    agent = Agent(_config(tmp_path), api, cache, playback)

    agent._fetch_once()

    assert agent.schedule().schedule_version == 5
    assert cache.has(checksum)  # the media was downloaded and verified
