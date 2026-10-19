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
"""FastMCP factory and the per-request credential middleware."""

import contextvars
import re
import uuid
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .api_client import SpanningClient, SpanningCredentials
from .config import PLATFORM_ALIASES, REGIONS, Settings

HEADER_API_TOKEN = "X-Spanning-API-Token"
HEADER_ADMIN_EMAIL = "X-Spanning-Admin-Email"
HEADER_PLATFORM = "X-Spanning-Platform"
HEADER_REGION = "X-Spanning-Region"
REQUIRED_HEADERS = [HEADER_API_TOKEN, HEADER_ADMIN_EMAIL]

_PLACEHOLDER = re.compile(r"^\$\{.*\}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_REGION_IN_HOST = re.compile(r"o365-api-([a-z]{2})\.spanningbackup\.com", re.IGNORECASE)

# Per-request isolation: the middleware sets these for the duration of one
# HTTP request and resets them in `finally`. Nothing credential-related is
# ever stored at module level.
_credentials_var: contextvars.ContextVar[SpanningCredentials | None] = contextvars.ContextVar(
    "spanning_credentials", default=None
)
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("spanning_request_id", default="-")

INSTRUCTIONS = (
    "Spanning Backup backs up a Microsoft 365 tenant's mailboxes, OneDrive, "
    "calendars, contacts, SharePoint sites and Teams channels every day. This "
    "server reads that account: which users are licensed (assigned) for "
    "backup, and how each day's backup went per user and per service.\n"
    "Usage:\n"
    "- Health of the account: spanning_get_license_usage (licenses vs "
    "assigned users) and spanning_list_users (assigned / deleted flags).\n"
    "- One person: spanning_get_user(userId = their user principal name) -> "
    "spanning_list_services (latest result per service) -> "
    "spanning_list_backups(service, days) for the daily history.\n"
    "- Backup results count objects: successful, partial and failed.\n"
    "Restores and the admin audit log are not available in Spanning's API; "
    "those tools say so and point to the Spanning web app. Only Microsoft 365 "
    "accounts are supported."
)


def get_client_from_context() -> SpanningClient | None:
    """The client for the current request, or None outside a request."""
    credentials = _credentials_var.get()
    if credentials is None:
        return None
    return SpanningClient(credentials)


def _clean(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text or _PLACEHOLDER.match(text):
        return None
    return text


def normalize_platform(value: str | None) -> str | None:
    """Canonical platform (m365, gws, salesforce); m365 when absent; None when unknown."""
    if not value:
        return "m365"
    key = re.sub(r"[^a-z0-9]", "", value.lower())
    return PLATFORM_ALIASES.get(key)


def normalize_region(value: str) -> str | None:
    text = value.strip().lower()
    if text in REGIONS:
        return text
    match = _REGION_IN_HOST.search(text)
    return match.group(1).lower() if match and match.group(1).lower() in REGIONS else None


class GatewayCredentialsMiddleware:
    """Reads the Spanning headers on /mcp requests into the request context.

    Needs X-Spanning-API-Token and X-Spanning-Admin-Email; otherwise 401.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)

        def header(name: str) -> str | None:
            return _clean(request.headers.get(name))

        token, email = header(HEADER_API_TOKEN), header(HEADER_ADMIN_EMAIL)
        missing = [name for name, value in ((HEADER_API_TOKEN, token), (HEADER_ADMIN_EMAIL, email)) if not value]
        if missing:
            await JSONResponse(
                {
                    "error": "Missing credentials",
                    "message": f"Missing header(s): {', '.join(missing)}",
                    "required_headers": REQUIRED_HEADERS,
                    "optional_headers": [HEADER_PLATFORM, HEADER_REGION],
                },
                status_code=401,
            )(scope, receive, send)
            return

        platform = normalize_platform(header(HEADER_PLATFORM))
        region_raw = header(HEADER_REGION)
        region = normalize_region(region_raw) if region_raw else None
        problem = None
        if not _EMAIL.match(email or ""):
            problem = f"{HEADER_ADMIN_EMAIL} must be an email address"
        elif platform is None:
            problem = f"{HEADER_PLATFORM} must be m365 (o365), gws (gapps) or salesforce"
        elif region_raw and not region:
            problem = f"{HEADER_REGION} must be one of {', '.join(REGIONS)}"
        if problem:
            await JSONResponse({"error": "Invalid header", "message": problem}, status_code=400)(scope, receive, send)
            return

        credentials = SpanningCredentials(
            admin_email=email or "", api_token=token or "", platform=platform or "m365", region=region
        )
        request_id = (request.headers.get("x-request-id") or "").strip()[:64] or uuid.uuid4().hex[:12]
        creds_token = _credentials_var.set(credentials)
        rid_token = request_id_var.set(request_id)
        try:
            await self.app(scope, receive, send)
        finally:
            _credentials_var.reset(creds_token)
            request_id_var.reset(rid_token)


def create_mcp_server(settings: Settings | None = None) -> FastMCP:
    """Build the FastMCP server and register every tool."""
    mcp = FastMCP(
        name="spanning-mcp",
        instructions=INSTRUCTIONS,
        # The service sits behind the gateway, so Host is never localhost;
        # DNS-rebinding protection would reject those requests with 421.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        stateless_http=True,
    )

    client_factory: Callable[[], SpanningClient | None] = get_client_from_context

    from .tools import account, users

    for module in (account, users):
        module.register(mcp, client_factory)

    return mcp
