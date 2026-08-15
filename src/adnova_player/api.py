"""
The device's one road to Dashboard.

Every call is signed the way SignedRequest.php expects — method, path,
body hash, timestamp, single-use nonce — so a captured request cannot be
edited or replayed. The stand key never travels; only the signature made
from it does.

Two failure principles, because this runs unattended behind a shop's
flaky wifi:

**A network error is a normal event, not an exception.** Every method
returns a result the caller can act on — a manifest or None, an
acknowledgement or None — rather than raising. The screen keeps playing
its cached plan while the network is down; nothing here is allowed to
turn a dropped connection into a stopped player.

**Nothing blocks forever.** Every request has a timeout. A hung server
must not freeze the poll loop, because that loop is also what notices the
screen has gone wrong.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .config import Config
from .signing import sign

log = logging.getLogger("adnova.api")

# Long enough for a large manifest over a slow link, short enough that a
# black-holed connection does not hold the loop open for a minute.
_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)


class DashboardApi:
    """A thin, signed HTTP client. One instance per process, reused."""

    def __init__(self, config: Config, client: httpx.Client | None = None) -> None:
        self._config = config
        # Injectable so tests can pass a transport; in production the
        # default client is fine, and reused so connections are pooled.
        self._client = client or httpx.Client(
            timeout=_TIMEOUT,
            follow_redirects=False,  # a redirect to elsewhere is not our server
            headers={"User-Agent": f"adnova-player/{_version()}"},
        )

    # ── Manifest ─────────────────────────────────────────────────────────

    def fetch_manifest(self) -> dict[str, Any] | None:
        """
        The raw signed manifest document, or None on any failure.

        Returned unparsed on purpose: the caller verifies the signature
        over these exact bytes, and re-serialising first would change them.
        """
        response = self._get(self._config.manifest_url)
        if response is None:
            return None

        try:
            document = response.json()
        except json.JSONDecodeError:
            log.warning("Manifest response was not JSON (HTTP %s).", response.status_code)
            return None

        if not isinstance(document, dict):
            log.warning("Manifest response was not a JSON object.")
            return None

        return document

    # ── Reports ──────────────────────────────────────────────────────────

    def send_heartbeat(self, body: dict[str, Any]) -> dict[str, Any] | None:
        """
        Report status, and read back the control channel.

        The response tells the device whether to refetch, and carries any
        on-demand request Dashboard has queued (the pull-based "ping").
        None means the heartbeat did not land — the caller simply tries
        again next tick.
        """
        response = self._post(self._config.heartbeat_url, body)
        if response is None:
            return None
        return _json_or_none(response)

    def send_playback(self, body: dict[str, Any]) -> dict[str, Any] | None:
        """
        Upload proof-of-play. Idempotent server-side by (slot, time), so a
        retried batch after a timeout never bills an advertiser twice —
        which is exactly why the caller is free to retry on None.
        """
        response = self._post(self._config.playback_url, body)
        if response is None:
            return None
        return _json_or_none(response)

    def close(self) -> None:
        self._client.close()

    # ── Internals ────────────────────────────────────────────────────────

    def _get(self, url: str) -> httpx.Response | None:
        return self._send("GET", url, b"")

    def _post(self, url: str, body: dict[str, Any]) -> httpx.Response | None:
        raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
        return self._send("POST", url, raw)

    def _send(self, method: str, url: str, body: bytes) -> httpx.Response | None:
        path = httpx.URL(url).path

        headers = {
            "Accept": "application/json",
            **sign(
                self._config.stand_id,
                self._config.stand_key,
                method,
                path,
                body,
            ),
        }
        if method == "POST":
            headers["Content-Type"] = "application/json"

        try:
            response = self._client.request(method, url, content=body or None, headers=headers)
        except httpx.HTTPError as exc:
            # The expected case on a shop network: report it and let the
            # loop carry on with the cached plan.
            log.warning("%s %s failed: %s", method, path, exc)
            return None

        if response.status_code >= 500:
            log.warning("%s %s → HTTP %s (server side)", method, path, response.status_code)
            return None
        if response.status_code == 401:
            # Either the clock has drifted past the signing window or the
            # key is wrong. Both are worth shouting about — a whole fleet
            # can fall off after a bad NTP day.
            log.error("%s %s → 401: request was rejected as unauthenticated.", method, path)
            return None
        if response.status_code >= 400:
            log.warning("%s %s → HTTP %s", method, path, response.status_code)
            return None

        return response


def _json_or_none(response: httpx.Response) -> dict[str, Any] | None:
    try:
        data = response.json()
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _version() -> str:
    from . import __version__

    return __version__
