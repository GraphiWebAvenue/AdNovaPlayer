"""Decode-probe: bad media is quarantined; a missing tool never blocks."""

import hashlib
import subprocess
from pathlib import Path

from adnova_player.cache import MediaCache
from adnova_player.probe import probe_media


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["ffprobe"], returncode=returncode, stdout=stdout, stderr=stderr,
    )


def test_probe_accepts_a_file_with_streams():
    ok, _ = probe_media(
        Path("x"),
        runner=lambda p: _proc(0, '{"streams": [{"codec_type": "video"}]}'),
    )
    assert ok


def test_probe_rejects_a_nonzero_exit():
    ok, detail = probe_media(
        Path("x"), runner=lambda p: _proc(1, "", "moov atom not found"),
    )
    assert not ok
    assert "moov" in detail


def test_probe_rejects_a_file_with_no_streams():
    ok, _ = probe_media(Path("x"), runner=lambda p: _proc(0, '{"streams": []}'))
    assert not ok


def test_probe_fails_open_when_the_tool_errors():
    def boom(_path):
        raise OSError("ffprobe not installed")

    ok, _ = probe_media(Path("x"), runner=boom)
    assert ok  # never blank the fleet because a dev tool is missing


def _cache_with_file(tmp_path, content=b"hello", probe=None):
    cache = MediaCache(tmp_path / "media", probe=probe)
    checksum = hashlib.sha256(content).hexdigest()
    cache.path_for(checksum).write_bytes(content)
    return cache, checksum


def test_an_undecodable_cached_file_is_quarantined(tmp_path):
    cache, checksum = _cache_with_file(tmp_path, probe=lambda p: (False, "bad codec"))

    assert cache.has(checksum)          # the bytes are authentic
    assert cache.is_playable(checksum)  # ...and trusted until probed
    cache.probe_all([checksum])
    assert not cache.is_playable(checksum)   # quarantined after the probe
    assert cache.undecodable_count() == 1


def test_a_decodable_cached_file_stays_playable(tmp_path):
    cache, checksum = _cache_with_file(tmp_path, probe=lambda p: (True, ""))
    cache.probe_all([checksum])
    assert cache.is_playable(checksum)
    assert cache.undecodable_count() == 0


def test_eviction_forgets_the_probe_verdict(tmp_path):
    cache, checksum = _cache_with_file(tmp_path, probe=lambda p: (False, "bad"))
    cache.probe_all([checksum])
    assert cache.undecodable_count() == 1
    cache.evict_except(set())            # remove everything
    assert cache.undecodable_count() == 0   # a re-download would be re-probed
