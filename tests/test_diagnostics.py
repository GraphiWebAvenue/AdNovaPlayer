"""
Boot self-test and the diagnostics bundle.

The checks are injected their environment (which binaries exist, whether the
card is writable), so every degraded state is exercised here without needing
that hardware. The bundle's one hard promise — no secret ever leaves — is
pinned too.
"""

from __future__ import annotations

import json

from adnova_player.diagnostics import failures, redacted_bundle, run_self_test

ALL_PRESENT = lambda name: "/usr/bin/" + name   # noqa: E731 — terse test stub


def test_all_checks_pass_on_a_healthy_box(tmp_path):
    checks = run_self_test(
        stand_id=3, cache_dir=tmp_path, has_trusted_keys=True, which=ALL_PRESENT,
    )
    assert failures(checks) == []


def test_a_missing_player_is_a_critical_failure_and_sorts_first(tmp_path):
    checks = run_self_test(
        stand_id=3, cache_dir=tmp_path, has_trusted_keys=True,
        which=lambda name: None if name == "mpv" else "/usr/bin/" + name,
    )
    assert "player" in failures(checks)
    player = next(c for c in checks if c.name == "player")
    assert player.severity == "critical"
    assert checks[0].ok is False           # failures lead the list


def test_no_stand_id_fails_identity(tmp_path):
    checks = run_self_test(
        stand_id=None, cache_dir=tmp_path, has_trusted_keys=True, which=ALL_PRESENT,
    )
    assert "identity" in failures(checks)


def test_a_read_only_card_fails_storage(tmp_path):
    afile = tmp_path / "afile"
    afile.write_bytes(b"x")               # a file, not a writable dir
    checks = run_self_test(
        stand_id=3, cache_dir=afile, has_trusted_keys=True, which=ALL_PRESENT,
    )
    assert "storage" in failures(checks)


def test_missing_signing_keys_is_only_a_warning(tmp_path):
    checks = run_self_test(
        stand_id=3, cache_dir=tmp_path, has_trusted_keys=False, which=ALL_PRESENT,
    )
    signing = next(c for c in checks if c.name == "signing")
    assert signing.ok is False
    assert signing.severity == "warn"


def test_the_bundle_carries_the_picture_but_never_a_secret(tmp_path):
    checks = run_self_test(
        stand_id=3, cache_dir=tmp_path, has_trusted_keys=True, which=ALL_PRESENT,
    )
    bundle = redacted_bundle(
        stand_id=3, player_version="1.2.3", checks=checks,
        health={"temp_c": 41.0, "storage_writable": True},
    )
    blob = json.dumps(bundle)

    assert bundle["stand_id"] == 3
    assert bundle["player_version"] == "1.2.3"
    assert bundle["self_test_failures"] == []
    # The redaction promise: no key, no key-shaped string, no env secret name.
    assert "stand_key" not in blob
    assert "ADNOVA_STAND_KEY" not in blob
    assert "a" * 64 not in blob
