"""
Signing what the device sends back.

`trust.py` is the inbound half — proving Dashboard wrote the plan. This is
the outbound half, and it answers a different question: proving a
heartbeat or a playback log really came from this stand.

It matters for money. Playback logs are what advertisers are billed on. A
captured upload replayed a thousand times inflates an invoice, and a
captured heartbeat replayed forever makes a dead stand look alive on the
fleet screen — the two failures nobody would notice until a customer
did.

A bearer token alone cannot stop either, because a token proves only that
somebody knows the token, and every proxy and log aggregator the request
passes through has now seen it. The signature covers the method, path and
body, and a timestamp plus a single-use nonce make each request good
exactly once.

Symmetric HMAC here rather than Ed25519, deliberately: this key lives on
a device in a shop window, so it is scoped to one stand and grants
nothing beyond that stand's own reports. The asymmetric key is on the
side where a compromise would be fleet-wide.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

HEADER_ID = "X-AdNova-Id"
HEADER_TIMESTAMP = "X-AdNova-Timestamp"
HEADER_NONCE = "X-AdNova-Nonce"
HEADER_SIGNATURE = "X-AdNova-Signature"


def canonical(method: str, path: str, timestamp: str, nonce: str, body: bytes) -> bytes:
    """
    The exact string both ends hash.

    Must match SignedRequest::compute() in Dashboard. Newline separated
    with no escaping is safe because no part can contain a newline: the
    method is a verb, the path is URL-encoded, the timestamp is digits,
    the nonce is alphanumeric, and the body is a hex digest. Concatenating
    without a separator is where signing schemes usually go wrong.

    The body is hashed rather than included so a long playback batch is
    not buffered twice, and so the signature survives whatever the
    transfer encoding does to it.
    """
    return "\n".join([
        method.upper(),
        "/" + path.lstrip("/"),
        timestamp,
        nonce,
        hashlib.sha256(body).hexdigest(),
    ]).encode("utf-8")


def sign(
    stand_id: int | str,
    secret: str,
    method: str,
    path: str,
    body: bytes = b"",
) -> dict[str, str]:
    """
    Headers for one request. Call it per request, never cache the result.

    The nonce is what makes a captured request unusable a second time, so
    reusing a set of headers — a retry that reuses them, say — is the one
    way to break this. A retry must re-sign.
    """
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)

    signature = hmac.new(
        secret.encode("utf-8"),
        canonical(method, path, timestamp, nonce, body),
        hashlib.sha256,
    ).hexdigest()

    return {
        HEADER_ID: str(stand_id),
        HEADER_TIMESTAMP: timestamp,
        HEADER_NONCE: nonce,
        HEADER_SIGNATURE: signature,
    }
