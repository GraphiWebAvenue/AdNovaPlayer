"""
The self-picture the device sends home.

Only the SD-armor probe carries branching worth pinning here — the sensors
are best-effort reads of hardware that is absent on the test box. The probe
must catch a read-only card, the tell-tale of a dying SD, without needing a
real one to remount.
"""

from __future__ import annotations

from adnova_player.health import _storage_writable, read


def test_storage_is_writable_on_a_normal_dir(tmp_path):
    assert _storage_writable(tmp_path) is True


def test_storage_is_not_writable_when_writes_are_refused(tmp_path):
    # A path that exists but is a file, not a directory: writing a probe file
    # "inside" it fails — standing in for a read-only mount without one.
    afile = tmp_path / "afile"
    afile.write_bytes(b"x")
    assert _storage_writable(afile) is False


def test_storage_writability_is_unknown_when_the_path_is_missing(tmp_path):
    assert _storage_writable(tmp_path / "nope") is None


def test_read_flags_a_read_only_card_in_the_warnings(tmp_path):
    afile = tmp_path / "afile"
    afile.write_bytes(b"x")

    health = read(afile)

    assert health.storage_writable is False
    assert any("read-only" in w for w in health.warnings)
