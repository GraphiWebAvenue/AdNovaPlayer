"""
Proof of play, buffered against a flaky network.

Every slot that actually reaches the screen is recorded here, because
this is what advertisers are billed on. The device may be offline when a
slot plays, so entries are held on disk and uploaded when the network
returns — and the upload is idempotent server-side, keyed on
(slot, time), so a batch retried after a timeout never bills twice.

The buffer is bounded. A device offline for a week must not fill its card
with logs and then have no room for the media it is meant to play, so the
oldest entries are dropped once the ring is full. Losing the tail of a
long outage's logs is a smaller harm than a screen that cannot play.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("adnova.playback")

# A week of one-minute slots is ~10k. This ceiling holds well past any
# realistic outage while staying tiny on disk.
MAX_ENTRIES = 20000


@dataclass(frozen=True)
class Entry:
    slot_id: int
    ad_id: int | None
    started_at: str
    ended_at: str | None
    outcome: str  # played | partial | skipped | media_missing | stand_closed | interrupted | error
    played_seconds: float | None = None
    schedule_version: int | None = None
    detail: str | None = None
    # Verified capture (#8/#9/#11): optional proof the ad rendered on screen,
    # {"kind": "screenshot", "hash": "<sha256>", "captured_at": "<iso>"}.
    # None = unverified. Defaulted so rows written by older builds still load.
    verification: dict | None = None


class PlaybackLog:
    """
    A disk-backed ring of playback entries.

    Thread-safe: the playback side appends from the request path while the
    upload loop drains from another thread, so both take the lock. The
    on-disk copy survives a reboot — a device that logged plays and then
    lost power must still bill for them.
    """

    def __init__(self, path: Path, max_entries: int = MAX_ENTRIES) -> None:
        self._path = path
        self._max = max_entries
        self._lock = threading.Lock()
        self._entries: deque[Entry] = deque(maxlen=max_entries)
        self._load()

    def record(self, entry: Entry) -> None:
        """Append one play. Oldest is dropped if the ring is full."""
        with self._lock:
            self._entries.append(entry)
            self._flush()

    def finalize(
        self,
        slot_id: int,
        started_at: str,
        ended_at: str,
        played_seconds: float,
        outcome: str = "played",
        detail: str | None = None,
        verification: dict | None = None,
    ) -> None:
        """
        Close an open entry (one recorded on start with ended_at=None) with
        its real end time, duration, and outcome. Matched on
        (slot_id, started_at) so a re-flushed/reloaded ring still finds it.

        Recording on start and finalizing on end means a device that loses
        power mid-play still billed the impression (as an open `played`),
        while a device that runs normally gets accurate duration + a
        `partial` mark when a slot was cut short.
        """
        with self._lock:
            for i, e in enumerate(self._entries):
                if (
                    e.slot_id == slot_id
                    and e.started_at == started_at
                    and e.ended_at is None
                ):
                    self._entries[i] = Entry(
                        slot_id=e.slot_id,
                        ad_id=e.ad_id,
                        started_at=e.started_at,
                        ended_at=ended_at,
                        outcome=outcome,
                        played_seconds=played_seconds,
                        schedule_version=e.schedule_version,
                        detail=detail or e.detail,
                        verification=verification or e.verification,
                    )
                    self._flush()
                    return

    def pending(self) -> int:
        with self._lock:
            return len(self._entries)

    def take_batch(self, limit: int = 500) -> list[Entry]:
        """
        The oldest entries, up to `limit`, without removing them.

        They are removed only once the upload is acknowledged, via
        `ack`. Taking without removing is what makes a failed upload safe:
        the entries are still here for the next attempt.
        """
        with self._lock:
            return list(self._entries)[:limit]

    def ack(self, uploaded: list[Entry]) -> None:
        """Drop entries the server has accepted. Idempotent."""
        if not uploaded:
            return
        keys = {(e.slot_id, e.started_at) for e in uploaded}
        with self._lock:
            self._entries = deque(
                (e for e in self._entries if (e.slot_id, e.started_at) not in keys),
                maxlen=self._max,
            )
            self._flush()

    # ── persistence ──────────────────────────────────────────────────────

    def _flush(self) -> None:
        """Write the whole ring atomically. Called under the lock."""
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps([asdict(e) for e in self._entries]),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except OSError as exc:
            # A device that cannot persist its log still keeps the in-memory
            # copy and uploads from that; losing durability is bad, losing
            # the loop is worse.
            log.warning("Could not persist the playback log: %s", exc)

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            rows = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Ignoring an unreadable playback log (%s).", exc)
            return
        if not isinstance(rows, list):
            log.warning("Playback log was not a list; ignoring it.")
            return

        # Skip individual corrupt rows rather than dropping the whole file —
        # SD flips a byte here and there, and one bad record must not lose
        # every billable impression around it.
        kept, dropped = 0, 0
        for row in rows:
            try:
                self._entries.append(Entry(**row))
                kept += 1
            except (TypeError, ValueError):
                dropped += 1
        if dropped:
            log.warning("Dropped %d corrupt playback row(s); kept %d.", dropped, kept)


def now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()
