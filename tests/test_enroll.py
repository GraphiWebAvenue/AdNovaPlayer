"""
Zero-touch enrollment, device side.

The device introduces itself, waits, and — only once Dashboard says
approved — collects and writes the key. These pin that it never sends its
secret in the clear, that it resumes the same identity across a restart,
and that credentials are written without clobbering the rest of the file.
"""

from __future__ import annotations

import hashlib

import httpx

from adnova_player.enroll import Adoption, Enroller, write_credentials


def enroller(tmp_path, handler):
    e = Enroller("https://dash.example", "fleet-token-123", tmp_path / "enroll")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return e, client


def test_the_secret_is_never_sent_only_its_hash(tmp_path):
    seen = {}

    def handler(req):
        seen["body"] = req.content.decode()
        seen["token"] = req.headers.get("X-AdNova-Enroll")
        return httpx.Response(200, json={"status": "pending"})

    e, client = enroller(tmp_path, handler)
    e.introduce(client)

    # The raw secret is on disk but never in the request; only its hash is.
    secret = (tmp_path / "enroll" / "device_secret").read_text()
    assert secret not in seen["body"]
    assert hashlib.sha256(secret.encode()).hexdigest() in seen["body"]
    assert seen["token"] == "fleet-token-123"


def test_the_identity_is_stable_across_instances(tmp_path):
    e1, c1 = enroller(tmp_path, lambda r: httpx.Response(200, json={"status": "pending"}))
    e1.introduce(c1)
    first_id = (tmp_path / "enroll" / "device_id").read_text()

    # A "reboot" — a fresh Enroller over the same state dir reuses the id.
    e2, c2 = enroller(tmp_path, lambda r: httpx.Response(200, json={"status": "pending"}))
    e2.introduce(c2)
    assert (tmp_path / "enroll" / "device_id").read_text() == first_id


def test_poll_returns_an_adoption_once_approved(tmp_path):
    def handler(req):
        return httpx.Response(200, json={
            "status": "approved",
            "stand_id": 10,
            "stand_key": "k" * 64,
            "signing_key": {"key_id": "kid1", "public_key": "cHVi"},
        })

    e, client = enroller(tmp_path, handler)
    result = e.poll(client)

    assert isinstance(result, Adoption)
    assert result.stand_id == 10
    assert result.stand_key == "k" * 64
    assert result.key_id == "kid1"


def test_poll_reports_pending_and_rejected(tmp_path):
    e, client = enroller(tmp_path, lambda r: httpx.Response(200, json={"status": "pending"}))
    assert e.poll(client) == "pending"

    e2, c2 = enroller(tmp_path, lambda r: httpx.Response(200, json={"status": "rejected"}))
    assert e2.poll(c2) == "rejected"


def test_a_network_error_is_not_fatal(tmp_path):
    def handler(req):
        raise httpx.ConnectError("no network yet")

    e, client = enroller(tmp_path, handler)
    assert e.introduce(client) is None
    assert e.poll(client) is None


def test_write_credentials_sets_the_keys_and_keeps_the_rest(tmp_path):
    env = tmp_path / "env"
    env.write_text(
        "# comment\n"
        "ADNOVA_BASE_URL=https://dash.example\n"
        "ADNOVA_CACHE_DIR=/var/lib/adnova-player\n"
        "ADNOVA_ENROLL_TOKEN=fleet-token\n"
    )

    write_credentials(env, Adoption(
        stand_id=10, stand_key="k" * 64, key_id="kid1", public_key="cHVi",
    ))

    text = env.read_text()
    assert "ADNOVA_STAND_ID=10" in text
    assert f"ADNOVA_STAND_KEY={'k' * 64}" in text
    assert '"kid1": "cHVi"' in text
    # The pre-existing settings survive.
    assert "ADNOVA_BASE_URL=https://dash.example" in text
    assert "ADNOVA_CACHE_DIR=/var/lib/adnova-player" in text
