# Copyright 2026 MSPbots.ai
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Credential middleware: 401/400 on bad headers, per-request isolation."""

import asyncio

import pytest
from starlette.testclient import TestClient

from spanning_mcp.__main__ import build_http_app
from spanning_mcp.server import (
    GatewayCredentialsMiddleware,
    _credentials_var,
    create_mcp_server,
    get_client_from_context,
    normalize_platform,
    normalize_region,
)

MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
CREDS = {"X-Spanning-API-Token": "your-api-token", "X-Spanning-Admin-Email": "admin@example.com"}


def _client() -> TestClient:
    return TestClient(build_http_app(create_mcp_server()))


def test_health_is_local():
    with _client() as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.parametrize("drop", list(CREDS))
def test_each_required_header_is_required(drop):
    headers = {k: v for k, v in CREDS.items() if k != drop}
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | headers)
    assert resp.status_code == 401
    assert drop in resp.json()["message"]


@pytest.mark.parametrize(
    "extra",
    [
        {"X-Spanning-Admin-Email": "not-an-email"},
        {"X-Spanning-Platform": "dropbox"},
        {"X-Spanning-Region": "mars"},
    ],
)
def test_bad_values_return_400(extra):
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | CREDS | extra)
    assert resp.status_code == 400


def test_tools_list_over_http():
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | CREDS)
    assert resp.status_code == 200
    for name in ("spanning_status", "spanning_list_users", "spanning_list_backups"):
        assert name in resp.text


@pytest.mark.parametrize(
    "value,expected",
    [(None, "m365"), ("o365", "m365"), ("M365", "m365"), ("Office 365", "m365"), ("gapps", "gws"),
     ("Google Workspace", "gws"), ("salesforce", "salesforce"), ("dropbox", None)],
)
def test_normalize_platform(value, expected):
    assert normalize_platform(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [("us", "us"), ("EU", "eu"), ("https://o365-api-uk.spanningbackup.com", "uk"), ("xx", None)],
)
def test_normalize_region(value, expected):
    assert normalize_region(value) == expected


def _run_middleware(headers: list[tuple[bytes, bytes]]) -> dict:
    seen: dict = {}

    async def app(scope, receive, send):
        seen["credentials"] = _credentials_var.get()
        seen["client"] = get_client_from_context()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    asyncio.run(GatewayCredentialsMiddleware(app)({"type": "http", "path": "/mcp", "headers": headers}, receive, send))
    return seen


def test_headers_reach_request_context_and_are_reset():
    seen = _run_middleware(
        [
            (b"x-spanning-api-token", b" token-123 "),
            (b"x-spanning-admin-email", b"admin@example.com"),
            (b"x-spanning-platform", b"o365"),
            (b"x-spanning-region", b"EU"),
        ]
    )
    creds = seen["credentials"]
    assert (creds.api_token, creds.admin_email, creds.platform, creds.region) == ("token-123", "admin@example.com", "m365", "eu")
    assert seen["client"].region == "eu"
    assert _credentials_var.get() is None


def test_concurrent_requests_do_not_share_credentials():
    observed: list[tuple[str, str]] = []

    async def app(scope, receive, send):
        before = _credentials_var.get().api_token
        await asyncio.sleep(0.01)
        observed.append((before, _credentials_var.get().api_token))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    middleware = GatewayCredentialsMiddleware(app)

    def scope(token: bytes) -> dict:
        return {
            "type": "http",
            "path": "/mcp",
            "headers": [(b"x-spanning-api-token", token), (b"x-spanning-admin-email", b"admin@example.com")],
        }

    async def both():
        await asyncio.gather(middleware(scope(b"tenant-a"), receive, send), middleware(scope(b"tenant-b"), receive, send))

    asyncio.run(both())
    assert sorted(observed) == [("tenant-a", "tenant-a"), ("tenant-b", "tenant-b")]


def test_credentials_repr_hides_token():
    seen = _run_middleware(
        [(b"x-spanning-api-token", b"very-secret-token"), (b"x-spanning-admin-email", b"admin@example.com")]
    )
    assert "very-secret-token" not in repr(seen["credentials"])
