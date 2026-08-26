"""
The display watchdog: noticing a panel that has gone dark.

Everything else in this system watches the brain. This watches the only part
that actually lights the screen — the in-session stack, which starts from a
desktop autostart hook and has no systemd unit, no Restart=always and no
watchdog beneath it. A stand could sit dark for weeks while its heartbeats
kept Dashboard's dot green; these tests pin the escalation that ends that.

The two steps are deliberately far apart in consequence, and the tests treat
them that way: asking for a relaunch is cheap and fires by default, rebooting
a board on a customer's premises is a fleet flag that defaults off and may
happen at most once per process.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from adnova_player.cache import MediaCache
from adnova_player.config import load
from adnova_player.playback_log import PlaybackLog
from adnova_player.schedule import PlayItem

from .test_agent import FakeApi

NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC)


def make_agent(tmp_path, **env):
    """An agent whose display state file and clock the test controls."""
    from adnova_player.agent import Agent

    config = load({
        "ADNOVA_STAND_ID": "3",
        "ADNOVA_STAND_KEY": "a" * 64,
        "ADNOVA_CACHE_DIR": str(tmp_path),
        **env,
    })
    agent = Agent(
        config,
        FakeApi(),
        MediaCache(tmp_path / "media"),
        PlaybackLog(tmp_path / "playback.json"),
    )
    agent._display_state_path = str(tmp_path / "display.json")

    # A stand that is open and has something to play — otherwise a dark panel
    # is correct, not a fault, and the watchdog rightly says nothing.
    agent._current = PlayItem(
        slot_id=1, ad_id=9, kind="image", local_src="/media/x",
        muted=True, duration_seconds=30, priority="manual",
    )
    agent._operating_hours = None
    agent._timezone = "UTC"

    agent._now = lambda: agent._fake_now  # type: ignore[method-assign]
    agent._fake_now = NOW
    return agent


def write_display(agent, *, playing: bool, age_seconds: float = 0.0):
    """Put a driver snapshot on disk, `age_seconds` old."""
    with open(agent._display_state_path, "w", encoding="utf-8") as f:
        json.dump({
            "src": "/media/x",
            "playing": playing,
            "freeze_recoveries": 0,
            "at": datetime.now(tz=UTC).timestamp() - age_seconds,
        }, f)


def advance(agent, seconds: float):
    agent._fake_now = agent._fake_now + timedelta(seconds=seconds)


# ── The quiet cases: nothing is wrong ────────────────────────────────────


def test_a_playing_panel_is_never_touched(tmp_path):
    agent = make_agent(tmp_path)
    write_display(agent, playing=True)

    for _ in range(5):
        agent._check_display_alive()
        advance(agent, 600)

    assert agent._display_bad_since is None
    assert not agent._display_restart_asked


def test_a_stand_with_no_plan_is_not_a_fault(tmp_path):
    """Before the first manifest there is nothing to show. Dark is correct."""
    agent = make_agent(tmp_path)
    agent._current = None

    agent._check_display_alive()
    advance(agent, 3600)
    agent._check_display_alive()

    assert agent._display_bad_since is None


def test_closed_hours_are_not_a_fault(tmp_path):
    """A panel that is off because the shop is shut must not be escalated."""
    agent = make_agent(tmp_path)
    # Shut every day. An explicit open:false is the one thing hours.py treats
    # as a deliberate instruction to go dark, so this holds whatever the real
    # clock says — the hours check reads wall time, not the injected one.
    agent._operating_hours = {
        day: {"open": False}
        for day in (
            "monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday",
        )
    }

    agent._check_display_alive()
    advance(agent, 7200)
    agent._check_display_alive()

    assert agent._display_bad_since is None


# ── The escalation ───────────────────────────────────────────────────────


def test_a_dark_panel_asks_for_a_relaunch_once_the_threshold_passes(tmp_path):
    agent = make_agent(tmp_path)
    write_display(agent, playing=False)

    agent._check_display_alive()          # first sighting: start the clock
    assert agent._display_bad_since == NOW
    assert not agent._display_restart_asked

    advance(agent, 300)                   # 5 min — under the 10 min threshold
    agent._check_display_alive()
    assert not agent._display_restart_asked

    advance(agent, 400)                   # now past it
    agent._check_display_alive()
    assert agent._display_restart_asked


def test_a_missing_driver_snapshot_counts_as_dark(tmp_path):
    """No display.json at all is the exact shape of the failure we hit."""
    agent = make_agent(tmp_path)  # nothing written

    agent._check_display_alive()
    advance(agent, 700)
    agent._check_display_alive()

    assert agent._display_restart_asked


def test_a_stale_snapshot_counts_as_dark(tmp_path):
    """
    A driver that died leaves its last snapshot behind, frozen. Reading
    `playing: true` from a file nobody has touched in an hour would be
    exactly the wrong conclusion, so freshness is checked, not just the flag.
    """
    agent = make_agent(tmp_path)
    write_display(agent, playing=True, age_seconds=3600)

    agent._check_display_alive()
    advance(agent, 700)
    agent._check_display_alive()

    assert agent._display_restart_asked


def test_recovery_clears_the_episode(tmp_path):
    agent = make_agent(tmp_path)
    write_display(agent, playing=False)
    agent._check_display_alive()
    advance(agent, 700)
    agent._check_display_alive()
    assert agent._display_restart_asked

    write_display(agent, playing=True)
    agent._check_display_alive()

    assert agent._display_bad_since is None
    assert not agent._display_restart_asked


# ── The reboot, which must be hard to reach ──────────────────────────────


def test_the_reboot_is_off_by_default(tmp_path):
    """Per the fleet rule: a feature that acts on hardware defaults OFF."""
    agent = make_agent(tmp_path)
    write_display(agent, playing=False)
    calls: list[list[str]] = []
    agent._exec = lambda argv: (calls.append(argv), (True, ""))[1]  # type: ignore[method-assign]

    agent._check_display_alive()
    for _ in range(10):
        advance(agent, 600)
        agent._check_display_alive()

    assert calls == []
    assert not agent._display_rebooted


def test_the_reboot_fires_once_when_enabled(tmp_path):
    agent = make_agent(tmp_path, ADNOVA_DISPLAY_WATCHDOG_REBOOT="true")
    write_display(agent, playing=False)
    calls: list[list[str]] = []
    agent._exec = lambda argv: (calls.append(argv), (True, ""))[1]  # type: ignore[method-assign]

    agent._check_display_alive()
    advance(agent, 700)
    agent._check_display_alive()      # relaunch asked
    assert calls == []

    advance(agent, 1300)              # past 1800s total
    agent._check_display_alive()
    assert calls == [["sudo", "-n", "systemctl", "reboot"]]

    # A stand whose panel is genuinely broken must not reboot every half hour
    # forever. Once per process, and the operator gets the event trail instead.
    for _ in range(5):
        advance(agent, 1800)
        agent._check_display_alive()
    assert len(calls) == 1
