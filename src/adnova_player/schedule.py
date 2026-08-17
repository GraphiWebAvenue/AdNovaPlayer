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
from .manifest import Manifest, Media, Slot


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
    # A fallback loop repeats until real content returns; a scheduled slot
    # plays once and moves on. The browser reads this to set video `loop`.
    loop: bool = False

    @property
    def is_fallback(self) -> bool:
        return self.priority == "fallback"

    @property
    def is_test(self) -> bool:
        return self.priority == "test"


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


def fallback_item(media: Media | None, cache: MediaCache) -> PlayItem:
    """
    Build the gap-filler from the operator's chosen media, if it is cached.

    Falls back to the built-in filler when there is no configured media or
    it has not downloaded yet — so the promise holds at every stage: an
    unconfigured stand, a stand mid-download, and a stand with its loop
    ready all show *something*, never black. The loop repeats until real
    content returns.
    """
    if media is None or not cache.is_playable(media.checksum_sha256):
        return FALLBACK

    is_video = media.type == "video"
    return PlayItem(
        slot_id=-1,
        ad_id=None,
        kind="video" if is_video else "image",
        local_src=cache.local_url_path(media.checksum_sha256),
        muted=True,  # a gap-filler is always silent; it is not an ad
        duration_seconds=0,  # loops, so duration is irrelevant
        priority="fallback",
        label="AdNova",
        loop=True,
    )


def test_item(media: Media | None, cache: MediaCache, muted: bool = True) -> PlayItem | None:
    """
    Build the operator's test broadcast, if its media is cached.

    Returns None when there is no test or its bytes have not arrived yet —
    the caller then falls through to the real schedule, so a test that is
    still downloading never blanks the screen. Loops like the fallback, but
    ranks above everything: the operator triggered it to see one ad now.
    """
    if media is None or not cache.is_playable(media.checksum_sha256):
        return None

    is_video = media.type == "video"
    return PlayItem(
        slot_id=-2,
        ad_id=None,  # a test never bills; ad_id stays out of the play log
        kind="video" if is_video else "image",
        local_src=cache.local_url_path(media.checksum_sha256),
        muted=not is_video or muted,
        duration_seconds=0,  # loops, so duration is irrelevant
        priority="test",
        label="TEST",
        loop=True,
    )


class Schedule:
    """
    A verified manifest, resolved against the cache.

    Holds no network client and does no I/O beyond reading the cache index,
    so `now_playing` is a pure, fast lookup. A new manifest means a new
    Schedule; nothing here mutates.
    """

    def __init__(
        self,
        manifest: Manifest | None,
        cache: MediaCache,
        emergency: PlayItem | None = None,
        fallback: PlayItem | None = None,
        test: PlayItem | None = None,
        offline_expired: bool = False,
    ) -> None:
        self._manifest = manifest
        self._cache = cache
        # True once the device has been out of contact with Dashboard longer
        # than the plan it holds is meant to cover (preload_hours). Past that
        # the cached slots are stale, so we stop honouring them and show the
        # operator's default loop until the network — and a fresh plan —
        # return.
        self._offline_expired = offline_expired
        # A takeover pushed from Dashboard through the heartbeat control
        # channel — a closure notice, a safety message — that preempts the
        # scheduled plan entirely while it is active.
        self._emergency = emergency
        # An operator's test broadcast: one ad on repeat, over the schedule,
        # while a live test is running. Ranks just below a safety takeover.
        self._test = test
        # What fills a gap: the operator's chosen loop when one is cached,
        # or the calm built-in filler. Never a black screen.
        self._fallback = fallback or FALLBACK

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
        # A live takeover wins over everything, including a scheduled
        # urgent slot. It is the one thing an operator reaches for when the
        # screen must say something *now*.
        if self._emergency is not None:
            return self._emergency

        # Out of contact too long: the cached plan has outlived the window it
        # was meant to cover, so its slots are no longer trustworthy. Show
        # the operator's default loop instead of stale ads until a fresh
        # plan arrives.
        if self._offline_expired:
            return self._fallback

        # A live test broadcast wins over the schedule (but not a safety
        # takeover). Dashboard only sends one while the operator's test is
        # running, so its presence here is the whole signal.
        if self._test is not None:
            return self._test

        if self._manifest is None:
            return self._fallback

        slot = self._manifest.slot_at(moment)
        if slot is None:
            return self._fallback

        # The slot is due but its bytes have not finished downloading. Rather
        # than cut straight to the house loop for the few seconds that takes,
        # hold on the most recent ad that IS cached — a real, recently-shown
        # advertisement reads far better in a shop window than the filler. It
        # is shown unbilled (ad_id dropped, fallback priority), so a download
        # hiccup never turns into a phantom impression the advertiser is
        # charged for. Only then, if nothing is cached at all, the house loop.
        return (
            self._resolve(slot)
            or self._recent_cached_filler(moment, skip=slot.slot_id)
            or self._fallback
        )

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
        keep = {
            slot.media.checksum_sha256
            for slot in self._manifest.slots_to_preload(moment)
        }
        # The gap-filler and any live test broadcast are referenced too, or
        # eviction would delete the very file just downloaded to hold them —
        # the browser would then request a purged /media/<checksum> and show
        # a black frame, exactly the thing the fallback exists to prevent.
        if self._manifest.fallback_media is not None:
            keep.add(self._manifest.fallback_media.checksum_sha256)
        if self._manifest.test_override_media is not None:
            keep.add(self._manifest.test_override_media.checksum_sha256)
        return keep

    def is_exhausted(self, moment: datetime) -> bool:
        """True once the plan has nothing left to say about the future."""
        return self._manifest is None or self._manifest.is_exhausted(moment)

    # ── Internals ────────────────────────────────────────────────────────

    def _recent_cached_filler(self, moment: datetime, skip: int) -> PlayItem | None:
        """
        The most recent already-started slot whose media is cached, as an
        unbilled hold for a transient download gap on the current slot.

        Returns None when nothing earlier is cached — the first slots of a
        fresh plan, before any media has landed — so the caller falls through
        to the house loop. The chosen ad is stripped of its ad_id and given
        fallback priority: it fills the screen but is never counted as a play.
        """
        if self._manifest is None:
            return None

        candidate: Slot | None = None
        for slot in self._manifest.slots:
            if slot.slot_id == skip or slot.starts_at > moment:
                continue
            if not self._cache.is_playable(slot.media.checksum_sha256):
                continue
            if candidate is None or slot.starts_at > candidate.starts_at:
                candidate = slot

        if candidate is None:
            return None

        is_video = candidate.media.type == "video"
        return PlayItem(
            slot_id=-1,
            ad_id=None,  # an unbilled hold; never counted as an impression
            kind="video" if is_video else "image",
            local_src=self._cache.local_url_path(candidate.media.checksum_sha256),
            muted=True,  # a gap-filler is silent, like the house loop
            duration_seconds=0,  # loops until the real slot's media arrives
            priority="fallback",
            label=candidate.label,
            loop=True,
        )

    def _resolve(self, slot: Slot) -> PlayItem | None:
        """A slot becomes a PlayItem only if its media is cached and valid."""
        checksum = slot.media.checksum_sha256
        if not self._cache.is_playable(checksum):
            # The file is not here yet, or it failed the decode probe. Either
            # way the fallback covers the gap; the download/probe loop sorts
            # it out before the slot's next turn.
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
