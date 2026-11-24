"""Async client for the hospital system.

No retries here: the executor owns retry. This client only turns every outcome into a value or a
typed agentcore error, so the executor can tell "refused" from "try again" from "no answer".
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from agentcore import ToolFailed, ToolOutcomeUnknown, ToolRetryable

HOSPITAL_TIMEOUT_S = 6.0  # the mock's chaos latency tops out at 2s (design-log 0.3)


class HospitalClient:
    def __init__(
        self,
        base_url: str | None = None,
        timeout_s: float = HOSPITAL_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url or os.getenv("HOSPITAL_URL", "http://localhost:8001"),
            timeout=timeout_s,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def now(self) -> dict[str, Any]:
        """The hospital's clock (fixed in the mock). Never the machine's."""
        return await self.get("/now")

    async def get(self, path: str, **params: Any) -> Any:
        return await self._send("GET", path, params={k: v for k, v in params.items() if v is not None})

    async def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return await self._send("POST", path, json=body or {})

    async def _send(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = await self._http.request(method, path, **kw)
        except httpx.TimeoutException as e:
            raise ToolOutcomeUnknown(f"The hospital system did not answer in time ({type(e).__name__}).") from e
        except httpx.TransportError as e:
            raise ToolOutcomeUnknown(f"Could not reach the hospital system ({type(e).__name__}).") from e
        if r.is_success:
            return r.json()
        if r.status_code >= 500:  # 503 from the mock, and anything else broken on their side
            raise ToolRetryable(f"The hospital system is unavailable (HTTP {r.status_code}).")
        raise ToolFailed(_refusal(r))


def _refusal(r: httpx.Response) -> str:
    try:
        body = r.json()
        return f"{body['error']}: {body['message']}"
    except (ValueError, KeyError, TypeError):
        return f"The hospital system refused the request (HTTP {r.status_code})."
