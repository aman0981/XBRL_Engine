"""Ticker -> CIK10 resolver using SEC's public ticker map."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from xie.core.logging import get_logger

if TYPE_CHECKING:
    from xie.regulators.sec.client import SECClient

log = get_logger("xie.sec.tickers")

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
CIK_PAD_WIDTH = 10


class TickerResolver:
    """Loads the SEC company_tickers.json once, then resolves ticker -> CIK10 in-memory."""

    def __init__(self, client: SECClient) -> None:
        self._client = client
        self._map: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def _ensure_loaded(self) -> None:
        if self._map:
            return
        async with self._lock:
            if self._map:
                return
            log.info("ticker_map_loading", url=COMPANY_TICKERS_URL)
            payload = await self._client.get_json(COMPANY_TICKERS_URL)
            self._map = {
                str(row["ticker"]).upper(): str(row["cik_str"]).zfill(CIK_PAD_WIDTH)
                for row in payload.values()
            }
            log.info("ticker_map_loaded", count=len(self._map))

    async def resolve(self, ticker: str) -> str:
        await self._ensure_loaded()
        cik = self._map.get(ticker.upper())
        if cik is None:
            raise KeyError(f"unknown ticker: {ticker}")
        return cik
