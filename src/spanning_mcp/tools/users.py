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
"""Users, the services backed up for them, and their daily backup results."""

from typing import Annotated, Any
from urllib.parse import parse_qs, urlsplit

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import SpanningClient, user_path
from ._common import (
    READ_ONLY,
    SERVICE_NAMES,
    ClientFactory,
    UserId,
    compact,
    outcome,
    run_tool,
    service_types,
    summary_rows,
    window_ms,
)

Days = Annotated[int | None, Field(description="How many days to cover, today included (default 7, max 30).", ge=1, le=30)]


def _page_token(next_link: Any) -> str | None:
    """Spanning pages with msIdOffset + sortPropOffset; fold them into one opaque token."""
    if not isinstance(next_link, str) or not next_link:
        return None
    query = parse_qs(urlsplit(next_link).query)
    ms_id, sort_prop = query.get("msIdOffset", [None])[0], query.get("sortPropOffset", [None])[0]
    return f"{sort_prop}|{ms_id}" if ms_id and sort_prop else None


def _offsets(token: str | None) -> dict[str, str]:
    if not token:
        return {}
    sort_prop, sep, ms_id = token.rpartition("|")
    if not sep or not sort_prop or not ms_id:
        raise ValueError("pageToken must be the next_page_token from the previous spanning_list_users call.")
    return {"sortPropOffset": sort_prop, "msIdOffset": ms_id}


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def spanning_list_users(
        limit: Annotated[int | None, Field(description="Users per page (default 50, max 200; larger values are capped).", ge=1, le=1000)] = None,
        assignedOnly: Annotated[bool | None, Field(description="Only users with a Spanning backup license.")] = None,
        includeBackupSummary: Annotated[bool | None, Field(description="Add today's backup results per user (bigger output).")] = None,
        pageToken: Annotated[str | None, Field(description="next_page_token from the previous page.")] = None,
    ) -> str:
        """List the tenant's Microsoft 365 users and whether each is licensed (assigned) for backup."""

        async def action(client: SpanningClient) -> dict:
            params = {"size": min(limit or 50, 200), "status": bool(includeBackupSummary), **_offsets(pageToken)}
            payload = await client.get("users", params) or {}
            users = [u for u in payload.get("users") or [] if isinstance(u, dict)] if isinstance(payload, dict) else []
            if assignedOnly:
                users = [u for u in users if u.get("assigned") or u.get("isArchive")]
            token = _page_token(payload.get("nextLink") if isinstance(payload, dict) else None)
            return compact(
                {
                    "users": users,
                    "count": len(users),
                    "has_more": token is not None,
                    "next_page_token": token,
                    "note": "assignedOnly filters this page only." if assignedOnly and token else None,
                }
            )

        return await run_tool("spanning_list_users", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def spanning_get_user(userId: UserId) -> str:
        """Get one user's license state (assigned, archive, deleted, admin) and today's backup results."""

        async def action(client: SpanningClient) -> Any:
            user = await client.get(user_path(userId), {"status": True})
            if isinstance(user, dict):
                for row in summary_rows(user):
                    row["outcome"] = outcome(row)
            return user

        return await run_tool("spanning_get_user", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def spanning_list_services(userId: UserId) -> str:
        """List the services backed up for a user (mail, drive, calendar, ...) with their latest result."""

        async def action(client: SpanningClient) -> dict:
            start, end = window_ms(7)
            user = await client.get(user_path(userId), {"status": True, "start": start, "end": end})
            latest: dict[str, dict] = {}
            for row in sorted(summary_rows(user), key=lambda r: str(r.get("date") or "")):
                kind = str(row.get("type") or "")
                if kind:
                    latest[kind] = row
            services = [
                compact(
                    {
                        "service": SERVICE_NAMES.get(kind, kind.lower()),
                        "type": kind,
                        "last_backup_date": row.get("date"),
                        "outcome": outcome(row),
                        "backup": row.get("backup"),
                    }
                )
                for kind, row in sorted(latest.items())
            ]
            return compact(
                {
                    "user": user.get("userPrincipalName", userId) if isinstance(user, dict) else userId,
                    "assigned": user.get("assigned") if isinstance(user, dict) else None,
                    "services": services,
                    "count": len(services),
                    "note": None if services else "No backups in the last 7 days; check the user is assigned a license.",
                }
            )

        return await run_tool("spanning_list_services", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def spanning_list_backups(
        userId: UserId,
        service: Annotated[str, Field(description="mail, drive, calendar, contacts, sites or teams.", min_length=1)],
        days: Days = None,
    ) -> str:
        """List a user's daily backup results for one service, newest first (successful / partial / failed)."""

        async def action(client: SpanningClient) -> dict:
            kinds = service_types(service)
            window = days or 7
            start, end = window_ms(window)
            user = await client.get(user_path(userId), {"status": True, "start": start, "end": end})
            backups = [
                {**row, "outcome": outcome(row)}
                for row in summary_rows(user)
                if str(row.get("type") or "").upper() in kinds
            ]
            backups.sort(key=lambda r: str(r.get("date") or ""), reverse=True)
            return {"user": userId, "service": service, "days": window, "backups": backups, "count": len(backups)}

        return await run_tool("spanning_list_backups", client_factory, action)
