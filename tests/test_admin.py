"""
The on-site status page and its login.

Two things are pinned: the password scheme is a real salted hash checked
in constant time, and the page is genuinely locked — no login, wrong
login, and an unconfigured password all fail closed.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from adnova_player.admin import attach_admin, hash_password, verify_password
from adnova_player.config import load


def config_with(password_hash: str):
    return load({
        "ADNOVA_STAND_ID": "3",
        "ADNOVA_STAND_KEY": "a" * 64,
        "ADNOVA_ADMIN_USER": "tech",
        "ADNOVA_ADMIN_PASSWORD_HASH": password_hash,
    })


def app_with(config):
    app = FastAPI()
    attach_admin(
        app,
        config,
        status=lambda: {"stand_id": 3, "online": True, "warnings": []},
        on_action=lambda name: name == "refetch",
    )
    return TestClient(app)


# ── Password scheme ────────────────────────────────────────────────────────


def test_a_password_round_trips():
    stored = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", stored)
    assert not verify_password("wrong", stored)


def test_the_hash_is_salted_so_two_identical_passwords_differ():
    assert hash_password("same") != hash_password("same")


def test_a_garbage_hash_never_verifies():
    assert not verify_password("anything", "not-a-real-hash")
    assert not verify_password("anything", "")


# ── The locked page ────────────────────────────────────────────────────────


def test_the_page_needs_a_login():
    client = app_with(config_with(hash_password("secret")))
    assert client.get("/admin/status").status_code == 401


def test_the_right_login_works():
    client = app_with(config_with(hash_password("secret")))
    res = client.get("/admin/status", auth=("tech", "secret"))
    assert res.status_code == 200
    assert res.json()["stand_id"] == 3


def test_a_wrong_password_is_refused():
    client = app_with(config_with(hash_password("secret")))
    assert client.get("/admin/status", auth=("tech", "nope")).status_code == 401


def test_a_wrong_username_is_refused():
    client = app_with(config_with(hash_password("secret")))
    assert client.get("/admin/status", auth=("intruder", "secret")).status_code == 401


def test_an_unconfigured_password_locks_the_page_entirely():
    # No hash → the page is closed, not open. Fail-closed for a page that
    # reveals the device's identity.
    client = app_with(config_with(""))
    assert client.get("/admin/status", auth=("tech", "anything")).status_code == 503


def test_a_known_action_is_accepted_and_an_unknown_one_is_not():
    client = app_with(config_with(hash_password("secret")))
    assert client.post("/admin/action/refetch", auth=("tech", "secret")).json()["ok"] is True
    assert client.post("/admin/action/wipe", auth=("tech", "secret")).status_code == 400
