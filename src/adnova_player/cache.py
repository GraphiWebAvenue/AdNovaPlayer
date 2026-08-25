"""
The local media store.

The manifest names media by URL and by SHA-256 checksum. This downloads
what the next few hours need, verifies every byte against that checksum
before trusting it, and keeps the disk from filling. The playback layer
only ever reads files this module has already vouched for.

Three rules:

**Nothing plays until its checksum matches.** A file whose bytes do not
hash to what the manifest promised is discarded, not shown — it is either
a corrupted download or a substituted file, and on a screen in a shop
window the two are treated the same. The checksum is the manifest's, and
the manifest is signed, so this extends the signature's guarantee all the
way to the pixels.

**A file is downloaded once.** Media is content-addressed by its
checksum, so the same advertisement across many slots is one file on
disk, and a file already present and valid is never fetched again.

**The disk is bounded.** A Pi has a small card. Downloads stop before it
fills, and the least-recently-needed files are evicted first, so a long
campaign cannot crowd out the plan that is actually playing.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import event_log
from .config import is_secure_url
from .probe import probe_media

log = logging.getLogger("adnova.cache")

# A media file larger than this is refused unread. Signage is short clips
# and stills; anything past here is a misconfiguration or an attempt to
# fill the card, and buffering it to find out is the harm itself.
MAX_MEDIA_BYTES = 512 * 1024 * 1024  # 512 MB

# Read in chunks so a large video never sits in memory whole — the Pi has
# little to spare, and the hash is computed as the bytes arrive.
_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class MediaNeed:
    """One file the schedule will want, as named by the manifest."""

    url: str
    checksum_sha256: str
    size_bytes: int = 0


class MediaCache:
    """
    Content-addressed media on the local disk.

    Files are stored under their checksum, so the name is the proof: if
    the file called <checksum> exists and its bytes hash to <checksum>, it
    is the right file. That check is cheap enough to run before playback
    and is what lets a cached file be trusted after a reboot without
    re-downloading it.
    """

    def __init__(
        self,
        directory: Path,
        client: httpx.Client | None = None,
        probe: Callable[[Path], tuple[bool, str]] | None = None,
    ) -> None:
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0),
            follow_redirects=False,
        )
        # Decode-probe results (see probe.py). `_probed` is every checksum
        # we've already probed; `_undecodable` is the subset that failed and
        # must be treated as if it were missing. `probe` is injectable for
        # tests so no real ffprobe is needed.
        self._probe = probe or probe_media
        self._probed: set[str] = set()
        self._undecodable: set[str] = set()

    # ── Reading ──────────────────────────────────────────────────────────

    def path_for(self, checksum: str) -> Path:
        return self._dir / checksum

    def has(self, checksum: str) -> bool:
        """Present on disk and still matching its name. Cheap enough per slot."""
        path = self.path_for(checksum)
        return path.exists() and self._verify(path, checksum)

    def is_playable(self, checksum: str) -> bool:
        """
        Present, matching its checksum, AND not known-undecodable.

        The playback layer resolves a slot only if this is true; a file
        proven undecodable behaves exactly like one still downloading, so
        the fallback covers its slot instead of a black frame. A file not
        yet probed is trusted (the checksum already vouches for its bytes);
        only a definitive probe failure quarantines it.
        """
        return checksum not in self._undecodable and self.has(checksum)

    def local_url_path(self, checksum: str) -> str:
        """The path the kiosk browser fetches this from, on the local server."""
        return f"/media/{checksum}"

    # ── Filling ──────────────────────────────────────────────────────────

    def ensure(self, need: MediaNeed) -> bool:
        """
        Make sure one file is present and valid. Returns whether it is.

        Idempotent: a file already downloaded and matching is a no-op. A
        download that fails or fails verification leaves nothing behind, so
        a later retry starts clean rather than resuming a corrupt file.
        """
        if self.has(need.checksum_sha256):
            return True

        # Media is fetched over TLS or not at all. The URL comes from a signed
        # manifest, so this is defence in depth: a misconfigured or compromised
        # origin cannot make the device pull ad bytes over plaintext http.
        if not is_secure_url(need.url):
            log.error(
                "Refusing %s: media URL is not https (%s).",
                need.checksum_sha256[:12], urlparse(need.url).scheme or "?",
            )
            event_log.record(
                "media.plaintext_refused", "security",
                f"{need.checksum_sha256[:12]} was offered over "
                f"{urlparse(need.url).scheme or '?'}, not https.",
            )
            return False

        if need.size_bytes and need.size_bytes > MAX_MEDIA_BYTES:
            log.warning(
                "Refusing %s: declared %d bytes exceeds the %d limit.",
                need.checksum_sha256[:12],
                need.size_bytes,
                MAX_MEDIA_BYTES,
            )
            return False

        return self._download(need)

    def ensure_all(self, needs: list[MediaNeed]) -> dict[str, bool]:
        """
        Fetch a whole preload horizon, in order, stopping cleanly on a full
        disk. Returns each checksum's outcome so the caller can log what is
        missing before its slot arrives.
        """
        results: dict[str, bool] = {}
        for need in needs:
            results[need.checksum_sha256] = self.ensure(need)
        return results

    def probe_all(self, checksums: Iterable[str]) -> None:
        """
        Decode-probe every cached file we have not probed yet.

        Runs on the fetch loop (background), never the hot path. Each file
        is probed once; a failure adds it to the quarantine set so
        is_playable() starts returning False for it. Missing files are
        skipped — they'll be probed once they download.
        """
        for checksum in checksums:
            if checksum in self._probed:
                continue
            path = self.path_for(checksum)
            if not (path.exists() and self._verify(path, checksum)):
                continue
            self._probed.add(checksum)
            ok, detail = self._probe(path)
            if not ok:
                self._undecodable.add(checksum)
                log.error(
                    "Media %s failed the decode probe (%s); quarantined.",
                    checksum[:12], detail,
                )

    def undecodable_count(self) -> int:
        """How many cached files failed the decode probe (for the heartbeat)."""
        return len(self._undecodable)

    # ── Housekeeping ─────────────────────────────────────────────────────

    def evict_except(self, keep: set[str]) -> int:
        """
        Delete every cached file whose checksum is not in `keep`.

        `keep` is the set the current plan still references. Anything else
        is a finished campaign taking up space. Returns how many files went,
        for the log.
        """
        removed = 0
        for path in self._dir.iterdir():
            if path.is_file() and path.name not in keep:
                try:
                    path.unlink()
                    removed += 1
                    # Drop any probe verdict so a later re-download of the
                    # same checksum is probed afresh rather than trusted.
                    self._probed.discard(path.name)
                    self._undecodable.discard(path.name)
                except OSError as exc:
                    log.warning("Could not evict %s: %s", path.name, exc)
        if removed:
            log.info("Evicted %d media file(s) no longer in the plan.", removed)
        return removed

    def enforce_budget(self, max_bytes: int, keep: set[str]) -> int:
        """
        Keep the cache under a total-byte budget by evicting the
        least-recently-cached files the plan does NOT still need, oldest
        first. Returns how many went.

        Files in `keep` (the current plan's media + fallback + test) are
        never evicted here — they are needed soon, and being over budget is
        a smaller harm than a black frame. If dropping every evictable file
        still leaves us over, we log and stop rather than delete content we
        are about to play.
        """
        if max_bytes <= 0:
            return 0
        used = self.used_bytes()
        if used <= max_bytes:
            return 0

        evictable = [
            p for p in self._dir.iterdir()
            if p.is_file() and p.name not in keep
        ]
        # Oldest first. mtime is set at download; readers don't touch it,
        # so this is "least-recently-cached", a fine LRU proxy on a Pi
        # (atime is usually disabled by noatime).
        evictable.sort(key=lambda p: p.stat().st_mtime)

        removed = 0
        for p in evictable:
            if used <= max_bytes:
                break
            try:
                size = p.stat().st_size
                p.unlink()
                self._probed.discard(p.name)
                self._undecodable.discard(p.name)
                used -= size
                removed += 1
            except OSError as exc:
                log.warning("Could not evict %s for budget: %s", p.name, exc)

        if used > max_bytes:
            log.warning(
                "Cache is still %d bytes over budget after evicting every "
                "file the plan no longer needs.", used - max_bytes,
            )
        if removed:
            log.info("Budget eviction removed %d file(s).", removed)
        return removed

    def used_bytes(self) -> int:
        return sum(p.stat().st_size for p in self._dir.iterdir() if p.is_file())

    def close(self) -> None:
        self._client.close()

    # ── Internals ────────────────────────────────────────────────────────

    def _download(self, need: MediaNeed) -> bool:
        """
        Stream to a temp file, hashing as we go, then atomically rename.

        The temp file is on the same filesystem as the cache, so the rename
        is atomic — a reader never sees a half-written file, and a power cut
        mid-download leaves only a stray temp file, never a corrupt cache
        entry wearing a valid name.
        """
        digest = hashlib.sha256()
        total = 0
        tmp_fd, tmp_name = tempfile.mkstemp(dir=self._dir, suffix=".part")
        tmp_path = Path(tmp_name)

        try:
            with (
                os.fdopen(tmp_fd, "wb") as out,
                self._client.stream("GET", need.url) as response,
            ):
                if response.status_code != 200:
                    log.warning(
                        "Download of %s → HTTP %s",
                        need.checksum_sha256[:12],
                        response.status_code,
                    )
                    return False

                for chunk in response.iter_bytes(_CHUNK):
                    total += len(chunk)
                    if total > MAX_MEDIA_BYTES:
                        log.warning(
                            "Download of %s aborted: exceeded %d bytes.",
                            need.checksum_sha256[:12],
                            MAX_MEDIA_BYTES,
                        )
                        return False
                    digest.update(chunk)
                    out.write(chunk)

            actual = digest.hexdigest()
            if actual != need.checksum_sha256:
                # The whole point. The bytes are not what the signed
                # manifest promised, so they are not what plays.
                log.error(
                    "Checksum mismatch for %s: got %s. Discarding.",
                    need.checksum_sha256[:12],
                    actual[:12],
                )
                # Bytes that do not match a signed manifest's checksum are, by
                # definition, bytes somebody substituted — a hijacked origin, a
                # cache poisoned in the middle, a corrupted CDN object. Never
                # merely a warning.
                event_log.record(
                    "media.checksum_failed", "security",
                    f"Expected {need.checksum_sha256[:12]}, got {actual[:12]} "
                    f"({total} bytes). Discarded.",
                )
                return False

            tmp_path.replace(self.path_for(need.checksum_sha256))
            log.info("Cached %s (%d bytes).", need.checksum_sha256[:12], total)
            return True

        except httpx.HTTPError as exc:
            log.warning("Download of %s failed: %s", need.checksum_sha256[:12], exc)
            return False
        finally:
            # Whatever happened, no temp file is left behind.
            if tmp_path.exists():
                with contextlib.suppress(OSError):
                    tmp_path.unlink()

    def _verify(self, path: Path, checksum: str) -> bool:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(_CHUNK), b""):
                    digest.update(chunk)
        except OSError:
            return False
        return digest.hexdigest() == checksum
