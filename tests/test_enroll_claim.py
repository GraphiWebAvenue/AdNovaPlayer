"""
The claim path: a device that was invited by name.

A claim token arrives on the SD card, minted against one stand. It turns
enrollment from "knock and wait for somebody to recognise the hardware" into
"present the invitation and be adopted". These tests hold the three things
that path must get right: the token reaches Dashboard, a refusal is reported
as a refusal rather than as patience, and the spent token does not linger in
the environment file afterwards.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from adnova_player.config import enrollment
from adnova_player.enroll import Adoption, Enroller, write_credentials


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x")


def test_claim_token_rides_the_introduction(tmp_path: Path) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"status": "approved"})

    enroller = Enroller("https://d.example", "fleet-token", tmp_path, claim_token="CLAIM-123")
    assert enroller.introduce(_client(handler)) == "approved"
    assert seen["claim_token"] == "CLAIM-123"
    # The device fingerprint still goes along: the claim says which stand,
    # the fingerprint says which box, and Dashboard records both.
    assert seen["device_id"] and seen["device_secret_hash"]


def test_no_claim_token_means_no_claim_field(tmp_path: Path) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"status": "pending"})

    Enroller("https://d.example", "fleet-token", tmp_path).introduce(_client(handler))
    assert "claim_token" not in seen


def test_a_refused_claim_is_not_reported_as_closed(tmp_path: Path) -> None:
    """
    403 means two different things. Without a claim it is "enrollment is shut,
    try later" — worth waiting out. With one it is "this token is dead", which
    waiting never mends, and the two must not be confused: one asks a
    technician to wait, the other asks them to fetch a new claim file.
    """
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "claim token is not valid"})

    with_claim = Enroller("https://d.example", "", tmp_path / "a", claim_token="DEAD")
    without = Enroller("https://d.example", "fleet", tmp_path / "b")

    assert with_claim.introduce(_client(handler)) == "claim_refused"
    assert without.introduce(_client(handler)) == "closed"


def test_adoption_clears_the_spent_claim_token(tmp_path: Path) -> None:
    env = tmp_path / "env"
    env.write_text(
        "ADNOVA_BASE_URL=https://d.example\n"
        "ADNOVA_CLAIM_TOKEN=SPENT\n"
        "ADNOVA_ADMIN_USER=admin\n",
        encoding="utf-8",
    )

    write_credentials(env, Adoption(stand_id=7, stand_key="k" * 64, key_id="k1", public_key="pub"))

    body = env.read_text(encoding="utf-8")
    assert "ADNOVA_CLAIM_TOKEN" not in body
    assert "ADNOVA_STAND_KEY=" + "k" * 64 in body
    # Lines the installer or an operator wrote survive untouched.
    assert "ADNOVA_ADMIN_USER=admin" in body


@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, None),
        ({"ADNOVA_CLAIM_TOKEN": "abc"}, "abc"),
        ({"ADNOVA_ENROLL_TOKEN": "fleet"}, ""),
    ],
)
def test_a_claim_token_alone_is_enough_to_enter_enrollment(env, expected) -> None:
    """
    A claimed device carries no fleet token — the claim is its whole
    authorisation. If config still demanded the fleet token, every Pi
    provisioned the new way would refuse to start.
    """
    result = enrollment(env)
    if expected is None:
        assert result is None
    else:
        assert result is not None and result.claim_token == expected
