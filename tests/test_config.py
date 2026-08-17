"""
Provisioning.

A device is one stand, fixed at install. These pin that it refuses to
start half-configured — a player with no identity has nothing safe to do
— and that the optional pieces have working defaults.
"""

from __future__ import annotations

import pytest

from adnova_player.config import ConfigError, load

VALID = {
    "ADNOVA_STAND_ID": "3",
    "ADNOVA_STAND_KEY": "a" * 64,
    "ADNOVA_BASE_URL": "https://dashboard.adnovatech.online",
}


def test_a_plaintext_base_url_is_rejected():
    # http to a real host would let a network attacker read the stand key's
    # use and feed a forged schedule — refuse to start rather than run insecure.
    with pytest.raises(ConfigError):
        load({**VALID, "ADNOVA_BASE_URL": "http://dashboard.adnovatech.online"})


def test_a_loopback_http_base_url_is_allowed():
    # No network to attack on the loopback — handy for local dev and tests.
    config = load({**VALID, "ADNOVA_BASE_URL": "http://127.0.0.1:8000"})
    assert config.base_url == "http://127.0.0.1:8000"


def test_a_minimal_env_yields_a_working_config():
    config = load(VALID)

    assert config.stand_id == 3
    assert config.stand_key == "a" * 64
    assert config.manifest_url.endswith("/api/v1/player/3/manifest")
    assert config.heartbeat_url.endswith("/api/v1/player/heartbeat")
    # Defaults are present without being set.
    assert config.heartbeat_seconds == 60
    assert config.local_host == "127.0.0.1"


def test_a_missing_stand_id_is_fatal():
    env = dict(VALID)
    del env["ADNOVA_STAND_ID"]

    with pytest.raises(ConfigError, match="ADNOVA_STAND_ID"):
        load(env)


def test_a_missing_key_is_fatal():
    env = dict(VALID)
    del env["ADNOVA_STAND_KEY"]

    with pytest.raises(ConfigError, match="ADNOVA_STAND_KEY"):
        load(env)


def test_a_wrong_length_key_is_refused():
    # A truncated key is the kind of copy-paste error that would otherwise
    # surface as a baffling 401 an hour later.
    with pytest.raises(ConfigError, match="64-character"):
        load({**VALID, "ADNOVA_STAND_KEY": "tooshort"})


def test_a_non_numeric_stand_id_is_refused():
    with pytest.raises(ConfigError, match="integer"):
        load({**VALID, "ADNOVA_STAND_ID": "three"})


def test_trusted_keys_parse_from_json():
    config = load({**VALID, "ADNOVA_TRUSTED_KEYS": '{"kid1": "cHVi"}'})

    assert config.trusted_keys == {"kid1": "cHVi"}


def test_malformed_trusted_keys_are_fatal():
    # Silently trusting no keys would turn "signatures required" into
    # "every manifest refused" — a fleet outage dressed as a network fault.
    with pytest.raises(ConfigError, match="TRUSTED_KEYS"):
        load({**VALID, "ADNOVA_TRUSTED_KEYS": "not json"})

    with pytest.raises(ConfigError, match="TRUSTED_KEYS"):
        load({**VALID, "ADNOVA_TRUSTED_KEYS": '["a", "b"]'})


def test_base_url_trailing_slash_is_trimmed():
    config = load({**VALID, "ADNOVA_BASE_URL": "https://x.example/"})

    assert "//api" not in config.manifest_url.replace("https://", "")


def test_a_garbage_interval_falls_back_to_the_default():
    # A bad poll value should not stop a device booting; it just uses the
    # default until the manifest overrides it anyway.
    config = load({**VALID, "ADNOVA_HEARTBEAT_SECONDS": "soon"})

    assert config.heartbeat_seconds == 60
