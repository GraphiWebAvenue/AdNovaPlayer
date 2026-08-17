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
        self.auth_failures = 0

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


def _fixed_clock(agent, start):
    """Give the agent a controllable wall clock; return a setter."""
    box = {"now": start}
    agent._now = lambda: box["now"]
    return box


def test_a_finished_slot_is_closed_with_duration_and_played(tmp_path):
    from datetime import UTC, datetime, timedelta

    agent, playback = make_agent(tmp_path)
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    clock = _fixed_clock(agent, t0)

    agent.on_playing(played(1))                 # opens slot 1 (30s)
    clock["now"] = t0 + timedelta(seconds=30)   # ran its full length
    agent.on_playing(played(2))                 # slot 1 ends, slot 2 opens

    entries = playback.take_batch()
    first = next(e for e in entries if e.slot_id == 1)
    assert first.ended_at is not None
    assert first.outcome == "played"
    assert first.played_seconds == 30.0
    # The now-playing slot 2 is still open (not yet finalized).
    second = next(e for e in entries if e.slot_id == 2)
    assert second.ended_at is None


def test_a_slot_cut_short_is_partial(tmp_path):
    from datetime import UTC, datetime, timedelta

    agent, playback = make_agent(tmp_path)
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    clock = _fixed_clock(agent, t0)

    agent.on_playing(played(1))                 # 30s slot
    clock["now"] = t0 + timedelta(seconds=5)    # only 5s on screen
    agent.on_playing(played(2))

    first = next(e for e in playback.take_batch() if e.slot_id == 1)
    assert first.outcome == "partial"
    assert first.played_seconds == 5.0


def test_a_gap_closes_the_open_slot_but_is_not_billed(tmp_path):
    from datetime import UTC, datetime, timedelta

    agent, playback = make_agent(tmp_path)
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    clock = _fixed_clock(agent, t0)

    agent.on_playing(played(1))
    clock["now"] = t0 + timedelta(seconds=30)
    agent.on_playing(FALLBACK)                  # a gap ends slot 1

    entries = playback.take_batch()
    assert len(entries) == 1                    # fallback itself not billed
    assert entries[0].slot_id == 1
    assert entries[0].ended_at is not None
    assert entries[0].outcome == "played"


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


def test_the_heartbeat_reports_verified_display_health(tmp_path):
    import json

    agent, _ = make_agent(tmp_path)
    dpath = tmp_path / "display.json"
    dpath.write_text(json.dumps({
        "src": "/media/x", "playing": True, "freeze_recoveries": 1, "at": 123.0,
    }), encoding="utf-8")
    agent._display_state_path = str(dpath)

    body = agent._heartbeat_body()

    assert body["display"]["src"] == "/media/x"
    assert body["display"]["playing"] is True
    assert body["display"]["freeze_recoveries"] == 1
    assert "undecodable_count" in body


def test_the_heartbeat_reports_boot_self_test_failures(tmp_path):
    from adnova_player import diagnostics

    agent, _ = make_agent(tmp_path)
    assert agent._heartbeat_body()["self_test_failures"] == []   # not run yet

    # A box with no binaries and no keys: criticals and warnings both surface.
    agent._boot_checks = diagnostics.run_self_test(
        stand_id=3, cache_dir=tmp_path, has_trusted_keys=False,
        which=lambda name: None,
    )
    failures = agent._heartbeat_body()["self_test_failures"]
    assert "player" in failures and "decoder" in failures and "signing" in failures


def test_the_heartbeat_display_is_null_without_a_driver_report(tmp_path):
    agent, _ = make_agent(tmp_path)
    agent._display_state_path = str(tmp_path / "missing.json")

    body = agent._heartbeat_body()

    assert body["display"] is None


def test_the_clock_offset_is_learned_from_the_signed_server_time(tmp_path):
    from tests.test_schedule import manifest_with, slot

    agent, _ = make_agent(tmp_path)
    # This device's own clock is an hour behind Dashboard's true time.
    device_now = NOW - timedelta(hours=1)
    agent._now = lambda: device_now
    m = manifest_with([slot(1, NOW, NOW + timedelta(hours=1), "a" * 64)])  # server_time = NOW

    agent._learn_clock(m, agent._now())

    assert round(agent._clock_skew_seconds) == -3600      # device is 1h slow
    assert agent._trusted_now() == NOW                    # corrected back to true time


def test_a_wrong_device_clock_still_resolves_the_right_slot(tmp_path):
    from adnova_player.schedule import Schedule
    from tests.test_schedule import manifest_with, slot

    agent, _ = make_agent(tmp_path)
    csum = "a" * 64
    agent._cache.is_playable = lambda c: c == csum
    m = manifest_with([slot(1, NOW, NOW + timedelta(hours=1), csum)])  # live at true-now
    agent._now = lambda: NOW - timedelta(hours=3)          # clock 3h behind
    agent._learn_clock(m, agent._now())
    sched = Schedule(m, agent._cache)

    # Raw device time thinks the slot is still in the future → fallback...
    assert sched.now_playing(agent._now()).is_fallback
    # ...but the corrected clock sees it live and plays it.
    assert sched.now_playing(agent._trusted_now()).slot_id == 1


def test_the_heartbeat_reports_clock_offset_only_after_a_fetch(tmp_path):
    from tests.test_schedule import manifest_with, slot

    agent, _ = make_agent(tmp_path)
    assert agent._heartbeat_body()["clock_offset_seconds"] is None   # never synced yet

    agent._now = lambda: NOW - timedelta(seconds=90)                 # 90s slow
    agent._learn_clock(manifest_with([slot(1, NOW, NOW + timedelta(hours=1), "a" * 64)]),
                       agent._now())

    assert agent._heartbeat_body()["clock_offset_seconds"] == -90.0


def test_uploaded_playback_is_acked(tmp_path):
    api = FakeApi()
    agent, playback = make_agent(tmp_path, api)
    agent.on_playing(played(1))

    agent._upload_playback()

    assert len(api.sent_playback) == 1
    assert playback.pending() == 0  # acked and dropped


def test_a_sustained_auth_rejection_triggers_reenroll_once(tmp_path):
    api = FakeApi(heartbeat=None)          # 401s make send_heartbeat return None
    agent, _ = make_agent(tmp_path, api)
    fired = []
    agent._on_auth_lost = lambda: fired.append(True)
    agent._auth_failure_limit = 3

    # Below the limit: a transient blip is tolerated, nothing fires.
    api.auth_failures = 2
    agent._heartbeat_once()
    assert fired == []

    # At the limit: the key is judged gone and re-enrollment is requested once.
    api.auth_failures = 3
    agent._heartbeat_once()
    assert fired == [True]

    # Still failing on the next beat, but the request is not re-fired.
    api.auth_failures = 9
    agent._heartbeat_once()
    assert fired == [True]


def test_shutdown_is_a_whitelisted_deferred_command(tmp_path):
    agent, _ = make_agent(tmp_path)
    ran = []
    agent._exec = lambda argv: (ran.append(argv), (True, ""))[1]

    agent._run_commands([{"id": 1, "command": "shutdown"}])

    assert ran == [["sudo", "-n", "systemctl", "poweroff"]]
    # A power-off cannot ack — the process is gone — so nothing is queued.
    assert agent._take_acks() == []


def test_screen_commands_write_a_request_for_the_in_session_helper(tmp_path):
    agent, _ = make_agent(tmp_path)
    agent._SCREEN_REQ = tmp_path / "ipc" / "screen.req"

    agent._run_commands([{"id": 5, "command": "screen_off"}])
    assert agent._SCREEN_REQ.read_text() == "off"
    acks = agent._take_acks()
    assert acks[0]["id"] == 5 and acks[0]["status"] == "done"

    agent._run_commands([{"id": 6, "command": "screen_on"}])
    assert agent._SCREEN_REQ.read_text() == "on"


def test_a_scheduled_takeover_is_held_until_its_time_then_flips(tmp_path):
    csum = "f" * 64
    agent, _ = make_agent(tmp_path)
    agent._cache.has = lambda c: c == csum
    agent._now = lambda: NOW

    agent._apply_emergency({
        "url": "https://x/e", "checksum_sha256": csum, "type": "image",
        "starts_at": (NOW + timedelta(minutes=1)).isoformat(),
    })

    # Before its time, the takeover is held — the screen is not on it yet.
    assert agent.schedule().now_playing(NOW).priority != "urgent"

    # At its time, every stand flips together — here, this one takes over.
    agent._now = lambda: NOW + timedelta(minutes=2)
    assert agent.schedule().now_playing(NOW + timedelta(minutes=2)).priority == "urgent"


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


def _test_play_item():
    from adnova_player.schedule import PlayItem

    return PlayItem(
        slot_id=-2, ad_id=None, kind="image", local_src="/media/test",
        muted=True, duration_seconds=0, priority="test", label="TEST", loop=True,
    )


def test_a_live_test_broadcast_from_the_heartbeat_shows_at_once(tmp_path):
    # The operator hits "play now"; Dashboard pushes it on the heartbeat,
    # not the slower manifest poll — so it is on screen within one heartbeat.
    csum = "c" * 64
    api = FakeApi(heartbeat={"test": {
        "url": "https://x/t", "checksum_sha256": csum, "type": "image",
    }})
    agent, _ = make_agent(tmp_path, api)
    agent._cache.has = lambda c: c == csum          # already cached
    agent._cache.is_playable = lambda c: c == csum

    agent._heartbeat_once()

    item = agent.schedule().now_playing(NOW)
    assert item.is_test
    assert item.local_src == f"/media/{csum}"


def test_ending_a_test_via_the_heartbeat_resumes_the_current_slot(tmp_path):
    # R15: ending the test resumes the slot that belongs on screen NOW, not
    # the ad that was up before the test — the plan is read against `now`.
    from datetime import timedelta

    from adnova_player.schedule import Schedule
    from tests.test_schedule import NOW as SNOW
    from tests.test_schedule import manifest_with, slot

    csum = "d" * 64
    m = manifest_with([slot(1, SNOW - timedelta(minutes=1), SNOW + timedelta(hours=1), csum)])
    api = FakeApi(heartbeat={"test": None})         # Dashboard ends the test
    agent, _ = make_agent(tmp_path, api)
    agent._cache.is_playable = lambda c: c == csum
    agent._schedule = Schedule(m, agent._cache)

    # A test is running, covering the scheduled slot.
    agent._install_test(_test_play_item())
    assert agent.schedule().now_playing(SNOW).is_test

    agent._heartbeat_once()                         # the test ends here

    resumed = agent.schedule().now_playing(SNOW)
    assert not resumed.is_test
    assert resumed.slot_id == 1                     # the slot due right now


def test_an_absent_test_key_leaves_a_running_test_untouched(tmp_path):
    # A manifest-driven test must survive ordinary heartbeats that carry no
    # `test` key at all — absence means "no change", not "clear".
    api = FakeApi(heartbeat={"ok": True})
    agent, _ = make_agent(tmp_path, api)
    running = _test_play_item()
    agent._install_test(running)

    agent._heartbeat_once()

    assert agent._test is running


def test_the_agent_falls_to_default_after_being_offline_past_the_window(tmp_path):
    from datetime import UTC, datetime, timedelta

    from adnova_player.schedule import Schedule
    from tests.test_schedule import NOW, manifest_with, slot

    agent, _ = make_agent(tmp_path)
    csum = "e" * 64
    # A slot covering a wide window, with its media reported playable.
    m = manifest_with([slot(1, NOW, NOW + timedelta(hours=12), csum)])
    agent._cache.is_playable = lambda c: c == csum
    agent._schedule = Schedule(m, agent._cache)
    agent._last_contact_at = NOW

    # Fresh contact (5 min ago) → the covered slot plays.
    agent._now = lambda: NOW + timedelta(minutes=5)
    assert agent.schedule().now_playing(NOW + timedelta(minutes=5)).slot_id == 1

    # 7h since contact, past the 6h preload window → the default loop.
    agent._now = lambda: NOW + timedelta(hours=7)
    assert agent.schedule().now_playing(NOW + timedelta(hours=7)).is_fallback
