"""
The media store.

The property that matters: nothing plays until its bytes hash to what the
signed manifest promised. These tests attack that from every side a
corrupt download or a substituted file could — wrong bytes, a truncated
stream, a stale cache entry — and confirm none of them reach the disk
under a valid name.
"""

from __future__ import annotations

import hashlib

import httpx

from adnova_player.cache import MediaCache, MediaNeed


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def cache_with(tmp_path, handler) -> MediaCache:
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    return MediaCache(tmp_path / "media", client=client)


def test_a_plaintext_media_url_is_refused(tmp_path):
    # Defence in depth: even from a signed manifest, media is TLS or nothing.
    # The refusal happens before any request, so no network is touched.
    def explode(req):
        raise AssertionError("should never fetch a plaintext URL")

    cache = cache_with(tmp_path, explode)
    checksum = sha(b"whatever")
    assert cache.ensure(MediaNeed(url="http://x/media/1", checksum_sha256=checksum)) is False
    assert not cache.path_for(checksum).exists()


def test_a_valid_file_downloads_and_verifies(tmp_path):
    body = b"pretend this is an mp4"
    checksum = sha(body)

    cache = cache_with(tmp_path, lambda req: httpx.Response(200, content=body))
    need = MediaNeed(url="https://x/media/1", checksum_sha256=checksum, size_bytes=len(body))

    assert cache.ensure(need) is True
    assert cache.has(checksum)
    assert cache.path_for(checksum).read_bytes() == body


def test_a_checksum_mismatch_is_discarded(tmp_path):
    body = b"the wrong bytes"
    # The manifest promised a different file's checksum.
    promised = sha(b"the right bytes")

    cache = cache_with(tmp_path, lambda req: httpx.Response(200, content=body))
    need = MediaNeed(url="https://x/media/1", checksum_sha256=promised)

    assert cache.ensure(need) is False
    assert not cache.has(promised)
    # Nothing at all was left on disk — not even under the wrong name.
    assert list((tmp_path / "media").iterdir()) == []


def test_an_already_cached_file_is_not_refetched(tmp_path):
    body = b"cached already"
    checksum = sha(body)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, content=body)

    cache = cache_with(tmp_path, handler)
    need = MediaNeed(url="https://x/media/1", checksum_sha256=checksum)

    assert cache.ensure(need) is True
    assert cache.ensure(need) is True  # second call
    assert calls["n"] == 1  # only downloaded once


def test_a_stale_cache_entry_is_not_trusted(tmp_path):
    # A file on disk whose bytes no longer match its name — bit-rot, or a
    # file someone dropped in — must not be served as valid.
    checksum = sha(b"genuine")
    (tmp_path / "media").mkdir(parents=True)
    (tmp_path / "media" / checksum).write_bytes(b"tampered")

    cache = cache_with(tmp_path, lambda req: httpx.Response(404))

    assert cache.has(checksum) is False


def test_a_server_error_leaves_nothing_behind(tmp_path):
    checksum = sha(b"whatever")
    cache = cache_with(tmp_path, lambda req: httpx.Response(503))
    need = MediaNeed(url="https://x/media/1", checksum_sha256=checksum)

    assert cache.ensure(need) is False
    assert list((tmp_path / "media").iterdir()) == []


def test_an_oversized_declared_file_is_refused_unread(tmp_path):
    from adnova_player.cache import MAX_MEDIA_BYTES

    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, content=b"x")

    cache = cache_with(tmp_path, handler)
    need = MediaNeed(
        url="https://x/media/1",
        checksum_sha256=sha(b"x"),
        size_bytes=MAX_MEDIA_BYTES + 1,
    )

    assert cache.ensure(need) is False
    assert calls["n"] == 0  # never even reached out


def test_eviction_keeps_the_current_plan_and_drops_the_rest(tmp_path):
    keep_body, drop_body = b"still playing", b"finished campaign"
    keep, drop = sha(keep_body), sha(drop_body)

    cache = cache_with(tmp_path, lambda req: httpx.Response(200))
    (tmp_path / "media" / keep).write_bytes(keep_body)
    (tmp_path / "media" / drop).write_bytes(drop_body)

    removed = cache.evict_except({keep})

    assert removed == 1
    assert cache.path_for(keep).exists()
    assert not cache.path_for(drop).exists()


def test_ensure_all_reports_each_outcome(tmp_path):
    good = b"good"
    good_sum = sha(good)

    def handler(req):
        return httpx.Response(200, content=good)

    cache = cache_with(tmp_path, handler)
    results = cache.ensure_all([
        MediaNeed(url="https://x/1", checksum_sha256=good_sum),
        MediaNeed(url="https://x/2", checksum_sha256=sha(b"never served")),
    ])

    assert results[good_sum] is True
    assert results[sha(b"never served")] is False
