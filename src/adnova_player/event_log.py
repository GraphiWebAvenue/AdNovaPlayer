"""
The device's forensic record: what happened here, in order, provably.

A player sits unattended in a shop, on a network nobody here controls,
behind a door anybody can walk through. If it is ever tampered with, the
only witness is the device itself — so this module is written to be the
witness that survives the tampering.

Four properties make it one:

**Ordered.** Every event carries a monotonic `seq` that survives reboots,
re-installs and a wiped ring. Dashboard tracks the highest seq it has seen
per stand, so deleting events from the device does not delete them from
the story: the next shipment arrives with a gap, and a gap is itself an
alarm.

**Chained.** Each event's `hash` commits to the previous one. Editing a
line in the file breaks every hash after it. Combined with the seq, the
only undetectable attack is deleting the tail *and* never letting the
device talk again — at which point the silence is the alarm.

**Kept twice.** The ring is a shipping queue: events leave it once
Dashboard acks. The archive is the local copy, appended the moment an
event is recorded and rotated by size, so a technician standing in front
of an offline device can still read what happened.

**Unfloodable.** A flapping fault could otherwise fill the card and the
audit trail with one repeated line. Ordinary codes are rate-limited per
window and collapsed into a single "+N suppressed" note. Security events
are never suppressed — the whole point of a flood may be to bury one.

Nothing here may raise into a caller. A device that stops playing because
its logger had a bad day is a worse outcome than a missing log line.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("adnova.events")

# A couple of thousand events is tiny on disk and holds well past any burst;
# the oldest are dropped first, exactly like the playback ring.
MAX_EVENTS = 2000

# The local copy. Big enough to hold weeks of ordinary operation on a device
# that never phones home, small enough to be irrelevant next to the media.
ARCHIVE_MAX_BYTES = 2 * 1024 * 1024
ARCHIVE_KEEP = 2

# Flood control for non-security codes: at most BURST events with the same
# code per WINDOW seconds, then one collapsed note per window.
FLOOD_WINDOW_SECONDS = 300
FLOOD_BURST = 12

SEVERITIES = ("info", "warn", "error", "security")

# Anything whose value could be a credential is never written, at any depth.
# The device holds exactly one real secret (the stand key) but a detail string
# is assembled from whatever the caller had to hand, so this is a net, not a
# list of known cases.
#
# The leading `[A-Za-z_]*` matters more than it looks: `stand_key` and
# `api_key` do not start on a word boundary at "key", so a `\b` anchor would
# have walked straight past the one secret this device actually holds.
#
# An explicit `=` or `:` is required before the value. Without it, an ordinary
# sentence ("no signing key matches this manifest") would be mangled into
# `key=***`, and a trail nobody can read is its own kind of failure.
_SECRETISH = re.compile(
    r"(?i)([A-Za-z_]*(?:key|token|secret|password|signature|nonce|authorization))"
    r"\s*[=:]\s*(?:Bearer|Basic|Token|Digest)?\s*[\x22\x27]?([A-Za-z0-9+/=_.-]{6,})"
)
# A bare 64-hex run is the shape of the stand key and of every sha256 we would
# ever quote; truncate it rather than print it.
_LONGHEX = re.compile(r"\b[0-9a-fA-F]{64}\b")


def _redact(text: str) -> str:
    """Blank anything that looks like a credential. Never raises."""
    try:
        out = _SECRETISH.sub(lambda m: m.group(1) + "=***", text)
        return _LONGHEX.sub(lambda m: m.group(0)[:8] + "…", out)
    except Exception:  # pragma: no cover - a redactor that throws is worse than none
        return "***"


@dataclass(frozen=True)
class Event:
    event_id: str
    at: str
    sev: str  # info | warn | error | security
    code: str
    detail: str | None = None
    # The forensic spine. Older devices sent none of these; the receiver
    # treats them as optional so a mixed fleet keeps reporting.
    seq: int = 0
    prev: str | None = None
    hash: str | None = None
    # Correlation id, when the event belongs to a traced action, and an
    # optional subject id (slot / ad / command).
    rid: str | None = None
    ref: int | None = None


@dataclass
class _FloodState:
    window_started: float = 0.0
    emitted: int = 0
    suppressed: int = 0


@dataclass
class _Chain:
    """The head of the chain, persisted so it survives a drained ring."""

    seq: int = 0
    hash: str | None = None
    # Set once, the first time this device ever logs. Lets Dashboard tell "a
    # re-imaged device restarting its chain" apart from "someone wiped the
    # file", which look identical from the seq alone.
    boot_id: str = field(default_factory=lambda: uuid.uuid4().hex)


class EventLog:
    """
    A disk-backed, hash-chained ring of important events.

    The public surface is deliberately the same four calls the shipping loop
    already used — `record`, `pending`, `take_batch`, `ack` — so nothing about
    how events leave the device had to change to make them trustworthy.
    """

    def __init__(
        self,
        path: Path,
        max_events: int = MAX_EVENTS,
        *,
        archive: bool = True,
        clock=None,
    ) -> None:
        self._path = path
        self._archive_path = path.with_name(path.stem + "-archive.jsonl")
        self._archive = archive
        self._max = max_events
        self._lock = threading.RLock()
        self._events: deque[Event] = deque(maxlen=max_events)
        self._chain = _Chain()
        self._flood: dict[str, _FloodState] = {}
        # Injectable only so tests can drive the flood window deterministically.
        self._monotonic = clock or time.monotonic
        self._load()

    # ── recording ────────────────────────────────────────────────────────

    @property
    def boot_id(self) -> str:
        return self._chain.boot_id

    @property
    def sequence(self) -> int:
        with self._lock:
            return self._chain.seq

    def record(
        self,
        code: str,
        sev: str = "info",
        detail: str | None = None,
        *,
        rid: str | None = None,
        ref: int | None = None,
    ) -> None:
        """
        Append one event, chained to the last. Never raises.

        `sev` decides two things: whether the event can be suppressed by flood
        control, and how loudly Dashboard reacts. "security" is reserved for
        things an attacker would cause — a refused manifest, a failed login, a
        rejected key — and is never dropped, never collapsed.
        """
        try:
            self._record(code, sev, detail, rid, ref)
        except Exception as exc:  # pragma: no cover - defensive by contract
            log.warning("Could not record the event %s: %s", code, exc)

    def _record(
        self,
        code: str,
        sev: str,
        detail: str | None,
        rid: str | None,
        ref: int | None,
    ) -> None:
        sev = sev if sev in SEVERITIES else "info"
        code = str(code)[:80]
        text = _redact(str(detail))[:200] if detail is not None else None

        with self._lock:
            if sev != "security" and not self._allow(code):
                return
            self._append(code, sev, text, rid, ref)
            self._flush()

    def _append(
        self,
        code: str,
        sev: str,
        detail: str | None,
        rid: str | None,
        ref: int | None,
    ) -> None:
        """Build the next link and put it in both the ring and the archive."""
        self._chain.seq += 1
        prev = self._chain.hash
        body = {
            "seq": self._chain.seq,
            "at": datetime.now(tz=UTC).isoformat(),
            "sev": sev,
            "code": code,
            "detail": detail,
            "rid": rid,
            "ref": ref,
            "boot_id": self._chain.boot_id,
        }
        digest = _digest(prev, body)
        self._chain.hash = digest

        event = Event(
            event_id=uuid.uuid4().hex,
            at=str(body["at"]),
            sev=sev,
            code=code,
            detail=detail,
            seq=self._chain.seq,
            prev=prev,
            hash=digest,
            rid=rid,
            ref=ref,
        )
        self._events.append(event)
        self._write_archive(event)

    def _allow(self, code: str) -> bool:
        """
        Flood control, per code.

        A code that fires more than FLOOD_BURST times in a window is muted for
        the rest of it and counted; the first event of the next window carries
        the tally, so the trail says "this happened 400 times" instead of
        holding 400 identical lines or, worse, dropping them silently.
        """
        now = self._monotonic()
        state = self._flood.setdefault(code, _FloodState(window_started=now))

        if now - state.window_started >= FLOOD_WINDOW_SECONDS:
            carried = state.suppressed
            state.window_started = now
            state.emitted = 0
            state.suppressed = 0
            if carried:
                # Recorded before the caller's own event, so the tally reads in
                # the right order.
                self._append(
                    code,
                    "warn",
                    f"+{carried} suppressed in the previous {FLOOD_WINDOW_SECONDS}s",
                    None,
                    None,
                )

        if state.emitted >= FLOOD_BURST:
            state.suppressed += 1
            return False

        state.emitted += 1
        return True

    # ── shipping ─────────────────────────────────────────────────────────

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

    def recent(self, limit: int = 50) -> list[dict]:
        """
        The newest events for the on-site status page, newest first.

        Reads the archive so a technician sees the real history and not just
        whatever has yet to be shipped — an idle device with a healthy uplink
        has an empty ring and plenty of story.
        """
        rows: list[dict] = []
        try:
            if self._archive_path.exists():
                with self._archive_path.open("r", encoding="utf-8") as handle:
                    tail = deque(handle, maxlen=limit)
                rows = [json.loads(line) for line in tail if line.strip()]
        except (OSError, ValueError) as exc:
            log.warning("Could not read the event archive: %s", exc)
        if not rows:
            with self._lock:
                rows = [asdict(e) for e in list(self._events)[-limit:]]
        rows.reverse()
        return rows

    def verify(self) -> tuple[bool, str | None]:
        """
        Re-walk the archived chain. Returns (ok, the first broken seq).

        Cheap enough to run from the boot self-test: a break means a line was
        edited or removed on this card, which is worth saying out loud even
        though the authoritative copy already left for Dashboard.
        """
        try:
            if not self._archive_path.exists():
                return True, None
            prev: str | None = None
            with self._archive_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    body = {
                        "seq": row.get("seq"),
                        "at": row.get("at"),
                        "sev": row.get("sev"),
                        "code": row.get("code"),
                        "detail": row.get("detail"),
                        "rid": row.get("rid"),
                        "ref": row.get("ref"),
                        "boot_id": row.get("boot_id", self._chain.boot_id),
                    }
                    if row.get("hash") != _digest(row.get("prev"), body):
                        return False, str(row.get("seq"))
                    if prev is not None and row.get("prev") != prev:
                        return False, str(row.get("seq"))
                    prev = row.get("hash")
            return True, None
        except (OSError, ValueError) as exc:
            log.warning("Could not verify the event archive: %s", exc)
            return True, None

    # ── persistence ──────────────────────────────────────────────────────

    def _write_archive(self, event: Event) -> None:
        """Append to the local copy, rotating by size. Never raises."""
        if not self._archive:
            return
        try:
            self._archive_path.parent.mkdir(parents=True, exist_ok=True)
            row = asdict(event)
            row["boot_id"] = self._chain.boot_id
            with self._archive_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            if self._archive_path.stat().st_size > ARCHIVE_MAX_BYTES:
                self._rotate()
        except OSError as exc:
            log.warning("Could not append to the event archive: %s", exc)

    def _rotate(self) -> None:
        try:
            oldest = self._archive_path.with_suffix(f".jsonl.{ARCHIVE_KEEP}")
            if oldest.exists():
                oldest.unlink()
            for index in range(ARCHIVE_KEEP - 1, 0, -1):
                src = self._archive_path.with_suffix(f".jsonl.{index}")
                if src.exists():
                    src.replace(self._archive_path.with_suffix(f".jsonl.{index + 1}"))
            self._archive_path.replace(self._archive_path.with_suffix(".jsonl.1"))
        except OSError as exc:
            log.warning("Could not rotate the event archive: %s", exc)

    def _flush(self) -> None:
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = {
            "chain": asdict(self._chain),
            "events": [asdict(e) for e in self._events],
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            # fsync before the rename: a power cut mid-write on a Pi is the
            # normal case, not the rare one, and a torn state file would lose
            # the chain head and look exactly like tampering.
            try:
                with tmp.open("rb") as handle:
                    os.fsync(handle.fileno())
            except OSError:
                pass
            tmp.replace(self._path)
        except OSError as exc:
            log.warning("Could not persist the event log: %s", exc)

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Ignoring an unreadable event log (%s).", exc)
            return

        # v1.6.0 and earlier wrote a bare list of events with no chain. Read
        # it, keep the events, and start the chain from there rather than
        # discarding a device's pending trail on upgrade.
        if isinstance(data, list):
            rows, chain = data, None
        elif isinstance(data, dict):
            rows, chain = data.get("events", []), data.get("chain")
        else:
            return

        if isinstance(chain, dict):
            self._chain = _Chain(
                seq=int(chain.get("seq") or 0),
                hash=chain.get("hash"),
                boot_id=str(chain.get("boot_id") or uuid.uuid4().hex),
            )

        if not isinstance(rows, list):
            return
        fields = set(Event.__dataclass_fields__)
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                self._events.append(Event(**{k: v for k, v in row.items() if k in fields}))
            except (TypeError, ValueError):
                continue


def _digest(prev: str | None, body: dict) -> str:
    payload = (prev or "") + "|" + json.dumps(body, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ── the process-wide log ────────────────────────────────────────────────
#
# Most of what is worth recording happens in modules that have no reason to
# know about the agent — the admin page's login check, the enrollment client,
# the HTTP layer's auth failures. Threading an EventLog through all of them
# would couple half the codebase to it, so there is one process-wide log,
# initialised once by main.py.
#
# Before it exists, calls are buffered rather than dropped: the most
# interesting minute in a device's life is the one before its config loaded.

_log: EventLog | None = None
_preinit: deque[tuple[str, str, str | None, str | None, int | None]] = deque(maxlen=200)
_init_lock = threading.Lock()


def init(path: Path, **kwargs) -> EventLog:
    """Create the process-wide log and drain anything recorded before it."""
    global _log
    with _init_lock:
        _log = EventLog(path, **kwargs)
        buffered = list(_preinit)
        _preinit.clear()
    for code, sev, detail, rid, ref in buffered:
        _log.record(code, sev, detail, rid=rid, ref=ref)
    return _log


def adopt(existing: EventLog) -> EventLog:
    """
    Make an already-built log the process-wide one, draining the buffer.

    The agent owns its log's path (it lives beside the playback log, under the
    configured cache dir) but everything else in the process needs to reach the
    same one. Rather than have two ideas of where events go, whichever side
    builds it first publishes it here.
    """
    global _log
    with _init_lock:
        _log = existing
        buffered = list(_preinit)
        _preinit.clear()
    for code, sev, detail, rid, ref in buffered:
        existing.record(code, sev, detail, rid=rid, ref=ref)
    return existing


def current() -> EventLog | None:
    """The process-wide log, or None before init(). For callers that must know."""
    return _log


def record(
    code: str,
    sev: str = "info",
    detail: str | None = None,
    *,
    rid: str | None = None,
    ref: int | None = None,
) -> None:
    """Record on the process-wide log, buffering if it does not exist yet."""
    live = _log
    if live is not None:
        live.record(code, sev, detail, rid=rid, ref=ref)
        return
    with _init_lock:
        if _log is None:
            _preinit.append((code, sev, detail, rid, ref))
            return
    _log.record(code, sev, detail, rid=rid, ref=ref)


def reset_for_tests() -> None:
    """Forget the process-wide log. Only tests should call this."""
    global _log
    with _init_lock:
        _log = None
        _preinit.clear()
