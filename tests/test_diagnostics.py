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


# ── The operational half of the bundle ───────────────────────────────────
#
# The self-test says whether the device came up sane. These say whether the
# screen is actually being driven right now — the fact that separates "the
# brain is fine, the panel is dead" from every other fault, and the one an
# operator previously had to SSH in to learn.


def test_session_processes_reports_each_part_separately():
    from adnova_player.diagnostics import session_processes

    # Only the launcher is up: the classic shape of a wedged display stack.
    def fake_pgrep(argv):
        return argv[-1] == r"adnova-kiosk\.sh"

    session = session_processes(runner=fake_pgrep)

    assert session["kiosk_launcher"] is True
    assert session["mpv_driver"] is False
    assert session["screenshot_uploader"] is False


def test_session_processes_survives_a_missing_pgrep():
    """
    This bundle is assembled exactly when a stand is already misbehaving, so
    an unanswerable question has to come back as "not running" rather than as
    an exception that takes the heartbeat loop with it.
    """
    from adnova_player.diagnostics import session_processes

    def explodes(argv):
        raise OSError("pgrep is not installed")

    session = session_processes(runner=explodes)

    assert set(session) == {
        "mpv_driver", "kiosk_launcher", "kiosk_helper", "screenshot_uploader",
    }
    assert all(up is False for up in session.values())


def test_kiosk_log_tail_is_bounded_and_forgiving(tmp_path):
    from adnova_player.diagnostics import kiosk_log_tail

    log = tmp_path / "kiosk.log"
    log.write_text("\n".join(f"line {i}" for i in range(200)), encoding="utf-8")

    tail = kiosk_log_tail(str(log), lines=10)
    assert len(tail) == 10
    assert tail[-1] == "line 199"

    # A stand whose launcher has never run has no such file, and that is not
    # an error — it is itself the diagnosis.
    assert kiosk_log_tail(str(tmp_path / "nope.log")) == []


def test_kiosk_log_tail_picks_the_session_that_tried_most_recently(tmp_path):
    """
    There is one log per uid.

    A single fixed name under /tmp let whichever account got there first lock
    every other account out of the file — which is precisely how a stand lost
    its screen: a service running as the wrong user created the launcher's
    lock, and the real launcher could never open it again. Per-uid names
    removed that trap and made this a glob, so the reader has to pick. The
    useful one is whichever session last tried to bring the display up.
    """
    import os

    from adnova_player.diagnostics import kiosk_log_tail

    stale = tmp_path / "adnova-kiosk-launch.1001.log"
    stale.write_text("the service account, failing\n", encoding="utf-8")
    live = tmp_path / "adnova-kiosk-launch.1000.log"
    live.write_text("the desktop session\n", encoding="utf-8")

    os.utime(stale, (1_000_000, 1_000_000))
    os.utime(live, (2_000_000, 2_000_000))

    assert kiosk_log_tail(str(tmp_path / "adnova-kiosk-launch.*.log")) == [
        "the desktop session",
    ]


def test_the_bundle_omits_the_new_fields_when_not_gathered(tmp_path):
    """An older caller's bundle must stay readable, not gain empty keys."""
    checks = run_self_test(
        stand_id=3, cache_dir=tmp_path, has_trusted_keys=True, which=ALL_PRESENT,
    )
    bundle = redacted_bundle(
        stand_id=3, player_version="1.2.3", checks=checks, health={},
    )

    assert "session" not in bundle
    assert "kiosk_log" not in bundle


def test_the_bundle_carries_the_display_picture_when_gathered(tmp_path):
    checks = run_self_test(
        stand_id=3, cache_dir=tmp_path, has_trusted_keys=True, which=ALL_PRESENT,
    )
    bundle = redacted_bundle(
        stand_id=3, player_version="1.2.3", checks=checks, health={},
        session={"mpv_driver": False, "kiosk_helper": False},
        kiosk_log=["no HDMI audio sink found — continuing on the default sink"],
        display={"playing": False},
    )

    assert bundle["session"]["mpv_driver"] is False
    assert "HDMI" in bundle["kiosk_log"][0]
    assert bundle["display"]["playing"] is False
