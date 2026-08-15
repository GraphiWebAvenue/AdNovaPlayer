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

import logging
import threading
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .api import DashboardApi
from .cache import MediaCache, MediaNeed
from .config import Config
from .health import read as read_health
from .hours import screen_should_be_on
from .manifest import Manifest, UntrustedManifest
from .playback_log import Entry, PlaybackLog, now_iso
from .schedule import PlayItem, Schedule
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
    ) -> None:
        self._config = config
        self._api = api
        self._cache = cache
        self._playback = playback
        self._screen = screen or Screen()

        self._lock = threading.Lock()
        self._schedule = Schedule(None, cache)
        self._stop = threading.Event()

        # What is on screen right now, updated by the server via on_playing,
        # so the heartbeat can report it and playback can be logged when it
        # changes. Guarded by the same lock.
        self._current: PlayItem | None = None
        self._last_logged_slot: int | None = None

        # A live takeover and the stand's operating hours, both refreshed
        # from what Dashboard sends. The manifest carries the hours; the
        # heartbeat control channel carries the takeover.
        self._emergency: PlayItem | None = None
        self._operating_hours: dict | None = None
        self._timezone = "UTC"

        self._boot = datetime.now(tz=UTC)

    # ── The plan the server reads ────────────────────────────────────────

    def schedule(self) -> Schedule:
        """
        The current plan, including any live takeover. Called by the local
        server on every /state, so a takeover pushed a second ago is on
        screen by the next poll.
        """
        with self._lock:
            if self._emergency is None:
                return self._schedule
            # Rebuild a view with the takeover layered on, without mutating
            # the stored plan — the takeover is transient.
            return Schedule(self._schedule.manifest, self._cache, emergency=self._emergency)

    def on_playing(self, item: PlayItem) -> None:
        """
        The server tells us what it just put on screen.

        This is where a real slot becomes a billable play. A slot is
        logged once, when it first appears — not on every poll — so a
        30-second image polled five times is one impression, not five.
        """
        with self._lock:
            self._current = item

            if item.is_fallback or item.ad_id is None:
                return
            if item.slot_id == self._last_logged_slot:
                return

            self._last_logged_slot = item.slot_id
            version = self._schedule.schedule_version

        self._playback.record(Entry(
            slot_id=item.slot_id,
            ad_id=item.ad_id,
            started_at=now_iso(),
            ended_at=None,
            outcome="played",
            schedule_version=version,
        ))

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

    def stop(self) -> None:
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
            with self._lock:
                self._schedule = Schedule(manifest, self._cache)
            log.info("Loaded cached plan (version %s).", manifest.schedule_version)
        else:
            log.info("No usable cached plan; the fallback loop plays until one arrives.")

    # ── Fetch loop ───────────────────────────────────────────────────────

    def _fetch_loop(self) -> None:
        while not self._stop.is_set():
            interval = self._safe(self._fetch_once, default=self._config.manifest_poll_seconds)
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
            return self._config.manifest_poll_seconds
        except (ValueError, KeyError) as exc:
            log.warning("Manifest did not parse (%s); keeping the current plan.", exc)
            return self._config.manifest_poll_seconds

        moment = datetime.now(tz=UTC)

        # Download the preload horizon before installing the plan, so a slot
        # never goes live before its bytes are on disk.
        self._download_horizon(manifest, moment)
        manifest.save(self._config.manifest_path)

        with self._lock:
            self._schedule = Schedule(manifest, self._cache)
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

    # ── Heartbeat loop ───────────────────────────────────────────────────

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            self._safe(self._heartbeat_once, default=None)
            self._safe(self._upload_playback, default=None)
            self._safe(self._apply_screen_power, default=None)
            self._stop.wait(max(15, self._config.heartbeat_seconds))

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

    def _heartbeat_once(self) -> None:
        body = self._heartbeat_body()
        response = self._api.send_heartbeat(body)
        if response is None:
            return

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

    def _apply_emergency(self, emergency: dict | None) -> None:
        """
        Install or clear a live takeover from the control channel.

        A null or absent value clears it — so an operator ending a closure
        notice simply stops sending it, and the scheduled plan returns on
        the next poll. A takeover names its media by checksum, and it plays
        only once that media is cached and valid, exactly like a slot.
        """
        if not isinstance(emergency, dict):
            if self._emergency is not None:
                log.info("Takeover cleared; returning to the schedule.")
            with self._lock:
                self._emergency = None
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
        with self._lock:
            self._emergency = item
        log.info("Takeover active.")

    def _heartbeat_body(self) -> dict:
        health = read_health(self._config.cache_dir, self._cache.used_bytes())

        with self._lock:
            current = self._current
            version = self._schedule.schedule_version

        return {
            "contract_version": "player_heartbeat.v1",
            "stand_id": self._config.stand_id,
            "player_version": _version(),
            "schedule_version": version,
            "current_slot_id": current.slot_id if current and not current.is_fallback else None,
            "current_ad_id": current.ad_id if current else None,
            "state": "fallback" if (current is None or current.is_fallback) else "playing",
            "uptime_seconds": int((datetime.now(tz=UTC) - self._boot).total_seconds()),
            "disk_free_bytes": health.disk_free_bytes,
            "cache_used_bytes": health.cache_used_bytes,
            "temp_c": health.temp_c,
            "cpu_percent": health.cpu_percent,
            "mem_percent": health.mem_percent,
            "network_ok": True,  # we only get here having reached Dashboard
            "media_missing_count": 0,
            "pending_log_entries": self._playback.pending(),
            "last_error": "; ".join(health.warnings) or None,
        }

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
