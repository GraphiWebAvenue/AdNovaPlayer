"""
Important device events, buffered and shipped to Dashboard.

A second instance of the playback-log pattern: a disk-backed, bounded ring
that never fills the card, drained best-effort after a successful heartbeat
and removed only once the server acks. It carries a compact stream of the
things worth a forensic record — a refused manifest, a lost key, a command
run — never per-frame chatter, so a device can be reconstructed after an
incident without flooding the network or the audit trail.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("adnova.events")

# A couple of thousand events is tiny on disk and holds well past any burst;
# the oldest are dropped first, exactly like the playback ring.
MAX_EVENTS = 2000


@dataclass(frozen=True)
class Event:
    event_id: str
    at: str
    sev: str  # info | warn | error | security
    code: str
    detail: str | None = None


class EventLog:
    """A disk-backed ring of important events, deduped by event_id on ack."""

    def __init__(self, path: Path, max_events: int = MAX_EVENTS) -> None:
        self._path = path
        self._max = max_events
        self._lock = threading.Lock()
        self._events: deque[Event] = deque(maxlen=max_events)
        self._load()

    def record(self, code: str, sev: str = "info", detail: str | None = None) -> None:
        """Append one event. Oldest is dropped if the ring is full."""
        event = Event(
            event_id=uuid.uuid4().hex,
            at=datetime.now(tz=UTC).isoformat(),
            sev=sev,
            code=code[:80],
            detail=(detail or None) and str(detail)[:200],
        )
        with self._lock:
            self._events.append(event)
            self._flush()

    def pending(self) -> int:
        with self._lock:
            return len(self._events)

    def take_batch(self, limit: int = 200) -> list[Event]:
        """The oldest events, up to `limit`, without removing them."""
        with self._lock:
            return list(self._events)[:limit]

    def ack(self, uploaded: list[Event]) -> None:
        """Drop events the server accepted. Idempotent."""
        if not uploaded:
            return
        ids = {e.event_id for e in uploaded}
        with self._lock:
            self._events = deque(
                (e for e in self._events if e.event_id not in ids),
                maxlen=self._max,
            )
            self._flush()

    # ── persistence ──────────────────────────────────────────────────────

    def _flush(self) -> None:
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps([asdict(e) for e in self._events]), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as exc:
            log.warning("Could not persist the event log: %s", exc)

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            rows = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Ignoring an unreadable event log (%s).", exc)
            return
        if not isinstance(rows, list):
            return
        for row in rows:
            try:
                self._events.append(Event(**row))
            except (TypeError, ValueError):
                continue
