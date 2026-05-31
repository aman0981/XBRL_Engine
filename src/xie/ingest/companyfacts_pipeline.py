"""Mode A pipeline: SEC companyfacts JSON -> stub filings, facts, canonical_line_items."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from xie.core.logging import get_logger
from xie.db.models import Context, Fact, Filing, Unit
from xie.regulators.sec.client import SECClient
from xie.regulators.sec.companyfacts import (
    CompanyFact,
    CompanyFactsClient,
    CompanyFactsPayload,
)
from xie.xbrl.canonical_project import project_canonical_for_entity

log = get_logger("xie.ingest.companyfacts")

EMPTY_DIMS_HASH = hashlib.sha256(b"{}").hexdigest()
UNIT_USD_PER_SHARES = ("iso4217:USD", "xbrli:shares")


@dataclass(slots=True)
class CompanyFactsIngestResult:
    cik10: str
    entity_name: str
    filings_upserted: int
    facts_upserted: int
    canonical_rows_upserted: int


def _unit_signature(unit: str) -> tuple[list[str], list[str] | None, str]:
    """Map SEC companyfacts unit string -> (numerator, denominator, unit_ref)."""
    if "/" in unit:
        num, den = unit.split("/", 1)
        return (
            [_xbrl_unit(num)],
            [_xbrl_unit(den)],
            f"{num}-per-{den}",
        )
    return ([_xbrl_unit(unit)], None, unit)


def _xbrl_unit(token: str) -> str:
    if token in ("USD", "EUR", "GBP", "JPY", "CNY", "INR", "CHF", "AUD", "CAD", "HKD"):
        return f"iso4217:{token}"
    return f"xbrli:{token}"


def _context_ref(fact: CompanyFact) -> str:
    if fact.period_instant is not None:
        return f"cf:{fact.period_instant.isoformat()}"
    return f"cf:{fact.period_start.isoformat() if fact.period_start else 'na'}_{fact.period_end.isoformat() if fact.period_end else 'na'}"


def _stub_filing_values(cik10: str, fact: CompanyFact) -> dict:
    return {
        "regulator": "SEC",
        "entity_id": cik10,
        "accession_no": fact.accession_no,
        "form_type": fact.form,
        "filing_date": fact.filed,
        "period_of_report": fact.period_end or fact.period_instant,
        "fiscal_year": fact.fiscal_year,
        "fiscal_period": fact.fiscal_period,
        "ingestion_mode": "companyfacts",
        "ingestion_status": "mode_a_ingested",
        "ingested_at": datetime.now(timezone.utc),
    }


class CompanyFactsPipeline:
    """End-to-end Mode A: fetch companyfacts -> upsert filings/contexts/units/facts -> canonical."""

    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        client: SECClient | None = None,
    ) -> None:
        self._session_maker = session_maker
        self._owns_client = client is None
        self._client = client or SECClient()
        self._cf_client = CompanyFactsClient(self._client)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch_by_cik(self, cik10: str) -> CompanyFactsIngestResult:
        payload = await self._cf_client.fetch(cik10)
        return await self._ingest(payload)

    async def _ingest(self, payload: CompanyFactsPayload) -> CompanyFactsIngestResult:
        cik10 = payload.cik10
        filings_upserted = 0
        facts_upserted = 0

        # Group facts by accession_no so we materialize one filing at a time.
        by_accession: dict[str, list[CompanyFact]] = {}
        for fact in payload.facts:
            if not fact.accession_no:
                continue
            by_accession.setdefault(fact.accession_no, []).append(fact)

        async with self._session_maker() as session:
            filing_ids = await self._upsert_filings(session, cik10, by_accession)
            filings_upserted = len(filing_ids)
            for accession, group in by_accession.items():
                filing_id = filing_ids[accession]
                contexts_by_ref = await self._upsert_contexts(session, filing_id, group)
                units_by_ref = await self._upsert_units(session, filing_id, group)
                inserted = await self._upsert_facts(
                    session, filing_id, group, contexts_by_ref, units_by_ref
                )
                facts_upserted += inserted
            await session.commit()

        canonical_count = await self._compute_canonical(cik10)
        log.info(
            "companyfacts_ingest_done",
            cik=cik10,
            filings=filings_upserted,
            facts=facts_upserted,
            canonical=canonical_count,
        )
        return CompanyFactsIngestResult(
            cik10=cik10,
            entity_name=payload.entity_name,
            filings_upserted=filings_upserted,
            facts_upserted=facts_upserted,
            canonical_rows_upserted=canonical_count,
        )

    async def _upsert_filings(
        self,
        session: AsyncSession,
        cik10: str,
        by_accession: dict[str, list[CompanyFact]],
    ) -> dict[str, int]:
        ids: dict[str, int] = {}
        for accession, group in by_accession.items():
            sample = group[0]
            values = _stub_filing_values(cik10, sample)
            # why: ON CONFLICT DO UPDATE always returns id; DO NOTHING returns nothing if conflict,
            # forcing a follow-up SELECT. Touching ingestion_status keeps the row monotonically updated.
            stmt = (
                pg_insert(Filing)
                .values(**values)
                .on_conflict_do_update(
                    constraint="uq_filings_regulator_accession",
                    set_={
                        "ingestion_mode": "companyfacts"
                        if values["ingestion_mode"] == "companyfacts"
                        else Filing.ingestion_mode,
                        "fiscal_year": values["fiscal_year"],
                        "fiscal_period": values["fiscal_period"],
                        "form_type": values["form_type"],
                        "filing_date": values["filing_date"],
                        "period_of_report": values["period_of_report"],
                    },
                )
                .returning(Filing.id)
            )
            row_id = (await session.execute(stmt)).scalar_one()
            ids[accession] = row_id
        return ids

    async def _upsert_contexts(
        self,
        session: AsyncSession,
        filing_id: int,
        facts: Iterable[CompanyFact],
    ) -> dict[str, int]:
        unique: dict[str, dict] = {}
        for fact in facts:
            ref = _context_ref(fact)
            if ref in unique:
                continue
            unique[ref] = {
                "filing_id": filing_id,
                "context_ref": ref,
                "entity_id": None,
                "period_start": fact.period_start,
                "period_end": fact.period_end,
                "period_instant": fact.period_instant,
                "dimensions": {},
                "dimensions_hash": EMPTY_DIMS_HASH,
            }
        if not unique:
            return {}
        stmt = (
            pg_insert(Context)
            .values(list(unique.values()))
            .on_conflict_do_update(
                constraint="uq_contexts_filing_ref",
                set_={"period_end": pg_insert(Context).excluded.period_end},
            )
            .returning(Context.id, Context.context_ref)
        )
        rows = (await session.execute(stmt)).all()
        return {ctx_ref: ctx_id for (ctx_id, ctx_ref) in rows}

    async def _upsert_units(
        self,
        session: AsyncSession,
        filing_id: int,
        facts: Iterable[CompanyFact],
    ) -> dict[str, int]:
        unique: dict[str, dict] = {}
        for fact in facts:
            num, den, ref = _unit_signature(fact.unit)
            if ref in unique:
                continue
            unique[ref] = {
                "filing_id": filing_id,
                "unit_ref": ref,
                "numerator_measures": num,
                "denominator_measures": den,
            }
        if not unique:
            return {}
        stmt = (
            pg_insert(Unit)
            .values(list(unique.values()))
            .on_conflict_do_update(
                constraint="uq_units_filing_ref",
                set_={"numerator_measures": pg_insert(Unit).excluded.numerator_measures},
            )
            .returning(Unit.id, Unit.unit_ref)
        )
        rows = (await session.execute(stmt)).all()
        return {unit_ref: unit_id for (unit_id, unit_ref) in rows}

    async def _upsert_facts(
        self,
        session: AsyncSession,
        filing_id: int,
        facts: list[CompanyFact],
        contexts_by_ref: dict[str, int],
        units_by_ref: dict[str, int],
    ) -> int:
        if not facts:
            return 0
        rows: list[dict] = []
        for fact in facts:
            ctx_ref = _context_ref(fact)
            _num, _den, unit_ref = _unit_signature(fact.unit)
            rows.append(
                {
                    "filing_id": filing_id,
                    "context_id": contexts_by_ref.get(ctx_ref),
                    "unit_id": units_by_ref.get(unit_ref),
                    "concept_qname": fact.qname,
                    "value_numeric": fact.value,
                    "value_text": None,
                    "period_start": fact.period_start,
                    "period_end": fact.period_end,
                    "period_instant": fact.period_instant,
                    "is_dimensional": False,
                    "is_hidden": False,
                    "source": "companyfacts",
                }
            )
        stmt = (
            pg_insert(Fact)
            .values(rows)
            .on_conflict_do_update(
                constraint="uq_facts_filing_concept_ctx_unit_src",
                set_={"value_numeric": pg_insert(Fact).excluded.value_numeric},
            )
        )
        result = await session.execute(stmt)
        return result.rowcount or len(rows)

    async def _compute_canonical(self, cik10: str) -> int:
        """Delegate to the shared projector. Mode A only at this stage; Mode B
        rows merge in later when modeb_pipeline runs."""
        async with self._session_maker() as session:
            return await project_canonical_for_entity(
                session, cik10, sources=("companyfacts",)
            )
