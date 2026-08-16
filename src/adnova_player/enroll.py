"""
Bringing a blank device into the fleet with no key typed by hand.

A freshly flashed Pi carries only a shared fleet enrollment token — enough
to say "I exist", nothing more. On first boot, when it has no stand key,
it:

  1. generates its own device id and a secret it never sends in the clear;
  2. introduces itself to Dashboard with the fleet token;
  3. waits, polling, until an admin adopts it onto a stand;
  4. collects that stand's key and the manifest signing key, writes them,
     and from then on runs as a normal device.

The security is not in this file — it is in Dashboard: the fleet token
only lets a device appear in a list, admin approval is the only thing that
releases a key, and the key comes back only to the device that can prove,
with its secret, that it is the one that enrolled. This side just follows
the steps and stores what it is given.

Everything here is best-effort and patient. A device with no network waits
and retries forever rather than failing — it has nothing else to do until
it is adopted.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import secrets
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger("adnova.enroll")

_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=20.0, pool=10.0)


@dataclass(frozen=True)
class Adoption:
    """What Dashboard hands back once a device is adopted."""

    stand_id: int
    stand_key: str
    key_id: str | None
    public_key: str | None


class Enroller:
    """
    Drives one device from blank to adopted.

    The device id and secret are persisted, so a reboot mid-enrollment
    resumes the same identity rather than starting over as a stranger.
    """

    def __init__(self, base_url: str, fleet_token: str, state_dir: Path) -> None:
        self._base = base_url.rstrip("/")
        self._token = fleet_token
        self._id_path = state_dir / "device_id"
        self._secret_path = state_dir / "device_secret"
        state_dir.mkdir(parents=True, exist_ok=True)

    # ── identity ─────────────────────────────────────────────────────────

    def _device_id(self) -> str:
        if self._id_path.exists():
            return self._id_path.read_text(encoding="utf-8").strip()
        value = secrets.token_hex(32)  # 64 hex chars
        self._id_path.write_text(value, encoding="utf-8")
        return value

    def _device_secret(self) -> str:
        if self._secret_path.exists():
            return self._secret_path.read_text(encoding="utf-8").strip()
        value = secrets.token_hex(32)
        self._secret_path.write_text(value, encoding="utf-8")
        self._secret_path.chmod(0o600)
        return value

    # ── the two calls ────────────────────────────────────────────────────

    def introduce(self, client: httpx.Client) -> str | None:
        """
        Say "I exist". Returns the status Dashboard reports, or None on a
        network error — in which case the caller simply tries again.
        """
        secret = self._device_secret()
        body = {
            "device_id": self._device_id(),
            "device_secret_hash": hashlib.sha256(secret.encode()).hexdigest(),
            "hostname": socket.gethostname(),
            "model": _model(),
            "mac": _mac(),
            "local_ip": _local_ip(),
        }
        try:
            response = client.post(
                f"{self._base}/api/v1/enroll",
                json=body,
                headers={"X-AdNova-Enroll": self._token},
            )
        except httpx.HTTPError as exc:
            log.warning("Enrollment call failed: %s", exc)
            return None

        if response.status_code == 403:
            log.info("Enrollment is closed on the server; waiting for it to open.")
            return "closed"
        if response.status_code >= 400:
            log.warning("Enrollment returned HTTP %s", response.status_code)
            return None

        return (response.json() or {}).get("status")

    def poll(self, client: httpx.Client) -> Adoption | str | None:
        """
        Ask "am I adopted yet?". Returns an Adoption once approved, a status
        string ("pending"/"rejected") while not, or None on a network error.
        """
        body = {"device_id": self._device_id(), "device_secret": self._device_secret()}
        try:
            response = client.post(f"{self._base}/api/v1/enroll/status", json=body)
        except httpx.HTTPError as exc:
            log.warning("Enrollment poll failed: %s", exc)
            return None

        if response.status_code >= 400:
            return "pending"

        data = response.json() or {}
        if data.get("status") != "approved":
            return str(data.get("status", "pending"))

        signing = data.get("signing_key") or {}
        return Adoption(
            stand_id=int(data["stand_id"]),
            stand_key=str(data["stand_key"]),
            key_id=signing.get("key_id"),
            public_key=signing.get("public_key"),
        )

    def confirm_claimed(self, client: httpx.Client) -> None:
        """Tell Dashboard the key has been written, so it stops handing it back."""
        body = {
            "device_id": self._device_id(),
            "device_secret": self._device_secret(),
            "claimed": True,
        }
        # Best-effort; the key is already written locally either way.
        with contextlib.suppress(httpx.HTTPError):
            client.post(f"{self._base}/api/v1/enroll/status", json=body)

    def client(self) -> httpx.Client:
        return httpx.Client(timeout=_TIMEOUT, follow_redirects=False)


def write_credentials(env_path: Path, adoption: Adoption) -> None:
    """
    Persist the adopted stand's identity into the environment file.

    Rewrites only the lines it owns, leaving anything else an operator put
    there intact. The file is the one the service reads on its next start.
    """
    lines: dict[str, str] = {}
    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in raw and not raw.lstrip().startswith("#"):
                key, _, value = raw.partition("=")
                lines[key.strip()] = value

    lines["ADNOVA_STAND_ID"] = str(adoption.stand_id)
    lines["ADNOVA_STAND_KEY"] = adoption.stand_key
    if adoption.key_id and adoption.public_key:
        import json

        lines["ADNOVA_TRUSTED_KEYS"] = json.dumps({adoption.key_id: adoption.public_key})

    body = "\n".join(f"{k}={v}" for k, v in lines.items()) + "\n"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = env_path.with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(env_path)


# ── device facts (all best-effort labels) ───────────────────────────────


def _model() -> str:
    path = Path("/proc/device-tree/model")
    try:
        if path.exists():
            return path.read_text(errors="ignore").strip("\x00").strip()
    except OSError:
        pass
    return "unknown"


def _mac() -> str:
    for iface in ("eth0", "wlan0"):
        p = Path(f"/sys/class/net/{iface}/address")
        try:
            if p.exists():
                return p.read_text().strip()
        except OSError:
            continue
    return ""


def _local_ip() -> str:
    try:
        out = subprocess.run(  # noqa: S603,S607 — fixed argv
            ["hostname", "-I"], capture_output=True, text=True, timeout=3
        )
        return out.stdout.split()[0] if out.stdout.split() else ""
    except (OSError, subprocess.SubprocessError):
        return ""
