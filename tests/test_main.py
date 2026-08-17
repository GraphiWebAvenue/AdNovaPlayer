"""
The thin wiring in main.py.

Almost everything here only makes sense with a real socket and systemd, so
it carries no unit-tested logic — except the credential surgery that auto
re-enrollment depends on, which is pure file work and pinned here.
"""

from __future__ import annotations

from adnova_player.main import _clear_stand_key


def test_clearing_the_stand_key_leaves_every_other_setting(tmp_path):
    env = tmp_path / "env"
    env.write_text(
        "ADNOVA_STAND_ID=3\n"
        "ADNOVA_STAND_KEY=" + ("a" * 64) + "\n"
        "ADNOVA_BASE_URL=https://dashboard.adnovatech.online\n"
        "ADNOVA_ENROLL_TOKEN=fleet-token\n",
        encoding="utf-8",
    )

    _clear_stand_key(env)

    text = env.read_text(encoding="utf-8")
    assert "ADNOVA_STAND_KEY" not in text          # the dead key is gone
    assert "ADNOVA_STAND_ID=3" in text             # identity kept
    assert "ADNOVA_BASE_URL=https://dashboard.adnovatech.online" in text
    assert "ADNOVA_ENROLL_TOKEN=fleet-token" in text   # so it can re-enroll


def test_clearing_a_missing_env_file_is_a_noop(tmp_path):
    # A device that never had an env file must not crash the re-enroll path.
    _clear_stand_key(tmp_path / "does-not-exist")   # must not raise
