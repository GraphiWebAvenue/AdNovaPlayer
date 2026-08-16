"""
What this device is, and how it reaches Dashboard.

Everything here is read once at start-up from the environment — the same
values the installer wrote to /etc/adnova-player/env. Nothing in this
file is a secret's home: the stand key lives in that root-owned 0600
file, and this only carries it into memory. The manifest public keys the
device trusts are provisioned the same way.

Two rules shape the design:

**The identity is fixed at provisioning.** A device is one stand. It does
not discover which stand it is, negotiate it, or change it at runtime —
that would be a way to point a screen at someone else's schedule. The
stand id and key are handed to it once and never move.

**Poll intervals are advisory.** The values here are only the fallback
for a device that has never fetched a manifest. Once it has one, the
manifest's own `poll` block wins, so the whole fleet is retuned from
Dashboard rather than by editing a file on every Pi.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(RuntimeError):
    """A provisioning value is missing or unusable. The service must not start."""


@dataclass(frozen=True)
class Enrollment:
    """The minimal settings a blank device needs to enroll itself."""

    base_url: str
    token: str
    cache_dir: Path
    local_host: str
    local_port: int


def enrollment(env: dict[str, str] | None = None) -> Enrollment | None:
    """
    What a device with no stand key needs to introduce itself, or None if
    it was not imaged for enrollment (no fleet token).

    Read separately from the full config because it must work in exactly
    the state the full config refuses to load — a device with no stand key
    yet. That is the whole point of enrollment.
    """
    env = env if env is not None else dict(os.environ)
    token = env.get("ADNOVA_ENROLL_TOKEN", "").strip()
    if not token:
        return None

    return Enrollment(
        base_url=env.get("ADNOVA_BASE_URL", "https://dashboard.adnovatech.online").rstrip("/"),
        token=token,
        cache_dir=Path(env.get("ADNOVA_CACHE_DIR", "/var/lib/adnova-player")),
        local_host=env.get("ADNOVA_LOCAL_HOST", "127.0.0.1"),
        local_port=_int(env, "ADNOVA_LOCAL_PORT", 8080),
    )


@dataclass(frozen=True)
class Config:
    stand_id: int
    stand_key: str
    base_url: str
    cache_dir: Path

    # The manifest signing keys this device trusts, key_id -> base64 public
    # key. More than one so a key can be rotated without a flag day. A
    # device with none still runs — it just cannot verify a signature, and
    # so refuses every manifest once signing is required, which is the
    # safe direction.
    trusted_keys: dict[str, str] = field(default_factory=dict)

    # The local admin page's credentials. A technician on-site logs in to
    # see status and logs; the playback view needs no login because it is
    # bound to localhost and shows only what Dashboard already sent.
    admin_user: str = "admin"
    admin_password_hash: str = ""

    # Fallbacks only. The manifest overrides both.
    manifest_poll_seconds: int = 600
    heartbeat_seconds: int = 60

    # Where the local server listens for the kiosk browser. Loopback only,
    # always — the browser is the sole client and it runs on this machine.
    local_host: str = "127.0.0.1"
    local_port: int = 8080

    @property
    def manifest_url(self) -> str:
        return f"{self.base_url}/api/v1/player/{self.stand_id}/manifest"

    @property
    def heartbeat_url(self) -> str:
        return f"{self.base_url}/api/v1/player/heartbeat"

    @property
    def playback_url(self) -> str:
        return f"{self.base_url}/api/v1/player/playback"

    @property
    def screenshot_url(self) -> str:
        return f"{self.base_url}/api/v1/player/screenshot"

    @property
    def screenshot_policy_url(self) -> str:
        return f"{self.base_url}/api/v1/player/screenshot/policy"

    @property
    def manifest_path(self) -> Path:
        return self.cache_dir / "manifest.json"

    @property
    def media_dir(self) -> Path:
        return self.cache_dir / "media"

    @property
    def log_dir(self) -> Path:
        return self.cache_dir / "logs"


def load(env: dict[str, str] | None = None) -> Config:
    """
    Build the config from the environment, or fail loudly.

    A missing stand id or key is fatal: a player with no identity has
    nothing safe to do, and starting it half-configured would only hide
    the real problem behind later, stranger errors. Everything optional
    has a sensible default so a minimal env still yields a working device.
    """
    env = env if env is not None else dict(os.environ)

    stand_id = _require_int(env, "ADNOVA_STAND_ID")
    stand_key = _require(env, "ADNOVA_STAND_KEY")

    if len(stand_key) != 64:
        raise ConfigError(
            "ADNOVA_STAND_KEY must be the 64-character key from the install "
            f"sheet; got {len(stand_key)} characters."
        )

    cache_dir = Path(env.get("ADNOVA_CACHE_DIR", "/var/lib/adnova-player"))

    return Config(
        stand_id=stand_id,
        stand_key=stand_key,
        base_url=env.get("ADNOVA_BASE_URL", "https://dashboard.adnovatech.online").rstrip("/"),
        cache_dir=cache_dir,
        trusted_keys=_load_keys(env.get("ADNOVA_TRUSTED_KEYS", "")),
        admin_user=env.get("ADNOVA_ADMIN_USER", "admin"),
        admin_password_hash=env.get("ADNOVA_ADMIN_PASSWORD_HASH", ""),
        manifest_poll_seconds=_int(env, "ADNOVA_MANIFEST_POLL_SECONDS", 600),
        heartbeat_seconds=_int(env, "ADNOVA_HEARTBEAT_SECONDS", 60),
        local_host=env.get("ADNOVA_LOCAL_HOST", "127.0.0.1"),
        local_port=_int(env, "ADNOVA_LOCAL_PORT", 8080),
    )


def _load_keys(raw: str) -> dict[str, str]:
    """
    Trusted manifest keys, as a JSON object mapping key_id to public key.

    A malformed value is fatal rather than ignored: silently trusting no
    keys would turn "signatures required" into "every manifest refused",
    a fleet-wide outage that would look like a network fault. Better to
    refuse to start and say why.
    """
    raw = raw.strip()
    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"ADNOVA_TRUSTED_KEYS is not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
    ):
        raise ConfigError(
            "ADNOVA_TRUSTED_KEYS must be a JSON object of key_id -> base64 public key."
        )

    return parsed


def _require(env: dict[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ConfigError(f"{key} is required and was not set.")
    return value


def _require_int(env: dict[str, str], key: str) -> int:
    value = _require(env, key)
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer; got {value!r}.") from exc


def _int(env: dict[str, str], key: str, default: int) -> int:
    value = env.get(key, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default
