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
import ssl
from typing import Any

import httpx

from . import event_log
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
        # Consecutive authentication rejections (401/403). A single one is
        # usually transient — a clock blip, a signing-window edge — but a run
        # of them means the stand key itself is no longer accepted, and the
        # agent watches this to decide it is time to re-enroll. Any accepted
        # response resets it to zero.
        self._auth_failures = 0

    @property
    def auth_failures(self) -> int:
        """How many authentication rejections in a row (0 once one succeeds)."""
        return self._auth_failures

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

    def send_logs(self, body: dict[str, Any]) -> dict[str, Any] | None:
        """
        Ship a batch of important device events. Best-effort like the rest —
        None on any failure, and the caller keeps the events for the next try.
        """
        response = self._post(self._config.logs_url, body)
        if response is None:
            return None
        return _json_or_none(response)

    def send_diagnostics(self, body: dict[str, Any]) -> dict[str, Any] | None:
        """
        Upload the redacted diagnostics bundle an operator asked for.

        This is the answer a shell would have given, sent over the channel the
        device already owns — the Pi sits behind a shop's NAT and nothing can
        dial in to it, so the only way to learn why a stand is misbehaving is
        for the stand to look at itself and say. Redacted by construction in
        diagnostics.redacted_bundle: keys never travel, only whether they are
        present.
        """
        response = self._post(self._config.diagnostics_url, body)
        if response is None:
            return None
        return _json_or_none(response)

    def get_screenshot_policy(self) -> dict[str, Any] | None:
        """
        What the operator wants right now: {"mode": "idle"|"once"|"live",
        "interval": seconds}. Tiny and cheap to poll, so the device grabs the
        screen only when someone is actually looking — never streaming frames
        off a shop's uplink unasked. None on any failure (treated as idle).
        """
        response = self._get(self._config.screenshot_policy_url)
        if response is None:
            return None
        return _json_or_none(response)

    def send_screenshot(self, jpeg: bytes) -> bool:
        """
        Upload a screenshot of the device's screen, base64 in a signed JSON
        body so it travels the same authenticated path as every other call.
        Best-effort: True on success, False on any failure — a screenshot is
        a diagnostic, never something the loop should stall on.
        """
        import base64

        body = {
            "stand_id": self._config.stand_id,
            "image_b64": base64.b64encode(jpeg).decode("ascii"),
        }
        response = self._post(self._config.screenshot_url, body)
        return response is not None

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
            # A TLS failure is not the same class of problem as a flaky link:
            # a certificate that stopped verifying on the way to Dashboard is
            # what an interception attempt looks like from in here, so it is
            # recorded as security rather than left inside a transport error.
            if _is_tls_failure(exc):
                event_log.record(
                    "api.tls.refused", "security",
                    f"{method} {path} refused the server certificate: {type(exc).__name__}.",
                )
            else:
                event_log.record("api.unreachable", "warn", f"{method} {path}: {type(exc).__name__}")
            return None

        if response.status_code >= 500:
            log.warning("%s %s → HTTP %s (server side)", method, path, response.status_code)
            event_log.record("api.server_error", "warn", f"{method} {path} → {response.status_code}")
            return None
        if response.status_code in (401, 403):
            # Either the clock has drifted past the signing window or the
            # key is wrong. Both are worth shouting about — a whole fleet
            # can fall off after a bad NTP day. A sustained run of these is
            # what tells the agent the key is truly gone and to re-enroll.
            self._auth_failures += 1
            log.error(
                "%s %s → %s: request rejected as unauthenticated (%d in a row).",
                method, path, response.status_code, self._auth_failures,
            )
            event_log.record(
                "api.auth.rejected", "security",
                f"{method} {path} → {response.status_code} ({self._auth_failures} in a row).",
            )
            return None
        if response.status_code >= 400:
            log.warning("%s %s → HTTP %s", method, path, response.status_code)
            event_log.record("api.rejected", "warn", f"{method} {path} → {response.status_code}")
            return None

        # An accepted response proves the key still works: clear the streak.
        self._auth_failures = 0
        return response


def _is_tls_failure(exc: Exception) -> bool:
    """
    Does this transport error mean the certificate was refused?

    httpx wraps the ssl module's error rather than exposing a type for it, so
    the cause chain is walked instead of matching on strings — a message match
    would quietly stop working the first time OpenSSL rewords something.
    """
    seen = 0
    current: BaseException | None = exc
    while current is not None and seen < 5:
        if isinstance(current, ssl.SSLError) or type(current).__name__ == "SSLError":
            return True
        current = current.__cause__ or current.__context__
        seen += 1
    return isinstance(exc, httpx.ConnectError) and "certificate" in str(exc).lower()


def _json_or_none(response: httpx.Response) -> dict[str, Any] | None:
    try:
        data = response.json()
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _version() -> str:
    from . import __version__

    return __version__
