"""
The bridge from a verified manifest to what the screen shows next.

The manifest layer proves a plan came from Dashboard. The cache layer
proves each file's bytes. This layer answers the only question the screen
actually asks — *what plays right now, and what plays after it* — and it
answers from local data in well under the 50 ms budget, because it runs
on every frame boundary and must never wait on a network.

It also decides what to do when the honest answer is "nothing": no slot
covers this moment, or the slot's media has not finished downloading. The
rule is absolute and lives here — never a black screen. When there is
nothing legitimate to show, the fallback loop shows instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .cache import MediaCache
from .manifest import Manifest, Slot


@dataclass(frozen=True)
class PlayItem:
    """One thing to put on screen, resolved to a local file the browser can load."""

    slot_id: int
    ad_id: int | None
    kind: str  # "image" | "video" | "fallback"
    local_src: str  # a /media/<checksum> path, or the bundled fallback
    muted: bool
    duration_seconds: int
    priority: str
    label: str | None = None

    @property
    def is_fallback(self) -> bool:
        return self.kind == "fallback"


# What the bundled loop looks like to the rest of the system: a synthetic
# item with no slot, shown whenever nothing real can be. The browser knows
# this src as its built-in filler.
FALLBACK = PlayItem(
    slot_id=-1,
    ad_id=None,
    kind="fallback",
    local_src="/fallback",
    muted=True,
    duration_seconds=10,
    priority="fallback",
    label="AdNova",
)


class Schedule:
    """
    A verified manifest, resolved against the cache.

    Holds no network client and does no I/O beyond reading the cache index,
    so `now_playing` is a pure, fast lookup. A new manifest means a new
    Schedule; nothing here mutates.
    """

    def __init__(self, manifest: Manifest | None, cache: MediaCache) -> None:
        self._manifest = manifest
        self._cache = cache

    @property
    def manifest(self) -> Manifest | None:
        return self._manifest

    @property
    def schedule_version(self) -> int:
        return self._manifest.schedule_version if self._manifest else 0

    def now_playing(self, moment: datetime) -> PlayItem:
        """
        What belongs on screen at `moment`.

        The fallback is returned — never None — for every reason the real
        answer is unavailable: no manifest yet, no slot covering now, or a
        slot whose media has not been cached. The caller shows whatever
        comes back and is spared having to reason about black screens.
        """
        if self._manifest is None:
            return FALLBACK

        slot = self._manifest.slot_at(moment)
        if slot is None:
            return FALLBACK

        return self._resolve(slot) or FALLBACK

    def next_change_after(self, moment: datetime) -> datetime | None:
        """
        When the screen should next reconsider what it is showing.

        The earlier of: the current slot ending, or the next slot starting.
        Lets the player sleep until something actually changes instead of
        polling — cheaper, and exact to the second.
        """
        if self._manifest is None:
            return None

        current = self._manifest.slot_at(moment)
        upcoming = self._manifest.next_slot_after(moment)

        candidates = []
        if current is not None:
            candidates.append(current.ends_at)
        if upcoming is not None:
            candidates.append(upcoming.starts_at)

        return min(candidates) if candidates else None

    def preload_checksums(self, moment: datetime) -> set[str]:
        """Every media checksum the plan still references, for cache eviction."""
        if self._manifest is None:
            return set()
        return {
            slot.media.checksum_sha256
            for slot in self._manifest.slots_to_preload(moment)
        }

    def is_exhausted(self, moment: datetime) -> bool:
        """True once the plan has nothing left to say about the future."""
        return self._manifest is None or self._manifest.is_exhausted(moment)

    # ── Internals ────────────────────────────────────────────────────────

    def _resolve(self, slot: Slot) -> PlayItem | None:
        """A slot becomes a PlayItem only if its media is cached and valid."""
        checksum = slot.media.checksum_sha256
        if not self._cache.has(checksum):
            # The file is not (yet) here. The fallback covers the gap, and
            # the download loop will have it before its next turn.
            return None

        is_video = slot.media.type == "video"

        return PlayItem(
            slot_id=slot.slot_id,
            ad_id=slot.ad_id,
            kind="video" if is_video else "image",
            local_src=self._cache.local_url_path(checksum),
            # An image is always silent; a video is silent unless the
            # manifest opted this stand into audio.
            muted=not is_video or slot.muted,
            duration_seconds=slot.duration_seconds,
            priority=slot.priority,
            label=slot.label,
        )
