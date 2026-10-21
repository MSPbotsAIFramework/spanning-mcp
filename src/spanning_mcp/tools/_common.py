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
"""Shared argument types, backup-summary helpers and the error-handling wrapper."""

import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from .._json import dump_json_capped, error_envelope
from ..api_client import SpanningClient, SpanningError
from ..config import SUPPORTED_PLATFORM

logger = logging.getLogger("spanning_mcp.tools")

ClientFactory = Callable[[], SpanningClient | None]

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)
LOCAL = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)

NOT_CONFIGURED = error_envelope(
    "not_configured",
    "Spanning credentials are missing for this request (X-Spanning-API-Token, X-Spanning-Admin-Email).",
    False,
)

UserId = Annotated[str, Field(description="The user's Microsoft 365 user principal name (e.g. jane@example.com).", min_length=1)]

# Service names callers use, mapped to Spanning's backup types.
SERVICE_TYPES = {
    "mail": ("MAIL",),
    "email": ("MAIL",),
    "mailbox": ("MAIL",),
    "drive": ("DRIVE",),
    "onedrive": ("DRIVE",),
    "calendar": ("CALENDAR",),
    "contacts": ("CONTACT",),
    "contact": ("CONTACT",),
    "sites": ("SHAREPOINT", "SPSITE"),
    "sharepoint": ("SHAREPOINT", "SPSITE"),
    "teams": ("TEAMS_CHANNEL",),
}
SERVICE_NAMES = {"MAIL": "mail", "DRIVE": "drive", "CALENDAR": "calendar", "CONTACT": "contacts",
                 "SHAREPOINT": "sites", "SPSITE": "sites", "TEAMS_CHANNEL": "teams"}


def compact(values: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None}


def service_types(service: str) -> tuple[str, ...]:
    key = "".join(ch for ch in service.lower() if ch.isalpha())
    if key in SERVICE_TYPES:
        return SERVICE_TYPES[key]
    raw = service.strip().upper()
    if raw in SERVICE_NAMES:
        return (raw,)
    raise ValueError(f"Unknown service {service!r}; use mail, drive, calendar, contacts, sites or teams.")


def window_ms(days: int, now: datetime | None = None) -> tuple[int, int]:
    """[start, end] in the 13-digit millisecond timestamps Spanning expects.

    Covers today plus the days - 1 before it, so the span never exceeds the
    API's per-request limit (30 days for one user).
    """
    end = now or datetime.now(UTC)
    start = (end - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def outcome(entry: dict) -> str:
    """One word for a backup summary entry: successful, partial, failed or none."""
    backup = entry.get("backup") if isinstance(entry.get("backup"), dict) else {}
    if (backup.get("failed") or 0) > 0:
        return "failed"
    if (backup.get("partial") or 0) > 0:
        return "partial"
    if (backup.get("successful") or 0) > 0:
        return "successful"
    return "none"


def summary_rows(user: Any) -> list[dict]:
    rows = user.get("backupSummary") if isinstance(user, dict) else None
    return [r for r in rows or [] if isinstance(r, dict)]


async def run_tool(
    name: str,
    client_factory: ClientFactory,
    action: Callable[[SpanningClient], Awaitable[Any]],
    needs_api: bool = True,
) -> str:
    """Resolve the caller's client, run ``action``, and always return a string.

    Errors never escape as exceptions: they become the standard error envelope.
    """
    client = client_factory()
    if client is None:
        return NOT_CONFIGURED
    if needs_api and client.header_error:
        return error_envelope("invalid_argument", client.header_error, False)
    if needs_api and client.platform != SUPPORTED_PLATFORM:
        return error_envelope(
            "invalid_argument",
            f"Spanning's public API covers Microsoft 365 accounts only; this connection is set to "
            f"{client.platform}. Use the Spanning web app for Google Workspace or Salesforce.",
            False,
        )
    started = time.monotonic()
    try:
        result = await action(client)
    except SpanningError as e:
        logger.info("tool=%s outcome=error status=%s ms=%d", name, e.status_code, _ms(started))
        return e.to_envelope()
    except ValueError as e:
        logger.info("tool=%s outcome=invalid_argument ms=%d", name, _ms(started))
        return error_envelope("invalid_argument", str(e), False)
    except Exception as e:  # noqa: BLE001 - tools must never raise
        logger.warning("tool=%s outcome=unexpected error=%s ms=%d", name, type(e).__name__, _ms(started))
        return error_envelope("upstream_error", f"Unexpected error: {type(e).__name__}", True)
    logger.info("tool=%s outcome=ok ms=%d", name, _ms(started))
    if result is None:
        result = {"result": None, "note": "Spanning returned an empty response."}
    return result if isinstance(result, str) else dump_json_capped(result)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
