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
"""Region detection, request shapes and backup-summary shaping against a mocked API."""

import base64
import json
from datetime import UTC, datetime

import httpx
import pytest

from spanning_mcp import api_client
from spanning_mcp.api_client import SpanningClient, SpanningCredentials, SpanningError
from spanning_mcp.server import _credentials_var, create_mcp_server
from spanning_mcp.tools._common import outcome, service_types, window_ms

CREDS = SpanningCredentials(admin_email="admin@example.com", api_token="token-123", region="us")
UPN = "jane@example.com"


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(api_client.asyncio, "sleep", instant)
    api_client.clear_region_cache()


def summary(date, kind, successful=0, partial=0, failed=0):
    return {"date": date, "type": kind, "backup": {"total": successful + partial + failed,
            "successful": successful, "partial": partial, "failed": failed}}


class Recorder:
    """Mock transport: replies by path below /external/."""

    def __init__(self, routes=None, default=None):
        self.requests: list[httpx.Request] = []
        self.routes = routes or {}
        self.default = default if default is not None else {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/external/")
        reply = self.routes.get(path, self.default)
        if callable(reply):
            return reply(request)
        return httpx.Response(200, json=reply)


async def _call(monkeypatch, recorder: Recorder, tool: str, args: dict, credentials: SpanningCredentials = CREDS):
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    monkeypatch.setattr(api_client, "_get_shared_http_client", lambda: http)
    ctx = _credentials_var.set(credentials)
    try:
        content, _ = await create_mcp_server().call_tool(tool, args)
    finally:
        _credentials_var.reset(ctx)
    return json.loads(content[0].text)


def _client(recorder: Recorder, credentials: SpanningCredentials = CREDS) -> SpanningClient:
    return SpanningClient(credentials, httpx.AsyncClient(transport=httpx.MockTransport(recorder)))


# ---- authentication and region ---------------------------------------------


async def test_basic_auth_and_regional_host():
    rec = Recorder()
    await _client(rec).get("tenant")
    request = rec.requests[0]
    assert str(request.url) == "https://o365-api-us.spanningbackup.com/external/tenant"
    expected = base64.b64encode(b"admin@example.com:token-123").decode()
    assert request.headers["Authorization"] == f"Basic {expected}"


async def test_region_is_detected_and_cached():
    def route(request: httpx.Request) -> httpx.Response:
        ok = request.url.host == "o365-api-eu.spanningbackup.com"
        return httpx.Response(200 if ok else 401, json={"status": "paid"})

    rec = Recorder({"tenant": route})
    creds = SpanningCredentials(admin_email="admin@example.com", api_token="token-123")
    client = _client(rec, creds)
    await client.get("tenant")
    assert client.region == "eu"
    probes = len(rec.requests) - 1
    assert probes == 6
    await _client(rec, creds).get("tenant")
    assert len(rec.requests) == probes + 2


async def test_no_region_accepts_the_credentials(monkeypatch):
    rec = Recorder(default=lambda r: httpx.Response(401, json={"message": "Unauthorized"}))
    creds = SpanningCredentials(admin_email="admin@example.com", api_token="token-123")
    result = await _call(monkeypatch, rec, "spanning_status", {}, creds)
    assert result["error"]["code"] == "unauthorized"
    assert "X-Spanning-Region" in result["error"]["message"]
    assert "token-123" not in json.dumps(result)


async def test_retries_5xx():
    replies = iter([httpx.Response(502), httpx.Response(200, json={"status": "paid"})])
    rec = Recorder({"tenant": lambda r: next(replies)})
    assert await _client(rec).get("tenant") == {"status": "paid"}


async def test_other_platforms_are_refused(monkeypatch):
    creds = SpanningCredentials(admin_email="admin@example.com", api_token="t", platform="gws", region="us")
    rec = Recorder()
    result = await _call(monkeypatch, rec, "spanning_list_users", {}, creds)
    assert result["error"]["code"] == "invalid_argument"
    assert "Microsoft 365" in result["error"]["message"]
    assert rec.requests == []


# ---- helpers ---------------------------------------------------------------


def test_window_ms_starts_at_midnight():
    start, end = window_ms(2, datetime(2026, 6, 10, 15, 30, tzinfo=UTC))
    assert start == int(datetime(2026, 6, 9, tzinfo=UTC).timestamp() * 1000)
    assert end == int(datetime(2026, 6, 10, 15, 30, tzinfo=UTC).timestamp() * 1000)


@pytest.mark.parametrize(
    "service,expected",
    [("mail", ("MAIL",)), ("OneDrive", ("DRIVE",)), ("sharepoint", ("SHAREPOINT", "SPSITE")), ("TEAMS_CHANNEL", ("TEAMS_CHANNEL",))],
)
def test_service_types(service, expected):
    assert service_types(service) == expected


def test_unknown_service():
    with pytest.raises(ValueError):
        service_types("photos")


@pytest.mark.parametrize(
    "entry,expected",
    [(summary("d", "MAIL", successful=3), "successful"), (summary("d", "MAIL", successful=2, partial=1), "partial"),
     (summary("d", "MAIL", successful=2, failed=1), "failed"), ({"type": "MAIL"}, "none")],
)
def test_outcome(entry, expected):
    assert outcome(entry) == expected


# ---- tools -----------------------------------------------------------------


async def test_list_users_paging_token(monkeypatch):
    next_link = "https://o365-api-us.spanningbackup.com/external/users?size=2&sortPropOffset=jane%40example.com&msIdOffset=abc"
    rec = Recorder({"users": {"users": [{"userPrincipalName": UPN, "assigned": True}, {"userPrincipalName": "x@example.com", "assigned": False}], "nextLink": next_link}})
    result = await _call(monkeypatch, rec, "spanning_list_users", {"limit": 2, "assignedOnly": True})
    params = rec.requests[0].url.params
    assert (params["size"], params["status"]) == ("2", "false")
    assert [u["userPrincipalName"] for u in result["users"]] == [UPN]
    assert result["next_page_token"] == "jane@example.com|abc"
    await _call(monkeypatch, rec, "spanning_list_users", {"pageToken": result["next_page_token"]})
    params = rec.requests[1].url.params
    assert (params["sortPropOffset"], params["msIdOffset"]) == ("jane@example.com", "abc")


async def test_bad_page_token(monkeypatch):
    result = await _call(monkeypatch, Recorder(), "spanning_list_users", {"pageToken": "garbage"})
    assert result["error"]["code"] == "invalid_argument"


async def test_get_user_adds_outcomes(monkeypatch):
    rec = Recorder({f"users/{UPN}": {"userPrincipalName": UPN, "backupSummary": [summary("2026-06-10", "MAIL", failed=1)]}})
    result = await _call(monkeypatch, rec, "spanning_get_user", {"userId": UPN})
    assert rec.requests[0].url.path == f"/external/users/{UPN}"
    assert result["backupSummary"][0]["outcome"] == "failed"


async def test_list_services_keeps_latest_per_type(monkeypatch):
    rows = [summary("2026-06-09", "MAIL", successful=1), summary("2026-06-10", "MAIL", partial=1),
            summary("2026-06-10", "DRIVE", successful=4)]
    rec = Recorder({f"users/{UPN}": {"userPrincipalName": UPN, "assigned": True, "backupSummary": rows}})
    result = await _call(monkeypatch, rec, "spanning_list_services", {"userId": UPN})
    params = rec.requests[0].url.params
    assert params["status"] == "true" and len(params["start"]) == 13
    services = {s["service"]: s for s in result["services"]}
    assert services["mail"]["outcome"] == "partial" and services["mail"]["last_backup_date"] == "2026-06-10"
    assert services["drive"]["outcome"] == "successful"


async def test_list_backups_filters_service_newest_first(monkeypatch):
    rows = [summary("2026-06-08", "MAIL", successful=1), summary("2026-06-10", "MAIL", failed=2), summary("2026-06-10", "DRIVE", successful=1)]
    rec = Recorder({f"users/{UPN}": {"backupSummary": rows}})
    result = await _call(monkeypatch, rec, "spanning_list_backups", {"userId": UPN, "service": "mail", "days": 30})
    window = rec.requests[0].url.params
    assert int(window["end"]) - int(window["start"]) <= 30 * 24 * 3600 * 1000
    assert [b["date"] for b in result["backups"]] == ["2026-06-10", "2026-06-08"]
    assert result["backups"][0]["outcome"] == "failed"
    assert result["days"] == 30


async def test_license_usage(monkeypatch):
    rec = Recorder({"tenant": {"licenses": 50, "assignedStandard": 42, "assigned": 44, "status": "paid"}})
    result = await _call(monkeypatch, rec, "spanning_get_license_usage", {})
    assert result["available_standard_licenses"] == 8


async def test_status(monkeypatch):
    rec = Recorder({"tenant": {"status": "paid"}})
    result = await _call(monkeypatch, rec, "spanning_status", {})
    assert result == {"connected": True, "platform": "m365", "region": "us", "admin_email": "admin@example.com", "tenant_status": "paid"}


@pytest.mark.parametrize(
    "tool,args,flag",
    [("spanning_queue_restore", {"userId": UPN, "service": "mail", "items": []}, "queued"),
     ("spanning_get_restore_status", {"restoreId": "r1"}, "available"),
     ("spanning_list_audit_log", {}, "available")],
)
async def test_unsupported_operations_call_nothing(monkeypatch, tool, args, flag):
    rec = Recorder()
    result = await _call(monkeypatch, rec, tool, args)
    assert result[flag] is False
    assert "web app" in result["message"]
    assert rec.requests == []


async def test_error_message_from_api():
    rec = Recorder({"tenant": lambda r: httpx.Response(404, json={"message": "User not found"})})
    with pytest.raises(SpanningError) as err:
        await _client(rec).get("tenant")
    assert err.value.status_code == 404 and "User not found" in err.value.message
