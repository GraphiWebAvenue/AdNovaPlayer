"""
The local schedule.

Two rules shape everything here:

**The cache is the source of truth for playback.** Deciding what plays next
must resolve from local data in under 50 ms, without touching the network.
A stand on a flaky connection plays correctly; a stand with no connection
at all plays the last good plan until it runs out.

**A manifest that fails to parse is discarded, not applied.** Keeping the
previous plan is always better than adopting a broken one — the screen is
in a shop window and nobody is watching it.

The same rule covers a manifest that fails to *verify*. See `trust.py`
for why the signature matters more than the transport does.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .trust import Verdict, verify

log = logging.getLogger("adnova.manifest")

CONTRACT_VERSION = "player_manifest.v1"

# Higher wins when two slots somehow overlap — a clock skew, a hand-edit
# race, a contract bug. Never let two things fight for the screen.
PRIORITY_ORDER = {"urgent": 3, "manual": 2, "template": 1, "ai": 0}


class UntrustedManifest(ValueError):
    """
    Raised when a manifest cannot be proven to come from Dashboard.

    A subclass of ValueError so every existing caller that already treats
    a bad manifest as "keep the old plan" does the right thing without
    being changed — but distinguishable, because this one deserves an
    alert while a malformed field only deserves a log line.
    """

    def __init__(self, verdict: Verdict) -> None:
        super().__init__(verdict.reason)
        self.verdict = verdict


@dataclass(frozen=True)
class Media:
    url: str
    type: str
    checksum_sha256: str
    bytes: int
    expires_at: datetime | None = None

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> Media:
        return cls(
            url=str(raw["url"]),
            type=str(raw.get("type", "image")),
            checksum_sha256=str(raw["checksum_sha256"]),
            bytes=int(raw.get("bytes", 0)),
            expires_at=_dt_optional(raw.get("expires_at")),
        )


@dataclass(frozen=True)
class Slot:
    slot_id: int
    ad_id: int | None
    starts_at: datetime
    ends_at: datetime
    duration_seconds: int
    priority: str
    media: Media
    label: str | None = None

    @property
    def rank(self) -> int:
        return PRIORITY_ORDER.get(self.priority, 0)

    def covers(self, moment: datetime) -> bool:
        return self.starts_at <= moment < self.ends_at

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> Slot:
        return cls(
            slot_id=int(raw["slot_id"]),
            ad_id=raw.get("ad_id"),
            starts_at=_dt(raw["starts_at"]),
            ends_at=_dt(raw["ends_at"]),
            duration_seconds=int(raw["duration_seconds"]),
            priority=str(raw.get("priority", "ai")),
            media=Media.parse(raw["media"]),
            label=raw.get("label"),
        )


@dataclass
class Manifest:
    stand_id: int
    schedule_version: int
    server_time: datetime
    window_from: datetime
    window_to: datetime
    slots: list[Slot] = field(default_factory=list)
    preload_hours: int = 6
    manifest_poll_seconds: int = 600
    heartbeat_seconds: int = 60
    fetched_at: datetime | None = None

    # The document exactly as it arrived, kept so the cache can be written
    # back byte for byte. Re-serialising from the parsed fields would
    # change the canonical form — a reordered key, a differently formatted
    # timestamp — and the signature would stop verifying on the next boot,
    # which is precisely when the device most needs its last good plan.
    raw: dict[str, Any] | None = field(default=None, repr=False)

    # ── parsing ─────────────────────────────────────────────────────────

    @classmethod
    def parse(
        cls,
        raw: dict[str, Any],
        *,
        public_keys: dict[str, str] | None = None,
        require_signature: bool = True,
    ) -> Manifest:
        """
        Strict about what it needs, forgiving about everything else.

        A slot that cannot be read is dropped with a warning rather than
        failing the whole manifest — losing one placement is a gap; losing
        the manifest is a dark screen.

        The signature is the one exception to that forgiveness, and it is
        checked before anything else is read. A manifest we cannot prove
        Dashboard wrote is not a slightly damaged plan we can salvage
        slots from; it is somebody else's plan, and none of it is used.

        Passing `public_keys=None` skips the check entirely. That is for
        tests and for the pre-signing fleet — a device in the field is
        configured with its keys and never reaches this path.
        """
        if public_keys is not None:
            verdict = verify(raw, public_keys, required=require_signature)
            if not verdict:
                raise UntrustedManifest(verdict)

        if raw.get("contract_version") != CONTRACT_VERSION:
            raise ValueError(
                f"Expected {CONTRACT_VERSION}, got {raw.get('contract_version')!r}"
            )

        slots: list[Slot] = []
        for entry in raw.get("slots", []):
            try:
                slots.append(Slot.parse(entry))
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("Dropping an unreadable slot: %s", exc)

        poll = raw.get("poll") or {}
        preload = raw.get("preload") or {}
        window = raw.get("window") or {}

        return cls(
            stand_id=int(raw["stand_id"]),
            schedule_version=int(raw.get("schedule_version", 0)),
            server_time=_dt(raw["server_time"]),
            window_from=_dt(window.get("from") or raw["server_time"]),
            window_to=_dt(window.get("to") or raw["server_time"]),
            slots=sorted(slots, key=lambda s: s.starts_at),
            preload_hours=int(preload.get("hours_ahead", 6)),
            manifest_poll_seconds=int(poll.get("manifest_seconds", 600)),
            heartbeat_seconds=int(poll.get("heartbeat_seconds", 60)),
            fetched_at=datetime.now(tz=UTC),
            raw=raw,
        )

    # ── the hot path ────────────────────────────────────────────────────

    def slot_at(self, moment: datetime) -> Slot | None:
        """
        What should be on screen right now.

        Linear over a sorted list: a 72-hour manifest holds a few thousand
        slots at most, and a scan of that is microseconds. An index would
        be a second thing to keep correct for no measurable gain.

        Overlaps should not happen. When they do, the higher priority wins
        rather than whichever the loop reached first — a deterministic
        answer beats an arbitrary one.
        """
        best: Slot | None = None
        for slot in self.slots:
            if slot.starts_at > moment:
                break
            if slot.covers(moment) and (best is None or slot.rank > best.rank):
                best = slot
        return best

    def next_slot_after(self, moment: datetime) -> Slot | None:
        for slot in self.slots:
            if slot.starts_at > moment:
                return slot
        return None

    def slots_to_preload(self, moment: datetime) -> list[Slot]:
        """Distinct media coming up inside the preload horizon."""
        horizon = moment + timedelta(hours=self.preload_hours)
        seen: set[str] = set()
        upcoming: list[Slot] = []

        for slot in self.slots:
            if slot.ends_at <= moment or slot.starts_at > horizon:
                continue
            if slot.media.checksum_sha256 in seen:
                continue
            seen.add(slot.media.checksum_sha256)
            upcoming.append(slot)

        return upcoming

    def is_exhausted(self, moment: datetime) -> bool:
        """True once the plan has nothing left to say about the future."""
        return not self.slots or self.slots[-1].ends_at <= moment

    # ── persistence ─────────────────────────────────────────────────────

    def save(self, path: Path) -> None:
        """
        Write atomically.

        A power cut halfway through a write would otherwise leave a
        truncated file that fails to parse on boot — and boot is exactly
        when the device most needs its last known good plan.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")

        # The original document when we have it, so the signature survives
        # the round trip; the reconstructed one only for a manifest that
        # was built rather than fetched (tests, the bundled fallback).
        document = self.raw if self.raw is not None else self.to_dict()

        temp.write_text(json.dumps(document, indent=2), encoding="utf-8")
        temp.replace(path)

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        public_keys: dict[str, str] | None = None,
        require_signature: bool = True,
    ) -> Manifest | None:
        """
        Read the last good plan back.

        Verified again on the way in. The disk is the device's own, so this
        is not the main defence — but a manifest that was trusted when it
        was written and is not trusted now means somebody has had a hand in
        the filesystem, and the right answer is still to refuse it.
        """
        if not path.exists():
            return None
        try:
            return cls.parse(
                json.loads(path.read_text(encoding="utf-8")),
                public_keys=public_keys,
                require_signature=require_signature,
            )
        except UntrustedManifest as exc:
            log.error("Cached manifest is not trusted (%s); ignoring it.", exc)
            return None
        except (OSError, ValueError, KeyError) as exc:
            log.warning("Cached manifest is unusable (%s); ignoring it.", exc)
            return None

    def to_dict(self) -> dict[str, Any]:
        """
        The parsed manifest, rendered back out.

        Not signature-preserving by design — use `raw` for that. This
        exists for logs, diagnostics, and manifests the device built
        itself.
        """
        return {
            "contract_version": CONTRACT_VERSION,
            "stand_id": self.stand_id,
            "schedule_version": self.schedule_version,
            "server_time": self.server_time.isoformat(),
            "window": {
                "from": self.window_from.isoformat(),
                "to": self.window_to.isoformat(),
            },
            "preload": {"hours_ahead": self.preload_hours},
            "poll": {
                "manifest_seconds": self.manifest_poll_seconds,
                "heartbeat_seconds": self.heartbeat_seconds,
            },
            "slots": [
                {
                    "slot_id": s.slot_id,
                    "ad_id": s.ad_id,
                    "starts_at": s.starts_at.isoformat(),
                    "ends_at": s.ends_at.isoformat(),
                    "duration_seconds": s.duration_seconds,
                    "priority": s.priority,
                    "label": s.label,
                    "media": {
                        "url": s.media.url,
                        "type": s.media.type,
                        "checksum_sha256": s.media.checksum_sha256,
                        "bytes": s.media.bytes,
                        "expires_at": s.media.expires_at.isoformat()
                        if s.media.expires_at
                        else None,
                    },
                }
                for s in self.slots
            ],
        }


def _dt(value: Any) -> datetime:
    """Parse a required timestamp. Raises when it is absent or malformed."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if value is None:
        raise ValueError("a required timestamp was null")

    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    # Naive timestamps would compare against an aware "now" and raise at the
    # worst moment. Assume UTC rather than crash on a slightly loose server.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _dt_optional(value: Any) -> datetime | None:
    """
    Parse a timestamp that is allowed to be absent.

    Kept separate from _dt on purpose: routing an optional field through
    the strict parser turns a missing `expires_at` into a dropped slot,
    which is a silently missing advertisement rather than a visible error.
    """
    if value in (None, ""):
        return None
    return _dt(value)
