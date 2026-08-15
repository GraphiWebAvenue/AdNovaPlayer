"""
Proving Dashboard wrote this plan.

The threat this answers is the one that would embarrass us in public: an
attacker putting *their* advertisement on a customer's screen. TLS does
not answer it on its own. The connection is terminated at Cloudflare and
again at nginx, and a device on a hostile network — a shop's own Wi-Fi,
a hijacked DNS server, a captive portal with a trusted certificate — can
be pointed at a proxy that answers instead of us.

So the manifest carries an Ed25519 signature over its own contents, and
the device holds only the public half. A Pi lifted out of a shop window
yields nothing that can sign a manifest for any stand, including itself.

The rule on the device is absolute: **a manifest whose signature does not
verify is discarded, and the previous plan keeps playing.** An attacker
who owns the whole network can stop a screen from updating. They cannot
choose what it shows.

Ed25519 verification is implemented here in pure Python. A Pi has no
guaranteed libsodium and adding a compiled dependency to a fleet that
updates over the air is a fleet-wide risk; verification runs once per
manifest fetch — roughly every ten minutes — so a few milliseconds of
pure Python costs nothing that matters.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("adnova.trust")

ALGORITHM = "ed25519"


@dataclass(frozen=True)
class Verdict:
    """Why a manifest was or was not trusted, in words an operator can use."""

    trusted: bool
    reason: str
    key_id: str | None = None

    def __bool__(self) -> bool:
        return self.trusted


def canonical(manifest: dict[str, Any]) -> bytes:
    """
    The exact bytes both ends sign.

    Must match ManifestSigner::canonical() in Dashboard byte for byte:
    recursive key sort, no whitespace, no ASCII escaping, no escaped
    slashes. Lists keep their order — slots are chronological and sorting
    them would change what the manifest means.

    If this ever drifts from the PHP side, every device stops accepting
    every manifest, so the shared test vector in the test suite is the
    thing that keeps it honest.
    """
    body = {key: value for key, value in manifest.items() if key != "signature"}

    return json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def verify(
    manifest: dict[str, Any],
    public_keys: dict[str, str],
    *,
    stand_id: int | None = None,
    required: bool = True,
) -> Verdict:
    """
    Check a manifest against the public keys this device was provisioned
    with, and against the stand it was issued for.

    `public_keys` maps key_id to a base64 public key. More than one is
    allowed so a key can be rotated without a flag day: Dashboard starts
    signing with the new key, devices already hold both, and the old one
    is removed a release later.

    `stand_id`, when given, is this device's own stand id, and the
    manifest must be one signed *for* it. The signature alone is not
    enough: Dashboard signs every stand's manifest with the same fleet
    key, so a genuine, correctly-signed manifest full of an attacker's
    own booked ads — fetched with the attacker's own stand key — would
    otherwise verify on any other device on the network and play her
    advertisement on a stranger's screen. The stand id is inside the
    signed bytes, so she cannot change it without breaking the signature;
    binding to it here closes the gap. Left None only where the caller
    genuinely has no identity yet (tests, first-boot provisioning).

    `required=False` is the migration window and nothing more. It lets a
    fleet that predates signing keep working while the keys are rolled
    out, and it is logged every single time so it cannot quietly become
    permanent.
    """
    signature = manifest.get("signature")

    if not isinstance(signature, dict):
        return _unsigned("This manifest carries no signature.", required)

    algorithm = signature.get("algorithm")

    if algorithm == "none":
        reason = signature.get("reason") or "The server is not signing manifests."
        return _unsigned(f"Not signed: {reason}", required)

    if algorithm != ALGORITHM:
        # An unknown algorithm is refused outright even in the migration
        # window: it is the shape a downgrade attack takes.
        return Verdict(False, f"Unsupported signature algorithm {algorithm!r}.")

    key_id = str(signature.get("key_id") or "")
    encoded = public_keys.get(key_id)

    if encoded is None:
        return Verdict(
            False,
            f"Signed with key {key_id or '(none)'}, which this device does not trust.",
            key_id,
        )

    try:
        public_key = base64.b64decode(encoded, validate=True)
        value = base64.b64decode(str(signature.get("value") or ""), validate=True)
    except (ValueError, TypeError):
        return Verdict(False, "The signature or key is not valid base64.", key_id)

    if len(public_key) != 32 or len(value) != 64:
        return Verdict(False, "The signature or key is the wrong size.", key_id)

    if not _ed25519_verify(value, canonical(manifest), public_key):
        # The interesting case. Either somebody edited the manifest in
        # transit, or they wrote a whole new one — and either way the
        # caller must keep playing what it already had.
        return Verdict(False, "The signature does not match this manifest.", key_id)

    # Signed, and signed for us. Checked after the signature so the value
    # being compared is one an attacker could not have altered.
    if stand_id is not None:
        signed_for = manifest.get("stand_id")
        try:
            same = int(signed_for) == int(stand_id)
        except (TypeError, ValueError):
            same = False
        if not same:
            return Verdict(
                False,
                f"This manifest was signed for stand {signed_for!r}, not this device.",
                key_id,
            )

    return Verdict(True, "Signature verified.", key_id)


def _unsigned(reason: str, required: bool) -> Verdict:
    if required:
        return Verdict(False, reason)

    log.warning("%s Accepting it because signatures are not yet required.", reason)
    return Verdict(True, reason)


# ─── Ed25519 ────────────────────────────────────────────────────────────────
#
# RFC 8032 verification over Curve25519's twisted Edwards form. Only the
# verify half is here — the device has no reason to be able to sign, and
# leaving the signing code out means a compromised device cannot be turned
# into one that mints manifests.

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _recover_x(y: int, sign: int) -> int | None:
    """The x coordinate for a compressed point, or None if there isn't one."""
    if y >= _P:
        return None

    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0

    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _I % _P
    if (x * x - x2) % _P != 0:
        return None

    if x % 2 != sign:
        x = _P - x

    return x


def _point_add(
    p: tuple[int, int, int, int], q: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    """Extended homogeneous coordinates — no inversion in the inner loop."""
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P

    e, f, g, h = b - a, d - c, d + c, b + a

    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _point_mul(scalar: int, point: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    result = (0, 1, 1, 0)
    while scalar > 0:
        if scalar & 1:
            result = _point_add(result, point)
        point = _point_add(point, point)
        scalar >>= 1
    return result


def _point_equal(p: tuple[int, int, int, int], q: tuple[int, int, int, int]) -> bool:
    if (p[0] * q[2] - q[0] * p[2]) % _P != 0:
        return False
    return (p[1] * q[2] - q[1] * p[2]) % _P == 0


_G_Y = 4 * pow(5, _P - 2, _P) % _P
_G_X = _recover_x(_G_Y, 0) or 0
_G = (_G_X, _G_Y, 1, _G_X * _G_Y % _P)


def _decompress(data: bytes) -> tuple[int, int, int, int] | None:
    if len(data) != 32:
        return None

    value = int.from_bytes(data, "little")
    sign = value >> 255
    y = value & ((1 << 255) - 1)

    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


def _ed25519_verify(signature: bytes, message: bytes, public_key: bytes) -> bool:
    """RFC 8032 §5.1.7. Returns False on any malformed input rather than raising."""
    try:
        point = _decompress(public_key)
        if point is None:
            return False

        r = _decompress(signature[:32])
        if r is None:
            return False

        s = int.from_bytes(signature[32:], "little")
        if s >= _L:
            # A non-canonical scalar; accepting it would make signatures
            # malleable, which is how signature-reuse tricks start.
            return False

        digest = hashlib.sha512(signature[:32] + public_key + message).digest()
        h = int.from_bytes(digest, "little") % _L

        return _point_equal(_point_mul(s, _G), _point_add(r, _point_mul(h, point)))
    except (ValueError, TypeError, IndexError):
        return False
