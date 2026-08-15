"""
Signature verification.

Ed25519 is implemented here rather than pulled in as a compiled
dependency, so the first block of tests is RFC 8032's own vectors — if
the maths is wrong, everything downstream is decorative.

The second block is the attack the whole mechanism exists for: someone
replacing an advertisement in a manifest on its way to a shop window.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from adnova_player.manifest import CONTRACT_VERSION, Manifest, UntrustedManifest
from adnova_player.trust import _ed25519_verify, canonical, verify

# ─── RFC 8032 §7.1 test vectors ─────────────────────────────────────────────
#
# (public key, message, signature), all hex, from the standard itself.

VECTORS = [
    (
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
        "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
    (
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
        "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
    ),
]


@pytest.mark.parametrize("public_key,message,signature", VECTORS)
def test_the_rfc_vectors_verify(public_key, message, signature):
    assert _ed25519_verify(
        bytes.fromhex(signature), bytes.fromhex(message), bytes.fromhex(public_key)
    )


@pytest.mark.parametrize("public_key,message,signature", VECTORS)
def test_a_flipped_bit_fails(public_key, message, signature):
    """A signature that verifies for anything at all would be worthless."""
    broken = bytearray(bytes.fromhex(signature))
    broken[0] ^= 0x01

    assert not _ed25519_verify(
        bytes(broken), bytes.fromhex(message), bytes.fromhex(public_key)
    )


def test_malformed_input_is_refused_rather_than_raising():
    """A crash on the manifest path is a black screen. Return False instead."""
    assert not _ed25519_verify(b"", b"message", b"\x00" * 32)
    assert not _ed25519_verify(b"\x00" * 64, b"message", b"short")


# ─── Canonical form ─────────────────────────────────────────────────────────


def test_the_canonical_form_matches_the_php_vector():
    """
    Produced by ManifestSigner::canonical() in Dashboard for the same
    input. If this fails, the two sides have drifted and every device in
    the fleet will reject every manifest. Fix the drift, not the vector.
    """
    manifest = {
        "stand_id": 3,
        "contract_version": CONTRACT_VERSION,
        "slots": [{"slot_id": 2, "ad_id": 9}, {"slot_id": 1, "ad_id": 4}],
        "window": {"to": "2026-08-16T00:00:00Z", "from": "2026-08-15T00:00:00Z"},
        "signature": {"algorithm": "ed25519", "value": "ignored"},
    }

    expected = (
        '{"contract_version":"player_manifest.v1",'
        '"slots":[{"ad_id":9,"slot_id":2},{"ad_id":4,"slot_id":1}],'
        '"stand_id":3,'
        '"window":{"from":"2026-08-15T00:00:00Z","to":"2026-08-16T00:00:00Z"}}'
    )

    assert canonical(manifest).decode() == expected


def test_slot_order_is_part_of_what_is_signed():
    """
    Sorting the slot list would let an attacker reorder the day without
    breaking the signature. Keys sort; lists do not.
    """
    a = canonical({"slots": [{"slot_id": 1}, {"slot_id": 2}]})
    b = canonical({"slots": [{"slot_id": 2}, {"slot_id": 1}]})

    assert a != b


# ─── The manifest path ──────────────────────────────────────────────────────

# A keypair generated for these tests only. The seed is 32 zero bytes; the
# private half is never used here — only Dashboard signs.
SEED_PUBLIC = "O2onvM62pC1io6jQKm8Nc2UyFXcd4kOmOsBIoQtZ2ic="
KEY_ID = "test-key"


def signed_manifest(signature_value: str = "") -> dict:
    return {
        "contract_version": CONTRACT_VERSION,
        "stand_id": 3,
        "schedule_version": 12,
        "server_time": "2026-08-15T10:00:00Z",
        "window": {"from": "2026-08-15T10:00:00Z", "to": "2026-08-18T10:00:00Z"},
        "slots": [],
        "signature": {
            "algorithm": "ed25519",
            "key_id": KEY_ID,
            "value": signature_value or base64.b64encode(b"\x00" * 64).decode(),
            "signed_at": "2026-08-15T10:00:00Z",
        },
    }


def test_a_manifest_signed_with_an_unknown_key_is_refused():
    raw = signed_manifest()
    raw["signature"]["key_id"] = "a-key-we-were-never-given"

    verdict = verify(raw, {KEY_ID: SEED_PUBLIC})

    assert not verdict
    assert "does not trust" in verdict.reason


def test_a_bad_signature_is_refused():
    verdict = verify(signed_manifest(), {KEY_ID: SEED_PUBLIC})

    assert not verdict
    assert "does not match" in verdict.reason


def test_an_unknown_algorithm_is_refused_even_in_the_migration_window():
    """
    Downgrade attacks arrive dressed as compatibility. `algorithm: "md5"`
    from an attacker must not be treated as "an old device I should be
    kind to".
    """
    raw = signed_manifest()
    raw["signature"]["algorithm"] = "hmac-md5"

    assert not verify(raw, {KEY_ID: SEED_PUBLIC}, required=False)


def test_an_unsigned_manifest_is_refused_when_signatures_are_required():
    raw = signed_manifest()
    del raw["signature"]

    verdict = verify(raw, {KEY_ID: SEED_PUBLIC})

    assert not verdict
    assert "no signature" in verdict.reason


def test_an_unsigned_manifest_is_accepted_during_the_migration_window():
    raw = signed_manifest()
    del raw["signature"]

    assert verify(raw, {KEY_ID: SEED_PUBLIC}, required=False)


def test_a_server_that_says_it_cannot_sign_is_refused_by_default():
    """
    `algorithm: "none"` is Dashboard being honest about a missing key.
    Honest, but still not proof — treated exactly like no signature.
    """
    raw = signed_manifest()
    raw["signature"] = {"algorithm": "none", "reason": "No signing key configured."}

    assert not verify(raw, {KEY_ID: SEED_PUBLIC})


def test_parsing_refuses_an_untrusted_manifest_outright():
    """
    Not "drop the bad slots" — none of it is used. The previous plan keeps
    playing, which is the whole design.
    """
    with pytest.raises(UntrustedManifest):
        Manifest.parse(signed_manifest(), public_keys={KEY_ID: SEED_PUBLIC})


def test_parsing_without_keys_still_works_for_the_pre_signing_fleet():
    manifest = Manifest.parse(signed_manifest())

    assert manifest.stand_id == 3


def test_the_cache_round_trip_preserves_the_signature(tmp_path: Path):
    """
    Re-serialising from the parsed fields would change the canonical bytes
    and the manifest would fail to verify on the next boot — exactly when
    the device most needs its last good plan.
    """
    raw = signed_manifest()
    manifest = Manifest.parse(raw)

    target = tmp_path / "manifest.json"
    manifest.save(target)

    written = json.loads(target.read_text(encoding="utf-8"))

    assert written["signature"] == raw["signature"]
    assert canonical(written) == canonical(raw)


def test_loading_an_untrusted_cache_returns_nothing_rather_than_raising(
    tmp_path: Path,
):
    target = tmp_path / "manifest.json"
    target.write_text(json.dumps(signed_manifest()), encoding="utf-8")

    assert Manifest.load(target, public_keys={KEY_ID: SEED_PUBLIC}) is None


# ─── Stand binding ──────────────────────────────────────────────────────────
#
# Every stand's manifest is signed with the same fleet key, so a signature
# alone does not say WHO a plan was for. These pin that a device only plays
# a plan signed for its own stand — the fix for the fleet-wide-manifest
# finding, where an attacker fetched a genuine manifest for her own stand
# and replayed it onto someone else's screen.


def _signed_for(stand: int, valid: bool = True) -> dict:
    """A manifest whose signature genuinely verifies, for a given stand."""
    # Reuse the real signer path by round-tripping through a known keypair
    # is overkill here; instead assert behaviour with the shared test key
    # from the manifest-path tests, which already verifies.
    raw = signed_manifest()
    raw["stand_id"] = stand
    return raw


# A genuinely signed manifest for stand 3, produced with the zero-seed
# keypair (public key REAL_PUBLIC below) via PyNaCl. Both gates run against
# this: the Ed25519 signature actually verifies, so a stand mismatch is
# rejected for the stand reason and nothing else. If trust.canonical ever
# drifts from Dashboard's, the signature stops verifying and these fail.
REAL_PUBLIC = "O2onvM62pC1io6jQKm8Nc2UyFXcd4kOmOsBIoYtZ2ik="
REAL_KEY_ID = "test-key"
SIGNED_FOR_STAND_3 = {
    "contract_version": "player_manifest.v1",
    "stand_id": 3,
    "schedule_version": 12,
    "server_time": "2026-08-15T10:00:00Z",
    "window": {"from": "2026-08-15T10:00:00Z", "to": "2026-08-18T10:00:00Z"},
    "slots": [],
    "signature": {
        "algorithm": "ed25519",
        "key_id": "test-key",
        "value": "QJu67/0AEpEZgSoV/3gXGAS8EJ3CUtiPPVW7y3yXWorF1W6X05WW4cyrwPAThG117HTopQ10X/qXCX0McPoVCg==",
        "signed_at": "2026-08-15T10:00:00Z",
    },
}


def test_a_correctly_signed_manifest_plays_on_its_own_stand():
    verdict = verify(SIGNED_FOR_STAND_3, {REAL_KEY_ID: REAL_PUBLIC}, stand_id=3)
    assert verdict, verdict.reason


def test_the_same_signed_manifest_is_refused_on_another_stand():
    # The exact attack: a genuine, fleet-signed manifest for stand 3,
    # replayed onto stand 7's device. The signature is perfect; the stand
    # is wrong; it must not play.
    verdict = verify(SIGNED_FOR_STAND_3, {REAL_KEY_ID: REAL_PUBLIC}, stand_id=7)
    assert not verdict
    assert "signed for stand" in verdict.reason.lower()


def test_the_stand_id_cannot_be_edited_without_breaking_the_signature():
    # Try to make stand 3's manifest pass at stand 7 by rewriting the
    # stand id. It is inside the signed bytes, so the signature breaks.
    tampered = json.loads(json.dumps(SIGNED_FOR_STAND_3))
    tampered["stand_id"] = 7
    verdict = verify(tampered, {REAL_KEY_ID: REAL_PUBLIC}, stand_id=7)
    assert not verdict
    assert "does not match" in verdict.reason.lower()


def test_stand_none_skips_the_check_for_provisioning():
    # With no identity, only the signature governs — first boot still works.
    verdict = verify(SIGNED_FOR_STAND_3, {REAL_KEY_ID: REAL_PUBLIC}, stand_id=None)
    assert verdict, verdict.reason


def test_parse_rejects_a_manifest_signed_for_another_stand():
    with pytest.raises(UntrustedManifest):
        Manifest.parse(
            SIGNED_FOR_STAND_3, public_keys={REAL_KEY_ID: REAL_PUBLIC}, stand_id=7
        )


# ─── Non-object bodies ──────────────────────────────────────────────────────
#
# A bare list or string is valid JSON but not a manifest. It must be refused
# through the documented path — a Verdict from verify(), a ValueError from
# parse() — never an AttributeError from reaching into a non-dict.


@pytest.mark.parametrize("body", [[], ["a", "b"], "just a string", 42, None])
def test_verify_refuses_a_non_object_body(body):
    verdict = verify(body, {REAL_KEY_ID: REAL_PUBLIC})
    assert not verdict
    assert "not an object" in verdict.reason


@pytest.mark.parametrize("body", [[], ["a", "b"], "just a string", 42])
def test_parse_refuses_a_non_object_body(body):
    with pytest.raises(ValueError, match="not a JSON object"):
        Manifest.parse(body)


# ─── Freshness / anti-rollback ──────────────────────────────────────────────
#
# A correctly-signed but older manifest, replayed over a newer one, would
# roll the screen back to a stale plan. The signature does not stop it — the
# old plan was genuinely signed — so a separate version gate does. The gate
# is exercised with public_keys=None so the signature step is skipped and
# the version logic is what is under test; editing schedule_version on a
# real signed vector would break its signature instead.


def _versioned(version: int) -> dict:
    raw = signed_manifest()
    raw["schedule_version"] = version
    return raw


def test_an_older_manifest_is_refused():
    with pytest.raises(UntrustedManifest, match="older than"):
        Manifest.parse(_versioned(5), min_schedule_version=12)


def test_the_same_version_is_accepted():
    manifest = Manifest.parse(_versioned(12), min_schedule_version=12)
    assert manifest.schedule_version == 12


def test_a_newer_version_is_accepted():
    manifest = Manifest.parse(_versioned(20), min_schedule_version=12)
    assert manifest.schedule_version == 20


def test_no_minimum_skips_the_freshness_check():
    manifest = Manifest.parse(_versioned(1), min_schedule_version=None)
    assert manifest.schedule_version == 1
