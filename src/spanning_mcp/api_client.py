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
"""Async client for the Spanning Backup for Microsoft 365 API."""

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from ._json import error_envelope
from .config import API_URL_TEMPLATE, PROBE_PATH, REGIONS

logger = logging.getLogger("spanning_mcp.api")

_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
_PROBE_TIMEOUT = httpx.Timeout(10.0)
_MAX_RETRIES = 2
_MAX_BACKOFF_SECONDS = 20.0
# Whole-call budget including retries; the gateway times a tool call out at 120s.
_CALL_BUDGET_SECONDS = 100.0

# One connection pool for the process. It never holds credentials: the
# Authorization header is built per request from the caller's own key, so
# sharing the pool across tenants is safe.
_shared_http_client: httpx.AsyncClient | None = None


def _get_shared_http_client() -> httpx.AsyncClient:
    global _shared_http_client
    if _shared_http_client is None:
        _shared_http_client = httpx.AsyncClient(timeout=_TIMEOUT)
    return _shared_http_client


# Which region an account answers in (sha256 of email + token -> region code).
# Holds no credential and nothing that can be turned back into one; it only
# saves re-probing every region on each request.
_region_cache: "OrderedDict[str, tuple[str, float]]" = OrderedDict()
_REGION_CACHE_SIZE = 500
# Re-probe after a day, in case the account was moved to another region.
_REGION_CACHE_TTL_SECONDS = 24 * 3600


def clear_region_cache() -> None:
    _region_cache.clear()


# status_code -> (error code, retryable). 0 means no HTTP response at all
# (connection error or timeout).
_STATUS_TO_CODE: dict[int, tuple[str, bool]] = {
    0: ("upstream_error", True),
    400: ("invalid_argument", False),
    401: ("unauthorized", False),
    403: ("unauthorized", False),
    404: ("not_found", False),
    422: ("invalid_argument", False),
    429: ("rate_limited", True),
}


def classify_status(status_code: int) -> tuple[str, bool]:
    if status_code in _STATUS_TO_CODE:
        return _STATUS_TO_CODE[status_code]
    if status_code >= 500:
        return "upstream_error", True
    if status_code >= 400:
        return "invalid_argument", False
    return "upstream_error", True


class SpanningError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Spanning API error {status_code}: {message}")

    def to_envelope(self) -> str:
        code, retryable = classify_status(self.status_code)
        return error_envelope(code, self.message, retryable)


@dataclass(frozen=True)
class SpanningCredentials:
    admin_email: str
    api_token: str
    platform: str = "m365"
    region: str | None = None

    def __repr__(self) -> str:  # never leak the token into logs or tracebacks
        return (
            f"SpanningCredentials(admin_email={self.admin_email!r}, platform={self.platform!r}, "
            f"region={self.region!r}, api_token=<redacted>)"
        )


class SpanningClient:
    """Spanning API wrapper for one caller's admin email + API token (HTTP Basic auth)."""

    def __init__(self, credentials: SpanningCredentials, http_client: httpx.AsyncClient | None = None):
        self._credentials = credentials
        self._http = http_client
        self.region = credentials.region

    @property
    def platform(self) -> str:
        return self._credentials.platform

    @property
    def admin_email(self) -> str:
        return self._credentials.admin_email

    def _client(self) -> httpx.AsyncClient:
        return self._http or _get_shared_http_client()

    def _auth(self) -> httpx.BasicAuth:
        return httpx.BasicAuth(self._credentials.admin_email, self._credentials.api_token)

    def _cache_key(self) -> str:
        raw = f"{self._credentials.admin_email.lower()}\n{self._credentials.api_token}"
        return hashlib.sha256(raw.encode()).hexdigest()

    async def resolve_region(self) -> str:
        if self.region:
            return self.region
        key = self._cache_key()
        cached = _region_cache.get(key)
        if cached and cached[1] > time.monotonic():
            self.region = cached[0]
            return cached[0]

        async def probe(region: str) -> str | None:
            url = f"{API_URL_TEMPLATE.format(region=region)}/{PROBE_PATH}"
            try:
                resp = await self._client().get(
                    url, auth=self._auth(), headers={"Accept": "application/json"}, timeout=_PROBE_TIMEOUT
                )
            except httpx.RequestError:
                return None
            return region if resp.is_success else None

        results = await asyncio.gather(*(probe(r) for r in REGIONS))
        found = next((r for r in results if r), None)
        if not found:
            raise SpanningError(
                401,
                "No Spanning region accepted this admin email and API token. Check both (the token must belong to "
                "that admin), or send X-Spanning-Region (us, eu, uk, ca, ap, af).",
            )
        _region_cache[key] = (found, time.monotonic() + _REGION_CACHE_TTL_SECONDS)
        _region_cache.move_to_end(key)
        while len(_region_cache) > _REGION_CACHE_SIZE:
            _region_cache.popitem(last=False)
        self.region = found
        logger.info("spanning_api detected region=%s", found)
        return found

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        region = await self.resolve_region()
        url = f"{API_URL_TEMPLATE.format(region=region)}/{path.strip('/')}"
        query = {k: _param_value(v) for k, v in (params or {}).items() if v is not None}
        started = time.monotonic()
        attempt = 0
        while True:
            t0 = time.monotonic()
            resp: httpx.Response | None = None
            error: SpanningError | None = None
            try:
                resp = await self._client().get(url, params=query, auth=self._auth(), headers={"Accept": "application/json"})
                status = resp.status_code
            except httpx.TimeoutException:
                status, error = 0, SpanningError(0, f"Timed out calling Spanning (GET {path})")
            except httpx.RequestError as e:
                status = 0
                error = SpanningError(0, f"Network error calling Spanning (GET {path}): {type(e).__name__}")

            logger.info(
                "spanning_api method=GET path=%s status=%s ms=%d attempt=%d",
                path, status, (time.monotonic() - t0) * 1000, attempt,
            )

            # This server only reads, so every failure that may be transient is retried.
            retryable = status in (0, 429) or status >= 500
            if retryable and attempt < _MAX_RETRIES:
                delay = _retry_delay(resp, attempt)
                if time.monotonic() - started + delay < _CALL_BUDGET_SECONDS:
                    attempt += 1
                    await asyncio.sleep(delay)
                    continue
            if error is not None:
                raise error
            assert resp is not None
            if resp.status_code >= 400:
                raise SpanningError(resp.status_code, _error_message(resp, path))
            return _parse_body(resp)


def user_path(user_principal_name: str) -> str:
    return f"users/{quote(user_principal_name.strip(), safe='@')}"


def _param_value(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _retry_delay(resp: httpx.Response | None, attempt: int) -> float:
    if resp is not None:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), _MAX_BACKOFF_SECONDS))
            except ValueError:
                pass
    return min(float(2**attempt), _MAX_BACKOFF_SECONDS)


def _parse_body(resp: httpx.Response) -> Any:
    if not resp.content or not resp.content.strip():
        return None
    try:
        return resp.json()
    except ValueError:
        return {"raw_response": resp.text[:2000]}


def _error_message(resp: httpx.Response, path: str) -> str:
    fallback = f"Spanning returned HTTP {resp.status_code} for {path}"
    try:
        payload = resp.json()
    except ValueError:
        text = resp.text.strip()
        return f"{fallback}: {text[:300]}" if text else fallback
    if isinstance(payload, str) and payload:
        return f"{fallback}: {payload[:300]}"
    if isinstance(payload, dict):
        for key in ("message", "error", "errorMessage", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return f"{fallback}: {value[:300]}"
    return fallback
