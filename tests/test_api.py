"""
The signed client.

Two things are pinned: every request Dashboard receives is signed the way
AuthenticateStand expects, and no network failure ever raises — the loop
must keep turning while the shop wifi flaps.
"""

from __future__ import annotations

import httpx

from adnova_player.api import DashboardApi
from adnova_player.config import load
from adnova_player.signing import (
    HEADER_ID,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
)

CONFIG = load({
    "ADNOVA_STAND_ID": "3",
    "ADNOVA_STAND_KEY": "a" * 64,
    "ADNOVA_BASE_URL": "https://dash.example",
})


def api_with(handler) -> DashboardApi:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return DashboardApi(CONFIG, client=client)


def test_every_request_is_signed():
    seen = {}

    def handler(req):
        # httpx headers are case-insensitive; keep the object rather than
        # flattening to a plain dict so the lookups below stay honest.
        seen["headers"] = req.headers
        return httpx.Response(200, json={"contract_version": "player_manifest.v1"})

    api_with(handler).fetch_manifest()

    headers = seen["headers"]
    for header in (HEADER_ID, HEADER_TIMESTAMP, HEADER_NONCE, HEADER_SIGNATURE):
        assert header in headers
    assert headers[HEADER_ID] == "3"


def test_the_manifest_comes_back_as_the_raw_document():
    doc = {"contract_version": "player_manifest.v1", "stand_id": 3, "signature": {}}
    api = api_with(lambda req: httpx.Response(200, json=doc))

    assert api.fetch_manifest() == doc


def test_a_network_error_returns_none_not_an_exception():
    def handler(req):
        raise httpx.ConnectError("wifi down")

    # The whole promise: a dropped connection is a None, never a crash.
    assert api_with(handler).fetch_manifest() is None


def test_a_500_returns_none():
    assert api_with(lambda req: httpx.Response(503)).fetch_manifest() is None


def test_a_401_returns_none():
    assert api_with(lambda req: httpx.Response(401)).fetch_manifest() is None


def test_auth_failures_are_counted_and_cleared_on_success():
    # A run of 401/403 is counted so the agent can tell a dead key from a
    # blip; the first accepted response clears the streak.
    codes = iter([401, 403, 401, 200])
    doc = {"contract_version": "player_manifest.v1"}

    def handler(req):
        code = next(codes)
        return httpx.Response(code, json=doc if code == 200 else None)

    api = api_with(handler)
    api.fetch_manifest(); assert api.auth_failures == 1
    api.fetch_manifest(); assert api.auth_failures == 2
    api.fetch_manifest(); assert api.auth_failures == 3
    api.fetch_manifest(); assert api.auth_failures == 0   # accepted → reset


def test_a_non_json_manifest_returns_none():
    api = api_with(lambda req: httpx.Response(200, content=b"<html>not json"))
    assert api.fetch_manifest() is None


def test_heartbeat_sends_a_signed_post_and_reads_the_control_channel():
    captured = {}

    def handler(req):
        captured["method"] = req.method
        captured["body"] = req.content
        captured["signed"] = HEADER_SIGNATURE.lower() in {k.lower() for k in req.headers}
        return httpx.Response(200, json={"refetch_manifest": True})

    result = api_with(handler).send_heartbeat({"state": "playing"})

    assert captured["method"] == "POST"
    assert captured["signed"] is True
    assert result == {"refetch_manifest": True}


def test_playback_returns_none_on_failure_so_the_caller_can_retry():
    # Idempotent server-side, so retrying after a None never double-bills.
    assert api_with(lambda req: httpx.Response(500)).send_playback({"entries": []}) is None
