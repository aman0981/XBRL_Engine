"""SEC Mode B foundation pipeline: discovery -> download -> upsert filings row.

Phase 1 stops at raw-blob persistence. Fact extraction lands in Phase 2.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from xie.core.config import settings
from xie.core.logging import get_logger
from xie.db.models import Filing
from xie.regulators.sec.client import SECClient
from xie.regulators.sec.discovery import FilingHit, SubmissionsDiscovery
from xie.regulators.sec.filing_index import FilingBundle, FilingIndexFetcher
from xie.regulators.sec.tickers import TickerResolver

log = get_logger("xie.ingest.sec")


@dataclass(slots=True)
class IngestResult:
    cik10: str
    accession_no: str
    form_type: str
    filing_date: str
    primary_path: Path
    raw_sha256: str | None
    document_count: int
    persisted_filing_id: int | None


class SECIngestPipeline:
    def __init__(
        self,
        session_maker,
        client: SECClient | None = None,
        raw_root: Path | None = None,
    ) -> None:
        self._session_maker = session_maker
        self._owns_client = client is None
        self._client = client or SECClient()
        self._raw_root = raw_root or settings.raw_data_dir
        self._tickers = TickerResolver(self._client)
        self._discovery = SubmissionsDiscovery(self._client)
        self._index = FilingIndexFetcher(self._client, self._raw_root)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch_by_ticker(
        self,
        ticker: str,
        form: str,
        limit: int,
    ) -> list[IngestResult]:
        cik10 = await self._tickers.resolve(ticker)
        return await self.fetch_by_cik(cik10, form=form, limit=limit, ticker=ticker.upper())

    async def fetch_by_cik(
        self,
        cik10: str,
        form: str,
        limit: int,
        ticker: str | None = None,
    ) -> list[IngestResult]:
        hits: list[FilingHit] = await self._discovery.list_filings(
            cik10, forms={form}, limit=limit
        )
        results: list[IngestResult] = []
        for hit in hits:
            bundle = await self._index.fetch(
                cik10=cik10,
                accession_no=hit.accession_no,
                primary_document=hit.primary_document,
            )
            filing_id = await self._upsert(hit, bundle, ticker=ticker)
            results.append(
                IngestResult(
                    cik10=cik10,
                    accession_no=hit.accession_no,
                    form_type=hit.form_type,
                    filing_date=hit.filing_date.isoformat(),
                    primary_path=bundle.primary_path,
                    raw_sha256=bundle.primary_sha256,
                    document_count=len(bundle.documents),
                    persisted_filing_id=filing_id,
                )
            )
            log.info(
                "filing_ingested",
                cik=cik10,
                accession=hit.accession_no,
                form=hit.form_type,
                docs=len(bundle.documents),
                filing_id=filing_id,
            )
        return results

    async def _upsert(
        self,
        hit: FilingHit,
        bundle: FilingBundle,
        ticker: str | None,
    ) -> int | None:
        if self._session_maker is None:
            return None
        values = {
            "regulator": "SEC",
            "entity_id": hit.cik10,
            "ticker": ticker,
            "accession_no": hit.accession_no,
            "form_type": hit.form_type,
            "filing_date": hit.filing_date,
            "period_of_report": hit.period_of_report,
            "ixbrl_url": hit.primary_document_url(),
            "raw_sha256": bundle.primary_sha256,
            "raw_path": str(bundle.primary_path),
            "ingestion_mode": "source_ixbrl",
            "ingestion_status": "raw_downloaded",
            "ingested_at": datetime.now(timezone.utc),
        }
        async with self._session_maker() as session:  # type: AsyncSession
            stmt = (
                pg_insert(Filing)
                .values(**values)
                .on_conflict_do_update(
                    constraint="uq_filings_regulator_accession",
                    set_={
                        "raw_sha256": values["raw_sha256"],
                        "raw_path": values["raw_path"],
                        "ingestion_status": values["ingestion_status"],
                        "ingested_at": values["ingested_at"],
                    },
                )
                .returning(Filing.id)
            )
            result = await session.execute(stmt)
            row = result.scalar_one()
            await session.commit()
            return row
