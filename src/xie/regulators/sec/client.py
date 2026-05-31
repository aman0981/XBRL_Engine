"""SEC EDGAR HTTP client with mandatory User-Agent, 10 req/s shared rate limit, retries."""
from __future__ import annotations

import asyncio

import httpx
from aiolimiter import AsyncLimiter
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from xie.core.config import settings
from xie.core.logging import get_logger

log = get_logger("xie.sec.client")

SEC_RATE_LIMIT_PER_SEC = 10
RETRY_MAX_ATTEMPTS = 4
RETRY_WAIT_MIN_SEC = 1
RETRY_WAIT_MAX_SEC = 10

# why: SEC's fair-access policy bans missing/invalid UA; a single AsyncLimiter shared
# across instances enforces the 10 req/s limit that's per-IP, not per-host.
_shared_limiter = AsyncLimiter(SEC_RATE_LIMIT_PER_SEC, 1)


class SECForbiddenError(RuntimeError):
    """SEC returned 403 - typically bad UA or IP ban. Do not retry."""


class SECClient:
    """Async HTTP client for SEC data.sec.gov + sec.gov hosts."""

    def __init__(
        self,
        user_agent: str | None = None,
        client: httpx.AsyncClient | None = None,
        limiter: AsyncLimiter | None = None,
    ) -> None:
        self.user_agent = user_agent or settings.sec_user_agent
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        )
        self._limiter = limiter or _shared_limiter
        # why: SEC mandates UA per request; setting it per-call keeps SECClient as the
        # single source of truth regardless of how the underlying httpx client was built.
        self._headers = {
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip, deflate",
        }

    async def __aenter__(self) -> SECClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get(self, url: str) -> httpx.Response:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
            wait=wait_exponential(multiplier=1, min=RETRY_WAIT_MIN_SEC, max=RETRY_WAIT_MAX_SEC),
            retry=retry_if_exception_type((httpx.TransportError, _Retryable)),
            reraise=True,
        ):
            with attempt:
                async with self._limiter:
                    response = await self._client.get(url, headers=self._headers)
                if response.status_code == 403:
                    log.error("sec_forbidden", url=url, ua=self.user_agent)
                    raise SECForbiddenError(f"403 from {url} - check User-Agent")
                if response.status_code in (429, 503):
                    log.warning("sec_throttled", url=url, status=response.status_code)
                    raise _Retryable(f"{response.status_code} from {url}")
                response.raise_for_status()
                return response
        raise RuntimeError("unreachable")  # AsyncRetrying always raises on exhaust

    async def get_json(self, url: str) -> dict:
        response = await self.get(url)
        return response.json()

    async def get_bytes(self, url: str) -> bytes:
        response = await self.get(url)
        return response.content


class _Retryable(Exception):
    """Internal marker: response status warrants a retry."""


__all__ = ["SECClient", "SECForbiddenError"]
