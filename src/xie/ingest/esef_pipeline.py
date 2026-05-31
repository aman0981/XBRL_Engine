"""ESEF Mode B pipeline: discover filing via filings.xbrl.org -> download package
-> unzip -> parse iXBRL -> persist with regulator='ESEF', entity_id=LEI.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from xie.core.config import settings
from xie.core.hashing import sha256_file
from xie.core.logging import get_logger
from xie.db.models import Filing
from xie.ingest.modeb_pipeline import ModeBPipeline
from xie.regulators.esef.client import (
    ESEFFilingHit,
    download_package,
    search_filings,
)
from xie.regulators.esef.taxonomy_package import UnpackedPackage, unzip_package
from xie.xbrl.canonical_project import project_canonical_for_entity

log = get_logger("xie.ingest.esef")


@dataclass(slots=True)
class ESEFIngestResult:
    lei: str
    entity_name: str | None
    accession_no: str
    filing_id: int
    primary_path: str
    facts_persisted: int | None
    canonical_rows: int


class ESEFIngestPipeline:
    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self._session_maker = session_maker
        self._modeb = ModeBPipeline(session_maker=session_maker)
        self._raw_root = settings.raw_data_dir

    async def fetch_by_lei(
        self,
        lei: str,
        limit: int = 1,
    ) -> list[ESEFIngestResult]:
        hits = await search_filings(lei=lei, limit=limit)
        if not hits:
            log.info("esef_no_filings_found", lei=lei)
            return []
        return await self._ingest_hits(hits)

    async def fetch_by_country(
        self,
        country: str,
        limit: int = 1,
    ) -> list[ESEFIngestResult]:
        hits = await search_filings(country=country, limit=limit)
        return await self._ingest_hits(hits)

    async def _ingest_hits(
        self, hits: list[ESEFFilingHit]
    ) -> list[ESEFIngestResult]:
        results: list[ESEFIngestResult] = []
        for hit in hits:
            if not hit.lei or not hit.package_url:
                log.warning("esef_skip_missing_meta", api_id=hit.api_id)
                continue
            zip_path, unpacked = await self._fetch_and_unzip(hit)
            if unpacked.primary_ixbrl is None:
                log.warning("esef_no_primary_ixbrl", api_id=hit.api_id)
                continue
            filing_id = await self._upsert_filing_stub(hit, zip_path, unpacked.primary_ixbrl)
            modeb = await self._modeb.parse_and_persist(
                accession_no=_synth_accession(hit),
                primary_path=unpacked.primary_ixbrl,
            )
            async with self._session_maker() as session:
                canonical = await project_canonical_for_entity(
                    session,
                    hit.lei,
                    sources=("source_ixbrl",),
                    regulator="ESEF",
                )
            results.append(
                ESEFIngestResult(
                    lei=hit.lei,
                    entity_name=hit.entity_name,
                    accession_no=_synth_accession(hit),
                    filing_id=filing_id,
                    primary_path=str(unpacked.primary_ixbrl),
                    facts_persisted=modeb.facts_upserted,
                    canonical_rows=canonical,
                )
            )
            log.info("esef_filing_ingested", **asdict(results[-1]))
        return results

    async def _fetch_and_unzip(
        self, hit: ESEFFilingHit
    ) -> tuple[Path, UnpackedPackage]:
        target_dir = self._raw_root / "esef" / hit.lei / hit.api_id
        target_dir.mkdir(parents=True, exist_ok=True)
        zip_path = target_dir / "package.zip"
        if not zip_path.exists():
            await download_package(hit, zip_path)
        unpacked_dir = target_dir / "unpacked"
        # unzip_package is cheap (re-extract is roughly the same as a freshness
        # check) so we always run it; the result is the manifest + path map.
        unpacked = unzip_package(zip_path, unpacked_dir)
        return zip_path, unpacked

    async def _upsert_filing_stub(
        self,
        hit: ESEFFilingHit,
        zip_path: Path,
        primary_path: Path,
    ) -> int:
        sha = sha256_file(zip_path)
        accession = _synth_accession(hit)
        values = {
            "regulator": "ESEF",
            "entity_id": hit.lei,
            "ticker": None,
            "accession_no": accession,
            "form_type": "AFR",
            "filing_date": None,
            "period_of_report": hit.period_end,
            "fiscal_year": hit.fiscal_year,
            "fiscal_period": "FY",
            "ixbrl_url": hit.inline_xbrl_url or hit.package_url,
            "raw_sha256": sha,
            "raw_path": str(primary_path),
            "ingestion_mode": "source_ixbrl",
            "ingestion_status": "raw_downloaded",
            "ingested_at": datetime.now(timezone.utc),
        }
        async with self._session_maker() as session:
            stmt = (
                pg_insert(Filing)
                .values(**values)
                .on_conflict_do_update(
                    constraint="uq_filings_regulator_accession",
                    set_={
                        "raw_sha256": values["raw_sha256"],
                        "raw_path": values["raw_path"],
                        "period_of_report": values["period_of_report"],
                        "fiscal_year": values["fiscal_year"],
                        "ingestion_status": values["ingestion_status"],
                        "ingested_at": values["ingested_at"],
                    },
                )
                .returning(Filing.id)
            )
            row_id = (await session.execute(stmt)).scalar_one()
            await session.commit()
            return row_id


def _synth_accession(hit: ESEFFilingHit) -> str:
    """ESEF has no SEC-style accession_no; synthesise a stable identifier.

    Form: 'ESEF-' + first 24 chars of api_id. accession_no column is 30 chars.
    """
    return f"ESEF-{hit.api_id}"[:30]
