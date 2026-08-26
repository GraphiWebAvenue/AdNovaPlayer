"""
The brain's main loop.

Everything else is a part; this is what runs them. Three cooperating
rhythms, each on its own thread so a slow network never stalls the screen:

  fetch loop      pull the manifest, verify it, download the next few
                  hours of media, install the new plan for the server to
                  read. Runs on the manifest poll interval.

  heartbeat loop  report health, read the control channel back, and
                  upload buffered playback logs. Runs on the heartbeat
                  interval, and answers Dashboard's pull-based "ping" by
                  sending a fuller snapshot when asked.

  the server      already built (server.py) — the browser's only contact,
                  reading whatever plan the fetch loop last installed.

The design rule that governs all of it: **a failure in any one rhythm
never stops the others.** The network can be down for a week and the
screen keeps playing its cached plan; the model can be wrong and the
device still reports its temperature. Each loop catches its own errors,
logs them, and comes back on the next tick.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import diagnostics, event_log
from .api import DashboardApi
from .cache import MediaCache, MediaNeed
from .capture import ScreenCapture, capture_proof
from .config import Config
from .event_log import EventLog
from .health import local_ip as read_local_ip
from .health import read as read_health
from .hours import screen_should_be_on
from .manifest import Manifest, UntrustedManifest
from .playback_log import Entry, PlaybackLog, now_iso
from .schedule import FALLBACK, PlayItem, Schedule, fallback_item, test_item
from .screen import Screen

log = logging.getLogger("adnova.agent")


class Agent:
    """
    Holds the shared state the loops read and write, guarded so the fetch
    loop can swap in a whole new plan while the server is mid-read.
    """

    def __init__(
        self,
        config: Config,
        api: DashboardApi,
        cache: MediaCache,
        playback: PlaybackLog,
        screen: Screen | None = None,
        on_auth_lost: Callable[[], None] | None = None,
    ) -> None:
        self._config = config
        self._api = api
        self._cache = cache
        self._playback = playback
        # Important device events, shipped out-of-band after a good heartbeat.
        # Published process-wide as it is built, so the parts of the player
        # that have no reason to know about the agent — the admin page's login
        # check, the HTTP client, the enrollment flow — record into the same
        # chain rather than keeping their own idea of what happened here.
        self._events = event_log.adopt(EventLog(self._config.log_dir / "events.json"))
        self._screen = screen or Screen()
        # Verified capture (#8/#9/#11): a frame grabber + the feature flag.
        # Off by default; when on, a played slot attaches a screenshot hash.
        self._capture = ScreenCapture()
        self._verify_capture = self._config.verify_capture
        # Called once when the stand key has been rejected long enough to be
        # considered gone, so the process can drop back into enrollment for a
        # fresh key. Injected by main.py; absent in tests, where the decision
        # itself is what is checked.
        self._on_auth_lost = on_auth_lost
        # How many rejections in a row before we conclude the key is dead. At
        # one heartbeat a minute this is ~10 minutes of solid 401s — well past
        # any transient signing-window blip, which the clock correction already
        # removes anyway.
        self._auth_failure_limit = 10
        self._auth_lost_fired = False

        # The boot self-test result, filled by run_self_test() at start-up and
        # reported on every heartbeat so a stand that came up degraded (no mpv,
        # a read-only card) is visible without a site visit.
        self._boot_checks: list[diagnostics.Check] = []

        self._lock = threading.Lock()
        self._schedule = Schedule(None, cache)
        self._stop = threading.Event()

        # What is on screen right now, updated by the server via on_playing,
        # so the heartbeat can report it and playback can be logged when it
        # changes. Guarded by the same lock.
        self._current: PlayItem | None = None
        self._last_logged_slot: int | None = None
        # The slot currently open in the playback log (recorded on start,
        # closed with its real duration + outcome when it ends):
        # (slot_id, started_iso, started_dt, duration_seconds | None).
        self._open_play: tuple[int, str, datetime, float | None] | None = None
        # Wall clock for measuring played duration; injectable for tests.
        self._now = lambda: datetime.now(tz=UTC)
        # How far this device's own clock sits from Dashboard's true time,
        # learned from the signed manifest's server_time on every fetch. Added
        # to the wall clock (see _trusted_now) so slots resolve at the right
        # instant even on a Pi with no RTC and NTP down — "advance from the
        # last known good time", exactly as the working rules require. A
        # constant offset cancels out of any duration measured as a difference,
        # so only absolute-time slot decisions use the corrected clock.
        self._clock_offset = timedelta(0)
        # The raw skew last seen (device minus Dashboard), reported so an
        # operator sees a stand whose NTP is broken even while we correct it.
        # None until the first successful fetch.
        self._clock_skew_seconds: float | None = None

        # A live takeover and the stand's operating hours, both refreshed
        # from what Dashboard sends. The manifest carries the hours; the
        # heartbeat control channel carries the takeover.
        self._emergency: PlayItem | None = None
        # When a takeover is scheduled to begin. None means "now"; a future
        # time makes every stand flip to it at the same trusted-clock instant,
        # so a coordinated campaign appears in sync across the fleet rather
        # than drifting by each device's heartbeat timing.
        self._emergency_at: datetime | None = None
        self._operating_hours: dict | None = None
        self._timezone = "UTC"
        # The current gap-filler, rebuilt whenever a plan installs. Starts as
        # the built-in filler until a manifest names one.
        self._fallback: PlayItem = FALLBACK

        # A live test broadcast, when the operator has one running. Rebuilt on
        # each fetch from the manifest; None whenever no test is active.
        self._test: PlayItem | None = None

        # Whether the last heartbeat reached Dashboard, for the admin page.
        self._last_contact_ok = False
        # When Dashboard was last reached. Starts at boot so a device that
        # never gets online counts its offline window from power-on; used to
        # fall to the default loop once the cached plan has gone stale.
        self._last_contact_at = datetime.now(tz=UTC)
        # Where the display driver leaves its snapshot of what the panel is
        # really doing. Read on each heartbeat so Dashboard reports verified
        # playback, not just intent. Env-overridable to match the driver.
        self._display_state_path = os.environ.get(
            "ADNOVA_DISPLAY_STATE", "/var/lib/adnova-player/display.json"
        )
        # Display watchdog state: when the panel first went dark during hours
        # it should have been lit, and which escalations have already fired for
        # this episode. See _check_display_alive. `_display_rebooted` is
        # deliberately per-process, not per-episode: it is what stops a stand
        # whose panel is genuinely broken from rebooting itself every half hour
        # forever.
        self._display_bad_since: datetime | None = None
        self._display_restart_asked = False
        self._display_rebooted = False

        # Command outcomes the device has to report on the next heartbeat —
        # {id, status, detail} — so Dashboard shows each one done or failed
        # rather than the operator guessing whether a click landed.
        self._pending_acks: list[dict] = []

        # Monotonic timestamps the loops bump on each pass, read by the
        # watchdog to decide whether the process still deserves to live.
        self._fetch_alive = _mono()
        self._heartbeat_alive = _mono()

        self._boot = datetime.now(tz=UTC)

    # ── The plan the server reads ────────────────────────────────────────

    def schedule(self) -> Schedule:
        """
        The current plan, including any live takeover. Called by the local
        server on every /state, so a takeover pushed a second ago is on
        screen by the next poll.
        """
        with self._lock:
            emergency = self._active_emergency()
            offline = self._offline_expired(self._schedule.manifest)
            if emergency is None and not offline:
                return self._schedule
            # Rebuild a view with the takeover and/or the offline-expiry flag
            # layered on, without mutating the stored plan — both are
            # transient. The test override is already baked into the stored
            # plan, but carry it here too so it survives the rebuild.
            return Schedule(
                self._schedule.manifest,
                self._cache,
                emergency=emergency,
                fallback=self._fallback,
                test=self._test,
                offline_expired=offline,
            )

    def _active_emergency(self) -> PlayItem | None:
        """
        The takeover if it is due, else None.

        A takeover with a future start time is held until that instant, judged
        by the corrected clock — so a scheduled, fleet-wide takeover flips on
        every stand together instead of whenever each one's heartbeat happened
        to land. An immediate takeover (no start time) is active at once.
        """
        if self._emergency is None:
            return None
        if self._emergency_at is not None and self._trusted_now() < self._emergency_at:
            return None
        return self._emergency

    def _trusted_now(self) -> datetime:
        """
        The wall clock corrected by the offset learned from Dashboard's signed
        server_time. Use this for every absolute-time decision about what plays
        now, so a wrong device clock never plays the wrong slot. Duration
        measurements keep using the raw clock, where a constant offset cancels.
        """
        return self._now() + self._clock_offset

    def _learn_clock(self, manifest: Manifest, at: datetime) -> None:
        """
        Correct our clock from the signed manifest's server_time.

        server_time is covered by the signature, so it is trusted; the small
        transit between Dashboard stamping it and us reading it is well inside
        the schedule's second-level granularity. Replays are already refused by
        the monotonic schedule-version check, so a stale server_time cannot be
        fed in here to shift the clock backwards.
        """
        previous = self._clock_offset
        self._clock_skew_seconds = (at - manifest.server_time).total_seconds()
        self._clock_offset = manifest.server_time - at

        # A clock that jumps is either a Pi with no RTC finding its feet (once,
        # at boot) or something odder. Only a material change is recorded, so
        # ordinary sub-second drift never reaches the trail.
        moved = abs((self._clock_offset - previous).total_seconds())
        if moved >= 60:
            self._events.record(
                "clock.corrected", "warn",
                f"Offset moved by {moved:.0f}s to {self._clock_offset.total_seconds():.0f}s "
                f"(device is {self._clock_skew_seconds:.0f}s from Dashboard).",
            )

    def _offline_expired(self, manifest: Manifest | None) -> bool:
        """
        True once we've been out of contact longer than the plan is meant to
        cover (its preload window, default 6h). Past that, the cached slots
        are stale and the screen falls to the operator's default loop.
        """
        if manifest is None:
            return False
        hours = manifest.preload_hours or 6
        return (self._now() - self._last_contact_at).total_seconds() > hours * 3600

    def on_playing(self, item: PlayItem) -> None:
        """
        The server tells us what it just put on screen.

        This is where a real slot becomes a billable play. A slot is
        logged once, when it first appears — not on every poll — so a
        30-second image polled five times is one impression, not five.

        The entry is recorded on start (so a power loss mid-play still
        bills it) and *closed* when the slot ends, with its real played
        duration and outcome: `played` if it ran ~its full length,
        `partial` if it was cut short (e.g. a test broadcast preempted
        it). A slot displaced before it ever reaches the screen is never
        recorded, so it is never billed — matching R16.
        """
        to_record: Entry | None = None
        with self._lock:
            self._current = item

            same_real_slot = (
                not item.is_fallback
                and item.ad_id is not None
                and item.slot_id == self._last_logged_slot
            )
            # Anything other than "still the same real slot" closes the
            # open play (a gap, a test/emergency, or the next slot).
            if not same_real_slot and self._open_play is not None:
                self._finalize_open(self._now())

            if item.is_fallback or item.ad_id is None:
                # Not a billable slot. Clear the marker so the next real
                # slot logs even if it happens to share an id.
                self._last_logged_slot = None
                return
            if item.slot_id == self._last_logged_slot:
                return

            self._last_logged_slot = item.slot_id
            version = self._schedule.schedule_version
            started = now_iso()
            self._open_play = (
                item.slot_id, started, self._now(),
                item.duration_seconds,
            )
            to_record = Entry(
                slot_id=item.slot_id,
                ad_id=item.ad_id,
                started_at=started,
                ended_at=None,
                outcome="played",
                schedule_version=version,
            )

        if to_record is not None:
            self._playback.record(to_record)

    def _finalize_open(self, ended: datetime) -> None:
        """
        Close the currently-open play with its real duration + outcome.

        Called while holding self._lock; the playback log has its own
        lock, so this does not deadlock.
        """
        open_play = self._open_play
        self._open_play = None
        if open_play is None:
            return
        slot_id, started_iso, started_dt, duration = open_play
        played = max(0.0, (ended - started_dt).total_seconds())
        outcome = "played" if (not duration or played >= 0.9 * duration) else "partial"
        # Billing-grade proof, only for a full play and only when the stand is
        # opted in. Best-effort: a failed grab just leaves the play unverified.
        verification = None
        if self._verify_capture and outcome == "played":
            verification = capture_proof(self._capture)
        self._playback.finalize(
            slot_id=slot_id,
            started_at=started_iso,
            ended_at=ended.isoformat(),
            played_seconds=round(played, 1),
            outcome=outcome,
            verification=verification,
        )

    # ── Startup ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """Load any cached plan, then run the loops until stopped."""
        self._load_cached_plan()

        threads = [
            threading.Thread(target=self._fetch_loop, name="fetch", daemon=True),
            threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True),
        ]
        for thread in threads:
            thread.start()
        log.info("Agent started for stand %s.", self._config.stand_id)
        # Boot and shutdown bracket everything else in the trail. A device
        # that restarts unexpectedly leaves a start with no matching stop,
        # which is how a crash-loop or a yanked power lead reads afterwards.
        self._events.record(
            "service.started", "warn",
            f"Player {_version()} up for stand {self._config.stand_id} "
            f"(chain at seq {self._events.sequence}).",
        )

    def stop(self) -> None:
        self._events.record("service.stopping", "warn", "Asked to stop.")
        # One last shipment while the network is still up: the events from the
        # final minutes are the ones an incident review wants, and holding them
        # for a restart that may never come loses them.
        self._safe(self._upload_events, default=None)
        self._stop.set()

    def _load_cached_plan(self) -> None:
        """
        On boot, adopt the last good manifest if we have one.

        This is why a device that boots with no network still plays: the
        plan it had before the reboot is on disk, signed, and verified
        again on the way in.
        """
        manifest = Manifest.load(
            self._config.manifest_path,
            public_keys=self._config.trusted_keys or None,
            stand_id=self._config.stand_id,
        )
        if manifest is not None:
            fallback = fallback_item(manifest.fallback_media, self._cache)
            test = test_item(
                manifest.test_override_media, self._cache, manifest.test_override_muted
            )
            with self._lock:
                self._schedule = Schedule(
                    manifest, self._cache, fallback=fallback, test=test
                )
                self._fallback = fallback
                self._test = test
                self._operating_hours = manifest.operating_hours
                self._timezone = manifest.timezone or "UTC"
            log.info("Loaded cached plan (version %s).", manifest.schedule_version)
        else:
            log.info("No usable cached plan; the fallback loop plays until one arrives.")

    # ── Fetch loop ───────────────────────────────────────────────────────

    def _fetch_loop(self) -> None:
        while not self._stop.is_set():
            interval = self._safe(self._fetch_once, default=self._config.manifest_poll_seconds)
            self._fetch_alive = _mono()
            self._stop.wait(max(30, interval))

    def _fetch_once(self) -> int:
        """
        One fetch cycle. Returns how many seconds to wait before the next.

        Every failure mode ends the same way: keep the current plan, wait,
        try again. A manifest that will not verify, a network that is down,
        a media file that will not download — none of them take the screen
        off the last good plan.
        """
        document = self._api.fetch_manifest()
        if document is None:
            return self._config.manifest_poll_seconds

        try:
            manifest = Manifest.parse(
                document,
                public_keys=self._config.trusted_keys or None,
                stand_id=self._config.stand_id,
                min_schedule_version=self._held_version(),
            )
        except UntrustedManifest as exc:
            # The important refusal: somebody handed us a plan we cannot
            # prove Dashboard wrote, or an older one replayed. Keep playing.
            log.error("Refusing a manifest: %s", exc)
            self._events.record("manifest.refused", "security", str(exc))
            return self._config.manifest_poll_seconds
        except (ValueError, KeyError) as exc:
            log.warning("Manifest did not parse (%s); keeping the current plan.", exc)
            return self._config.manifest_poll_seconds

        # Learn the clock offset from this signed manifest first, so every
        # time-based decision below (which slots to preload, what to keep)
        # anchors on Dashboard's true time, not the device's possibly-wrong one.
        self._learn_clock(manifest, self._now())
        moment = self._trusted_now()

        # Download the preload horizon before installing the plan, so a slot
        # never goes live before its bytes are on disk.
        self._download_horizon(manifest, moment)

        # The fallback loop is downloaded too, so a gap never shows black
        # while the operator's chosen filler is still on the wire.
        if manifest.fallback_media is not None:
            self._cache.ensure(MediaNeed(
                url=manifest.fallback_media.url,
                checksum_sha256=manifest.fallback_media.checksum_sha256,
                size_bytes=manifest.fallback_media.bytes,
            ))

        # A test broadcast is downloaded on the same path, so hitting "play
        # now" in Dashboard puts the ad on screen as soon as its bytes land
        # rather than after the operator schedules and waits for a slot.
        if manifest.test_override_media is not None:
            self._cache.ensure(MediaNeed(
                url=manifest.test_override_media.url,
                checksum_sha256=manifest.test_override_media.checksum_sha256,
                size_bytes=manifest.test_override_media.bytes,
            ))

        manifest.save(self._config.manifest_path)

        fallback = fallback_item(manifest.fallback_media, self._cache)
        test = test_item(
            manifest.test_override_media, self._cache, manifest.test_override_muted
        )

        with self._lock:
            self._schedule = Schedule(
                manifest, self._cache, fallback=fallback, test=test
            )
            self._fallback = fallback
            self._test = test
            # Operating hours and timezone travel with the manifest, so the
            # screen-power decision needs no separate fetch and works offline.
            self._operating_hours = manifest.operating_hours
            self._timezone = manifest.timezone or "UTC"

        # Drop media the new plan no longer references.
        self._cache.evict_except(
            self._schedule_ref().preload_checksums(moment)
        )

        log.info("Installed plan version %s.", manifest.schedule_version)
        return manifest.manifest_poll_seconds

    def _download_horizon(self, manifest: Manifest, moment: datetime) -> None:
        needs = [
            MediaNeed(
                url=slot.media.url,
                checksum_sha256=slot.media.checksum_sha256,
                size_bytes=slot.media.bytes,
            )
            for slot in manifest.slots_to_preload(moment)
        ]
        results = self._cache.ensure_all(needs)
        missing = [c[:12] for c, ok in results.items() if not ok]
        if missing:
            log.warning("%d media file(s) not yet cached: %s", len(missing), ", ".join(missing))

        # Decode-probe the newly-cached media on this background thread, so
        # an undecodable file (valid checksum, but a codec/container the
        # device can't play) is caught and treated as missing here — not
        # discovered as a black frame at its paid slot.
        self._cache.probe_all(need.checksum_sha256 for need in needs)

    # ── Heartbeat loop ───────────────────────────────────────────────────

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            self._safe(self._heartbeat_once, default=None)
            self._safe(self._upload_playback, default=None)
            self._safe(self._apply_screen_power, default=None)
            # Runs after the beat, so its escalations ride the next one and
            # Dashboard learns why a stand relaunched or rebooted itself.
            self._safe(self._check_display_alive, default=None)
            # Free media that has already aired, every beat — so played
            # files are gone within ~a heartbeat even while offline, and the
            # card never fills with content that will not play again.
            self._safe(self._evict_played, default=None)
            self._heartbeat_alive = _mono()
            self._stop.wait(max(15, self._config.heartbeat_seconds))

    def _evict_played(self) -> None:
        """
        Drop media the current plan no longer references, and hold the cache
        under the manifest's byte budget.

        Skipped when there is no plan yet: the keep-set would be empty and
        we would evict the very fallback the screen is holding.
        """
        schedule = self._schedule_ref()
        manifest = schedule.manifest
        if manifest is None:
            return
        keep = schedule.preload_checksums(self._now())
        self._cache.evict_except(keep)
        if manifest.max_cache_bytes:
            self._cache.enforce_budget(manifest.max_cache_bytes, keep)

    def status(self) -> dict:
        """A snapshot for the on-site admin page. Read-only, cheap."""
        health = read_health(self._config.cache_dir, self._cache.used_bytes())
        with self._lock:
            current = self._current
            version = self._schedule.schedule_version
        return {
            "stand_id": self._config.stand_id,
            "player_version": _version(),
            "schedule_version": version,
            "now_playing": (current.label or f"slot {current.slot_id}")
            if current and not current.is_fallback else None,
            "online": self._last_contact_ok,
            "temp_c": health.temp_c,
            "disk_free_mb": (health.disk_free_bytes // (1024 * 1024))
            if health.disk_free_bytes is not None else None,
            "pending_logs": self._playback.pending(),
            "warnings": health.warnings,
        }

    def trigger(self, action: str) -> bool:
        """Run an operation the admin page asked for. Returns acceptance."""
        if action == "refetch":
            self._safe(self._fetch_once, default=self._config.manifest_poll_seconds)
            return True
        if action == "update":
            # The update runs from a privileged timer; the page only nudges
            # it. Returning True means "asked", not "done" — the page polls
            # status to see the result.
            import subprocess

            subprocess.Popen(  # noqa: S603,S607 — fixed unit name
                ["systemctl", "start", "adnova-update.service"],
            )
            return True
        return False

    def is_live(self) -> bool:
        """
        Have both loops made progress recently?

        The watchdog reads this. "Recently" is generous — several times the
        slowest interval — so a slow network never looks like a hang, but a
        thread wedged for minutes does. A loop that is merely waiting on its
        timer still counts as live, because it stamps at the top of each
        pass before it sleeps.
        """
        deadline = _mono() - self._liveness_window()
        return self._fetch_alive > deadline and self._heartbeat_alive > deadline

    def _liveness_window(self) -> float:
        # Three of the longest interval, floored so a fast config still
        # tolerates one slow cycle.
        longest = max(self._config.manifest_poll_seconds, self._config.heartbeat_seconds)
        return max(180.0, longest * 3)

    def _apply_screen_power(self) -> None:
        """
        Power the display to match the stand's operating hours.

        Evaluated in the stand's own timezone — the manifest names it — so
        a shop in Amsterdam sleeps on Amsterdam time regardless of where
        the Pi thinks it is. A missing or unknown zone falls back to the
        device clock, which still beats leaving the panel lit all night.
        """
        with self._lock:
            hours = self._operating_hours
            tzname = self._timezone

        try:
            zone = ZoneInfo(tzname)
        except Exception:  # noqa: BLE001 — an unknown zone must not stop the loop
            zone = ZoneInfo("UTC")

        local_now = datetime.now(tz=zone)
        self._screen.apply(screen_should_be_on(hours, local_now))

    def _check_display_alive(self) -> None:
        """
        Notice a panel that has gone dark, and do something about it.

        Everything else in this system watches the brain. `Restart=always`,
        the systemd watchdog and the board's hardware watchdog all guard the
        Python process — which can be perfectly healthy while the screen it
        exists to fill has shown nothing for a week. The display stack starts
        from a desktop autostart hook, so nothing restarts it and nothing
        reports it; Dashboard sees heartbeats and calls the stand online.

        This closes that loop from inside the device, because it is the only
        vantage point that can: Dashboard cannot reach a Pi behind a shop's
        NAT, and the operator cannot see the panel.

        Two steps, deliberately far apart in consequence. Asking the
        in-session helper to relaunch the display is a file write and costs
        nothing if it was a false alarm. Rebooting the board is a real
        intervention on someone's premises, so it is a fleet flag that
        defaults off and fires at most once per boot — a watchdog that can
        reboot-loop a stand is worse than the fault it was meant to fix.
        """
        # Only judge the panel when something should actually be on it. Outside
        # operating hours the screen is off on purpose, and a device with no
        # plan yet has nothing to show — neither is a fault.
        with self._lock:
            hours = self._operating_hours
            tzname = self._timezone
            has_plan = self._current is not None

        try:
            zone = ZoneInfo(tzname)
        except Exception:  # noqa: BLE001 — an unknown zone must not stop the loop
            zone = ZoneInfo("UTC")

        if not has_plan or not screen_should_be_on(hours, datetime.now(tz=zone)):
            self._display_bad_since = None
            return

        display = self._read_display_health()
        reported_at = display.get("at") if display else None
        fresh = False
        if isinstance(reported_at, int | float):
            # The driver stamps `at` each tick. Two heartbeats of slack, so a
            # slow beat or a driver mid-reload is never mistaken for a dead one.
            age = datetime.now(tz=UTC).timestamp() - float(reported_at)
            fresh = age < max(120.0, self._config.heartbeat_seconds * 2)

        if fresh and display.get("playing"):
            if self._display_bad_since is not None:
                self._events.record(
                    "display.recovered", "info",
                    "The panel is playing again.",
                )
            self._display_bad_since = None
            self._display_restart_asked = False
            return

        now = self._now()
        if self._display_bad_since is None:
            self._display_bad_since = now
            return

        dark_for = (now - self._display_bad_since).total_seconds()

        if (
            not self._display_restart_asked
            and dark_for >= self._config.display_stale_restart_seconds
        ):
            self._display_restart_asked = True
            ok, detail = self._request_kiosk_restart()
            self._events.record(
                "display.stalled", "error",
                f"Nothing on the panel for {int(dark_for)}s; "
                f"asked for a display restart ({detail}).",
            )
            log.warning("Display stale for %ss — requested a relaunch.", int(dark_for))
            if not ok:
                return

        if (
            self._config.display_watchdog_reboot
            and not self._display_rebooted
            and dark_for >= self._config.display_stale_reboot_seconds
        ):
            self._display_rebooted = True
            self._events.record(
                "display.reboot", "error",
                f"Panel dark for {int(dark_for)}s after a relaunch attempt; "
                "rebooting the board.",
            )
            # Shipped before the argv runs: a reboot never returns to write
            # its own log line. Same reasoning as _run_commands' deferred set.
            self._safe(self._upload_events, default=None)
            self._exec(["sudo", "-n", "systemctl", "reboot"])

    def _heartbeat_once(self) -> None:
        body = self._heartbeat_body()
        response = self._api.send_heartbeat(body)
        self._last_contact_ok = response is not None

        # A key rejected for too long is a key that is gone — the stand was
        # reassigned or deleted, or the key rotated. Drop back into enrollment
        # for a fresh one rather than showing the fallback loop forever. Checked
        # even on a None response, since a 401 is exactly what returns None.
        if self._api.auth_failures >= self._auth_failure_limit:
            self._handle_auth_lost()

        if response is None:
            return
        # A reached heartbeat is proof the network is up; reset the offline
        # clock that would otherwise fall the screen to the default loop.
        self._last_contact_at = self._now()

        # The control channel. Dashboard tells us to refetch after a
        # schedule change, and this is where the pull-based "ping" lands:
        # a flag asking for an immediate fuller report, answered by the
        # next heartbeat carrying it.
        if response.get("refetch_manifest"):
            log.info("Dashboard asked for a refetch; fetching now.")
            self._safe(self._fetch_once, default=self._config.manifest_poll_seconds)

        # A takeover: put an image on screen now, or clear one. Kept as a
        # cached media reference so it is checksum-verified like any other
        # media rather than a URL the device fetches on trust.
        self._apply_emergency(response.get("emergency"))

        # A live test broadcast, started or ended through the control channel
        # rather than the manifest. The point is immediacy: the instant an
        # operator stops the test, Dashboard sends `test: null` here and the
        # override clears on this heartbeat — the screen resumes the slot the
        # schedule says belongs on NOW, not the ad that was up before the test.
        # The key is honoured only when present; its absence means "no change",
        # so a manifest-driven test is not wiped by every ordinary heartbeat.
        if "test" in response:
            self._apply_test(response.get("test"))

        # Named operations queued by an operator. Only the fixed set below
        # is ever run — the response never carries a shell string, and this
        # would ignore one if it did.
        self._run_commands(response.get("commands"))

        # Ship any buffered important events now the network is proven up
        # (watchdog priority: logs go out on the next successful heartbeat).
        self._safe(self._upload_events, default=None)

    def _upload_events(self) -> None:
        batch = self._events.take_batch()
        if not batch:
            return
        body = {
            "contract_version": "player_logs.v1",
            "stand_id": self._config.stand_id,
            "player_version": _version(),
            "batch_id": uuid.uuid4().hex,
            # The chain head travels with the batch, so Dashboard can tell a
            # gap ("events 40-58 never arrived") from a quiet device, and a
            # re-imaged device (new boot_id, seq back to 1) from a wiped one
            # (same boot_id, seq gone backwards).
            "boot_id": self._events.boot_id,
            "sequence": self._events.sequence,
            "events": [
                {
                    "event_id": e.event_id,
                    "at": e.at,
                    "sev": e.sev,
                    "code": e.code,
                    "detail": e.detail,
                    "seq": e.seq,
                    "prev": e.prev,
                    "hash": e.hash,
                    "rid": e.rid,
                    "ref": e.ref,
                }
                for e in batch
            ],
        }
        if self._api.send_logs(body) is not None:
            self._events.ack(batch)

    def _run_commands(self, commands: object) -> None:
        """
        Execute the whitelisted operations the heartbeat delivered.

        The command is matched against a closed map to a fixed argv — there
        is no path from the network to an arbitrary shell. A command that
        restarts or reboots the device is acted on last and never acked,
        because the process is gone before it could; its returning heartbeat
        with fresh uptime is the confirmation. The others ack so Dashboard
        can show them done.
        """
        if not isinstance(commands, list):
            return

        # name -> (argv, survives) — survives=False means the process ends,
        # so it cannot ack and must run after everything else.
        runnable = {
            "refetch": (None, True),  # handled in-process, not via a command
            "restart": (["sudo", "-n", "systemctl", "restart", "adnova-player"], False),
            "reboot": (["sudo", "-n", "systemctl", "reboot"], False),
            # Power the whole board off from Dashboard — for a stand taken out
            # of service. There is no remote power-on (the board is off), so
            # this is deliberately paired in the UI with a note that turning it
            # back on needs someone on site or a smart plug.
            "shutdown": (["sudo", "-n", "systemctl", "poweroff"], False),
            "update": (["sudo", "-n", "systemctl", "start", "adnova-update.service"], True),
            # --no-block: an OS update runs for minutes; without it, starting
            # the oneshot unit would block this heartbeat until apt finished
            # and time the exec out. It returns at once; the real outcome
            # arrives later via the os-update.json result file.
            "os_update": (["sudo", "-n", "systemctl", "start", "--no-block", "adnova-os-update.service"], True),
        }
        # The line Dashboard shows when a command succeeds. The async ones
        # say so — their real outcome follows (a fresh uptime for a restart,
        # the os-update result file for an OS update).
        done_detail = {
            "refetch": "Schedule fetched.",
            "update": "Update started; the player restarts when it finishes.",
            "os_update": "OS update started; the result follows shortly.",
        }

        deferred: list[tuple[int, list[str]]] = []
        acks: list[dict] = []

        for entry in commands:
            if not isinstance(entry, dict):
                continue
            cid = entry.get("id")
            name = entry.get("command")
            if not isinstance(cid, int):
                continue

            # Two commands reach the Wayland session the player cannot touch
            # directly, so they go through the in-session helper via a trigger
            # file rather than a systemctl argv.
            if name == "screenshot":
                ok, detail = self._capture_ack()
                acks.append({"id": cid, "status": "done" if ok else "error", "detail": detail})
                continue
            if name == "restart_kiosk":
                ok, detail = self._request_kiosk_restart()
                acks.append({"id": cid, "status": "done" if ok else "error", "detail": detail})
                continue
            # The safe answer to "let me SSH in and look". Everything an
            # operator would run by hand — which parts of the display stack
            # are up, what the launcher logged, what is really on the panel —
            # gathered here and posted back, with no shell anywhere in the
            # path and no secret in the payload.
            if name == "diagnostics":
                ok, detail = self._send_diagnostics()
                acks.append({"id": cid, "status": "done" if ok else "error", "detail": detail})
                continue
            if name in ("screen_on", "screen_off"):
                ok, detail = self._request_screen(name == "screen_on")
                acks.append({"id": cid, "status": "done" if ok else "error", "detail": detail})
                continue

            if name not in runnable:
                # A command Dashboard sent that this player has no name for.
                # Harmless — the map is closed — but a fleet mid-upgrade and a
                # forged control response look the same from here, so it is on
                # the record either way.
                self._events.record(
                    "command.unknown", "security",
                    f"Ignored an unrecognised command {str(name)[:40]!r}.",
                    ref=cid,
                )
                continue

            argv, survives = runnable[name]

            if name == "refetch":
                self._safe(self._fetch_once, default=self._config.manifest_poll_seconds)
                acks.append({"id": cid, "status": "done", "detail": done_detail["refetch"]})
            elif survives:
                ok, detail = self._exec(argv)
                self._events.record(
                    "command.executed" if ok else "command.failed",
                    "warn" if ok else "error",
                    name if ok else f"{name}: {detail}",
                    ref=cid,
                )
                acks.append({
                    "id": cid,
                    "status": "done" if ok else "error",
                    "detail": done_detail.get(name, "Done.") if ok else detail,
                })
            else:
                # Restart/reboot last, so any acks and this heartbeat's
                # other work complete before the process disappears.
                deferred.append((cid, argv))

        if acks:
            self._pending_acks.extend(acks)

        for cid, argv in deferred:
            log.info("Running device command: %s", " ".join(argv))
            # Recorded and shipped *before* the argv runs: a reboot never comes
            # back to write its own log line, so "the device restarted because
            # an operator asked it to" has to be told in advance or not at all.
            self._events.record(
                "command.executing", "warn",
                f"{' '.join(argv)[:120]} — the process may not return.",
                ref=cid,
            )
            self._safe(self._upload_events, default=None)
            self._exec(argv)  # from here the process may not return

    def _exec(self, argv: list[str]) -> tuple[bool, str]:
        """Run a fixed argv; return (ok, detail) where detail explains a failure."""
        import subprocess

        try:
            result = subprocess.run(  # noqa: S603 — fixed argv from a closed map
                argv, capture_output=True, text=True, timeout=30
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("Command %s failed: %s", argv, exc)
            return False, str(exc)[:200]
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[:200]
            log.warning("Command %s exited %s: %s", argv, result.returncode, detail)
            return False, detail or f"exited {result.returncode}"
        return True, ""

    def _read_display_health(self) -> dict:
        """
        The display driver's latest snapshot, or {} if none.

        Mirrors _read_os_update: another process (the mpv driver, in the
        desktop session) writes it, we only read. Absent or unreadable simply
        means no driver has reported — the heartbeat then carries a null
        display block rather than failing.
        """
        try:
            with open(self._display_state_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _read_os_update(self) -> dict:
        """
        The last OS-update outcome, written by adnova-os-update.sh.

        Reported on every heartbeat so Dashboard always shows the latest
        result — "ok", the number of packages, or the error — instead of the
        operator wondering whether the update button did anything. Absent or
        unreadable simply means no update has run, which is the common case.
        """
        try:
            with open("/var/lib/adnova-player/os-update.json", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    # Signalling to the in-session helper. The player is hardened with
    # ProtectSystem=strict, so the only place it may write is its own state
    # dir — /var/lib/adnova-player — not /tmp. The helper (running in the
    # desktop session, in the adnova group) reads the request from there.
    _KIOSK_REQ = Path("/var/lib/adnova-player/ipc/restart-kiosk.req")
    _SCREEN_REQ = Path("/var/lib/adnova-player/ipc/screen.req")

    def _capture_ack(self) -> tuple[bool, str]:
        """
        Screenshots are handled out-of-band by the in-session uploader
        (adnova_player.shots), which grabs and posts the screen every ~30s
        independently of this service — so it works even when the player is
        down. A manual capture request just confirms that; there is nothing
        for the hardened player process to do.
        """
        return True, "the device uploads a screenshot automatically every ~30s"

    def _send_diagnostics(self) -> tuple[bool, str]:
        """Gather the bundle and post it. Never raises; the ack carries the outcome."""
        try:
            bundle = self.diagnostics_bundle()
        except Exception as exc:  # noqa: BLE001 — a diagnostics read must not kill the beat
            log.warning("Could not assemble the diagnostics bundle: %s", exc)
            return False, f"could not gather diagnostics: {str(exc)[:120]}"

        body = {
            "contract_version": "player_diagnostics.v1",
            "stand_id": self._config.stand_id,
            "player_version": _version(),
            "bundle": bundle,
        }
        if self._api.send_diagnostics(body) is None:
            return False, "diagnostics gathered but the upload failed"

        down = [name for name, up in bundle.get("session", {}).items() if not up]
        if down:
            return True, "Diagnostics sent. Not running: " + ", ".join(sorted(down)) + "."
        return True, "Diagnostics sent; the whole display stack is running."

    def _request_kiosk_restart(self) -> tuple[bool, str]:
        """Ask the in-session helper to relaunch the display."""
        try:
            self._KIOSK_REQ.parent.mkdir(parents=True, exist_ok=True)
            self._KIOSK_REQ.write_text("")
        except OSError as exc:
            return False, f"could not request a display restart: {exc}"
        return True, "display restart requested"

    def _request_screen(self, on: bool) -> tuple[bool, str]:
        """
        Ask the in-session helper to power the panel on or off.

        The panel is normally driven autonomously by the stand's operating
        hours; this is a manual override an operator can reach from Dashboard
        (a maintenance blackout, a late event). The player is hardened and
        cannot touch the Wayland session, so it drops a one-word request in its
        state dir for the in-session helper to act on (wlr-randr / CEC / DPMS).
        """
        try:
            self._SCREEN_REQ.parent.mkdir(parents=True, exist_ok=True)
            self._SCREEN_REQ.write_text("on" if on else "off")
        except OSError as exc:
            return False, f"could not request a screen change: {exc}"
        return True, f"screen {'on' if on else 'off'} requested"

    def run_self_test(self) -> list[diagnostics.Check]:
        """
        Run the boot self-test, log any failures, and keep the result.

        Called once at start-up. Never blocks the player — a degraded stand
        still shows something — but every failure is logged and then carried
        on the heartbeat so an operator sees it.
        """
        self._boot_checks = diagnostics.run_self_test(
            stand_id=self._config.stand_id,
            cache_dir=self._config.cache_dir,
            has_trusted_keys=bool(self._config.trusted_keys),
        )
        for check in self._boot_checks:
            if not check.ok:
                level = log.error if check.severity == "critical" else log.warning
                level("Boot self-test: %s — %s", check.name, check.detail)
                self._events.record(
                    "selftest.failed",
                    "error" if check.severity == "critical" else "warn",
                    f"{check.name}: {check.detail}",
                )

        # The trail checks itself while it is at it. A broken chain means a
        # line on this card was edited or removed since it was written, which
        # is worth an alert even though Dashboard holds the authoritative copy.
        ok, broken_at = self._events.verify()
        if not ok:
            self._events.record(
                "audit.chain_broken", "security",
                f"The local event chain does not verify from seq {broken_at}.",
            )
        return self._boot_checks

    def diagnostics_bundle(self) -> dict:
        """A redacted diagnostics bundle for the admin page or an operator pull."""
        health = read_health(self._config.cache_dir, self._cache.used_bytes())
        return diagnostics.redacted_bundle(
            stand_id=self._config.stand_id,
            player_version=_version(),
            checks=self._boot_checks,
            health={
                "temp_c": health.temp_c,
                "disk_free_bytes": health.disk_free_bytes,
                "storage_writable": health.storage_writable,
                "local_ip": read_local_ip(),
                "uptime_seconds": int((datetime.now(tz=UTC) - self._boot).total_seconds()),
                "warnings": health.warnings,
            },
            session=diagnostics.session_processes(),
            kiosk_log=diagnostics.kiosk_log_tail(),
            display=self._read_display_health() or None,
        )

    def _handle_auth_lost(self) -> None:
        """
        Act, once, on a stand key that has been rejected past the limit.

        Fires the injected callback a single time (main.py clears the dead key
        and restarts into enrollment); further heartbeats do not re-fire it,
        so the restart is requested once, not on every beat until it happens.
        """
        if self._auth_lost_fired:
            return
        self._auth_lost_fired = True
        log.error(
            "Stand key rejected %d times in a row; re-enrolling for a fresh key.",
            self._api.auth_failures,
        )
        self._events.record("auth.lost", "security", f"{self._api.auth_failures} rejections")
        if self._on_auth_lost is not None:
            self._safe(self._on_auth_lost, default=None)

    def _apply_emergency(self, emergency: dict | None) -> None:
        """
        Install or clear a live takeover from the control channel.

        A null or absent value clears it — so an operator ending a closure
        notice simply stops sending it, and the scheduled plan returns on
        the next poll. A takeover names its media by checksum, and it plays
        only once that media is cached and valid, exactly like a slot. An
        optional `starts_at` holds it until that instant so a fleet-wide
        takeover flips on every stand together (synchronised), judged by the
        corrected clock.
        """
        if not isinstance(emergency, dict):
            if self._emergency is not None:
                log.info("Takeover cleared; returning to the schedule.")
            with self._lock:
                self._emergency = None
                self._emergency_at = None
            return

        checksum = str(emergency.get("checksum_sha256") or "")
        url = str(emergency.get("url") or "")
        if not checksum or not url:
            return

        if not self._cache.has(checksum):
            self._cache.ensure(MediaNeed(url=url, checksum_sha256=checksum))
        if not self._cache.has(checksum):
            log.warning("Takeover media could not be cached; not shown.")
            return

        item = PlayItem(
            slot_id=-2,
            ad_id=None,
            kind="video" if emergency.get("type") == "video" else "image",
            local_src=self._cache.local_url_path(checksum),
            muted=bool(emergency.get("muted", True)),
            duration_seconds=int(emergency.get("duration_seconds", 30)),
            priority="urgent",
            label=emergency.get("label"),
        )
        starts_at = self._parse_time(emergency.get("starts_at"))
        with self._lock:
            self._emergency = item
            self._emergency_at = starts_at
        if starts_at is not None:
            log.info("Takeover scheduled for %s.", starts_at.isoformat())
        else:
            log.info("Takeover active.")

    @staticmethod
    def _parse_time(value: object) -> datetime | None:
        """An ISO-8601 instant from the control channel, or None if absent/bad."""
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        # Treat a naive time as UTC, so the comparison against the corrected
        # clock (always tz-aware UTC) never raises on a mixed pair.
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    def _apply_test(self, test: dict | None) -> None:
        """
        Start or end a live test broadcast from the control channel.

        A test plays one ad on repeat over the schedule while an operator
        watches it. Ending it must be instant: the moment Dashboard stops the
        test it sends `test: null` here, the override clears, and now_playing
        resolves to whatever slot the plan says belongs on screen at the
        current moment — never the ad that was up before the test, because the
        plan is evaluated against now, not resumed from a saved position (R15).
        Starting a test live is the same in reverse. A null (or non-dict)
        clears; a dict names its media by checksum and takes effect only once
        those bytes are cached and valid, exactly like a scheduled slot.
        """
        if not isinstance(test, dict):
            if self._test is not None:
                log.info("Test broadcast ended; resuming the scheduled slot.")
            self._install_test(None)
            return

        checksum = str(test.get("checksum_sha256") or "")
        url = str(test.get("url") or "")
        if not checksum or not url:
            return

        if not self._cache.has(checksum):
            self._cache.ensure(MediaNeed(url=url, checksum_sha256=checksum))
        if not self._cache.is_playable(checksum):
            log.warning("Test media could not be cached; not shown.")
            return

        is_video = test.get("type") == "video"
        item = PlayItem(
            slot_id=-2,
            ad_id=None,  # a test never bills; ad_id stays out of the play log
            kind="video" if is_video else "image",
            local_src=self._cache.local_url_path(checksum),
            muted=not is_video or bool(test.get("muted", True)),
            duration_seconds=0,  # loops, so duration is irrelevant
            priority="test",
            label="TEST",
            loop=True,
        )
        self._install_test(item)
        log.info("Test broadcast active (live).")

    def _install_test(self, item: PlayItem | None) -> None:
        """
        Swap the live test override in, and rebuild the stored plan around it.

        The test is baked into `self._schedule` (that is what the local server
        reads on the fast path), so changing `self._test` alone would leave a
        stale test on screen until the next fetch. Rebuilding here keeps the
        stored plan the single source of truth for what plays, so the change
        is visible on the very next /state poll.
        """
        with self._lock:
            self._test = item
            self._schedule = Schedule(
                self._schedule.manifest,
                self._cache,
                fallback=self._fallback,
                test=item,
            )

    def _heartbeat_body(self) -> dict:
        health = read_health(self._config.cache_dir, self._cache.used_bytes())

        with self._lock:
            current = self._current
            version = self._schedule.schedule_version

        # Taken and cleared together, so a failed heartbeat retries the whole
        # set on the next beat.
        acks = self._take_acks()
        os_update = self._read_os_update()
        display = self._read_display_health()

        return {
            "contract_version": "player_heartbeat.v1",
            "stand_id": self._config.stand_id,
            "player_version": _version(),
            "schedule_version": version,
            # Only a REAL schedule slot belongs here. The fallback (-1), a test
            # broadcast and an emergency takeover (both -2) are synthetic items
            # with no row behind them, and sending their sentinel id was not
            # merely untidy: Dashboard's column is unsigned, so every heartbeat
            # carrying -2 was rejected outright — and because the whole request
            # died with it, the control channel died too. Commands were never
            # delivered for as long as a test or a takeover was on screen,
            # which looked exactly like a device ignoring every button.
            # `state` and `test_active` already say what is playing instead.
            "current_slot_id": (
                current.slot_id
                if current is not None and current.slot_id is not None and current.slot_id >= 0
                else None
            ),
            "current_ad_id": current.ad_id if current else None,
            "state": "fallback" if (current is None or current.is_fallback) else "playing",
            # Whether a test broadcast is what is actually on the glass right
            # now — so Dashboard can confirm a test really started (and really
            # stopped) instead of assuming the button worked.
            "test_active": bool(current is not None and current.is_test),
            "uptime_seconds": int((datetime.now(tz=UTC) - self._boot).total_seconds()),
            "disk_free_bytes": health.disk_free_bytes,
            "cache_used_bytes": health.cache_used_bytes,
            "temp_c": health.temp_c,
            "cpu_percent": health.cpu_percent,
            "mem_percent": health.mem_percent,
            # False when the SD has gone read-only — a dying card, flagged so
            # ops can swap it before it fails outright (Feature: SD armor).
            "storage_writable": health.storage_writable,
            "network_ok": True,  # we only get here having reached Dashboard
            # How far the device's own clock is from Dashboard's (device minus
            # server, seconds). We correct for it internally; this is reported
            # so an operator can spot a stand whose NTP has quietly failed.
            # Null until the first successful fetch has taught us the offset.
            "clock_offset_seconds": (
                round(self._clock_skew_seconds, 1)
                if self._clock_skew_seconds is not None else None
            ),
            # Where this device lives on the shop's own network. Dashboard has
            # always accepted this field and the device never sent it, so the
            # one address an operator needs to reach a stand was known only for
            # the few minutes between enrollment and adoption, then lost.
            "local_ip": read_local_ip(),
            "media_missing_count": 0,
            "pending_log_entries": self._playback.pending(),
            "last_error": "; ".join(health.warnings) or None,
            # Commands finished since the last beat. The ids mark them done
            # (unchanged, older-Dashboard-compatible); command_results carries
            # the same set with a status and a human line for each.
            "completed_commands": [a["id"] for a in acks],
            "command_results": acks,
            # The latest OS-update outcome, if one has ever run, so the setup
            # page can show "ok · 3 packages" or the error next to the button.
            "os_update_status": os_update.get("status"),
            "os_update_detail": os_update.get("detail"),
            "os_update_at": os_update.get("at"),
            # Verified playback, from the display driver: what src is truly on
            # the panel, whether mpv's clock is advancing, and how many freezes
            # it has recovered from — so a distant screen's real state is
            # visible, not merely what was scheduled. Null when no driver has
            # reported yet (e.g. the browser path). See _read_display_health.
            "display": {
                "src": display.get("src"),
                "playing": display.get("playing"),
                "freeze_recoveries": display.get("freeze_recoveries"),
                "at": display.get("at"),
            } if display else None,
            # How many cached files failed the decode probe and are quarantined
            # (Feature: media pre-validation), so Dashboard can flag a stand
            # holding media it cannot actually play.
            "undecodable_count": self._cache.undecodable_count(),
            # Which boot self-test checks failed (empty when all passed), so a
            # stand that came up degraded is visible without a site visit.
            "self_test_failures": diagnostics.failures(self._boot_checks),
        }

    def _take_acks(self) -> list[dict]:
        acks = self._pending_acks
        self._pending_acks = []
        return acks

    def _upload_playback(self) -> None:
        batch = self._playback.take_batch()
        if not batch:
            return

        body = {
            "contract_version": "player_playback.v1",
            "player_version": _version(),
            "entries": [
                {
                    "slot_id": e.slot_id,
                    "ad_id": e.ad_id,
                    "started_at": e.started_at,
                    "ended_at": e.ended_at,
                    "played_seconds": e.played_seconds,
                    "outcome": e.outcome,
                    "schedule_version": e.schedule_version,
                    "detail": e.detail,
                    "verification": e.verification,
                }
                for e in batch
            ],
        }
        result = self._api.send_playback(body)
        if result is not None:
            # Acknowledged, so drop them. A None leaves them buffered for
            # the next tick — safe, because the server is idempotent.
            self._playback.ack(batch)
            log.info("Uploaded %d playback entries.", len(batch))

    # ── Internals ────────────────────────────────────────────────────────

    def _held_version(self) -> int | None:
        with self._lock:
            version = self._schedule.schedule_version
        return version or None

    def _schedule_ref(self) -> Schedule:
        with self._lock:
            return self._schedule

    def _safe(self, fn, default):
        """
        Run one loop body, swallowing anything it throws.

        This is the anti-crash core in software form: a bug in any single
        cycle logs a stack trace and the loop lives to run the next one. A
        Pi in a shop window that keeps limping is worth more than one that
        exits cleanly on the first unexpected error.
        """
        try:
            return fn()
        except Exception:  # noqa: BLE001 — deliberately catch-all; the loop must survive
            log.exception("A loop cycle failed; continuing.")
            return default


def _version() -> str:
    from . import __version__

    return __version__


def _mono() -> float:
    import time

    return time.monotonic()
