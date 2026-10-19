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
"""Account status and licenses, plus the operations Spanning's API does not offer."""

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import SpanningClient
from ._common import READ_ONLY, ClientFactory, UserId, compact, run_tool

WEB_APP_NOTE = "Use the Spanning Backup web app (Restore / Audit Log pages) for this."


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def spanning_status() -> str:
        """Check the Spanning connection: credentials work, region, platform and tenant state."""

        async def action(client: SpanningClient) -> dict:
            tenant = await client.get("tenant") or {}
            return compact(
                {
                    "connected": True,
                    "platform": client.platform,
                    "region": client.region,
                    "admin_email": client.admin_email,
                    "tenant_status": tenant.get("status") if isinstance(tenant, dict) else None,
                }
            )

        return await run_tool("spanning_status", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def spanning_get_license_usage() -> str:
        """Get license usage: purchased standard / archive licenses vs assigned users and shared mailboxes."""

        async def action(client: SpanningClient) -> Any:
            tenant = await client.get("tenant")
            if not isinstance(tenant, dict):
                return tenant
            licenses = tenant.get("licenses")
            assigned = tenant.get("assignedStandard", tenant.get("assigned"))
            available = licenses - assigned if isinstance(licenses, int) and isinstance(assigned, int) else None
            return {**tenant, "available_standard_licenses": available}

        return await run_tool("spanning_get_license_usage", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def spanning_queue_restore(
        userId: UserId,
        service: Annotated[str, Field(description="Service to restore (mail, drive, ...).", min_length=1)],
        items: Annotated[list[str], Field(description="Item ids to restore; empty for the whole service.")],
    ) -> str:
        """Explain how to restore: Spanning's API cannot start a restore, so nothing is queued here.

        Returns where to do it instead. Never tell the user a restore was started.
        """

        async def action(client: SpanningClient) -> dict:
            return {
                "queued": False,
                "user": userId,
                "service": service,
                "items": len(items),
                "message": "Spanning's public API has no restore operation. " + WEB_APP_NOTE,
            }

        return await run_tool("spanning_queue_restore", client_factory, action, needs_api=False)

    @mcp.tool(annotations=READ_ONLY)
    async def spanning_get_restore_status(
        restoreId: Annotated[str, Field(description="Restore job identifier.", min_length=1)],
    ) -> str:
        """Explain how to check a restore: Spanning's API does not expose restore jobs."""

        async def action(client: SpanningClient) -> dict:
            return {
                "restore_id": restoreId,
                "available": False,
                "message": "Spanning's public API does not expose restore jobs. " + WEB_APP_NOTE,
            }

        return await run_tool("spanning_get_restore_status", client_factory, action, needs_api=False)

    @mcp.tool(annotations=READ_ONLY)
    async def spanning_list_audit_log(
        since: Annotated[str | None, Field(description="ISO 8601 start (unused: see description).")] = None,
        until: Annotated[str | None, Field(description="ISO 8601 end (unused: see description).")] = None,
    ) -> str:
        """Explain how to read the admin audit log: Spanning's API does not expose it.

        For backup activity use spanning_list_backups or spanning_list_users with includeBackupSummary.
        """

        async def action(client: SpanningClient) -> dict:
            return {
                "available": False,
                "message": "Spanning's public API does not expose the admin audit log. " + WEB_APP_NOTE,
            }

        return await run_tool("spanning_list_audit_log", client_factory, action, needs_api=False)
