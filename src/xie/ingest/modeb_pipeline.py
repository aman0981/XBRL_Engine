"""Mode B pipeline: parse a downloaded primary iXBRL file -> persist contexts/units/facts.

Idempotent on (filing_id, concept_qname, context_id, unit_id, source).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from xie.core.logging import get_logger
from xie.db.models import Context, Fact, FactDimension, Filing, Unit
from xie.xbrl.canonical_project import project_canonical_for_entity
from xie.xbrl.ixbrl_parser import ParsedFact, ParsedFiling, parse_ixbrl

log = get_logger("xie.ingest.modeb")


@dataclass(slots=True)
class ModeBResult:
    filing_id: int
    accession_no: str
    contexts_upserted: int
    units_upserted: int
    facts_upserted: int
    fact_dimensions_upserted: int
    canonical_rows_upserted: int = 0


class ModeBPipeline:
    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self._session_maker = session_maker

    async def parse_and_persist(
        self, accession_no: str, primary_path: Path
    ) -> ModeBResult:
        parsed = parse_ixbrl(primary_path)
        async with self._session_maker() as session:
            filing_id = await self._lookup_filing(session, accession_no)
            if filing_id is None:
                raise RuntimeError(
                    f"filings row missing for {accession_no}; run Mode A or 'fetch --persist' first"
                )
            await self._mark_parsing(session, filing_id)
            ctx_ids = await self._upsert_contexts(session, filing_id, parsed)
            unit_ids = await self._upsert_units(session, filing_id, parsed)
            facts_inserted, fact_id_by_key = await self._upsert_facts(
                session, filing_id, parsed, ctx_ids, unit_ids
            )
            dims_inserted = await self._upsert_fact_dimensions(
                session, parsed, ctx_ids, fact_id_by_key
            )
            await self._mark_parsed(session, filing_id)
            filing_row = (
                await session.execute(
                    Filing.__table__.select().where(Filing.id == filing_id)
                )
            ).first()
            await session.commit()

        canonical_rows = 0
        if filing_row is not None:
            entity_str = filing_row.entity_id  # type: ignore[attr-defined]
            regulator = filing_row.regulator  # type: ignore[attr-defined]
            # ESEF filings don't have companyfacts; SEC may have both.
            sources = (
                ("companyfacts", "source_ixbrl")
                if regulator == "SEC"
                else ("source_ixbrl",)
            )
            async with self._session_maker() as session:
                canonical_rows = await project_canonical_for_entity(
                    session,
                    entity_str,
                    sources=sources,
                    regulator=regulator,
                )

        result = ModeBResult(
            filing_id=filing_id,
            accession_no=accession_no,
            contexts_upserted=len(ctx_ids),
            units_upserted=len(unit_ids),
            facts_upserted=facts_inserted,
            fact_dimensions_upserted=dims_inserted,
            canonical_rows_upserted=canonical_rows,
        )
        log.info(
            "modeb_persist_done",
            **{k: v for k, v in asdict(result).items() if k != "accession_no"},
            accession=accession_no,
        )
        return result

    async def _lookup_filing(
        self, session: AsyncSession, accession_no: str
    ) -> int | None:
        # why: SEC accession_no format (\d{10}-\d{2}-\d{6}) and ESEF synthetic
        # ('ESEF-<api_id>') don't overlap today, so matching by accession_no
        # alone is safe for both regulators. limit(1) is defensive in case a
        # future regulator (e.g. ESRS) ever uses an overlapping format — the
        # UNIQUE constraint is on (regulator, accession_no), not accession_no.
        stmt = select(Filing.id).where(Filing.accession_no == accession_no).limit(1)
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _mark_parsing(self, session: AsyncSession, filing_id: int) -> None:
        await session.execute(
            Filing.__table__.update()
            .where(Filing.id == filing_id)
            .values(
                ingestion_mode="both",
                ingestion_status="modeb_parsing",
                ingested_at=datetime.now(timezone.utc),
            )
        )

    async def _mark_parsed(self, session: AsyncSession, filing_id: int) -> None:
        await session.execute(
            Filing.__table__.update()
            .where(Filing.id == filing_id)
            .values(ingestion_status="modeb_parsed")
        )

    async def _upsert_contexts(
        self, session: AsyncSession, filing_id: int, parsed: ParsedFiling
    ) -> dict[str, int]:
        if not parsed.contexts:
            return {}
        rows = [
            {
                "filing_id": filing_id,
                "context_ref": c.context_ref,
                "entity_id": c.entity_id,
                "period_start": c.period_start,
                "period_end": c.period_end,
                "period_instant": c.period_instant,
                "dimensions": c.dimensions_jsonb,
                "dimensions_hash": c.dimensions_hash,
            }
            for c in parsed.contexts
        ]
        stmt = (
            pg_insert(Context)
            .values(rows)
            .on_conflict_do_update(
                constraint="uq_contexts_filing_ref",
                set_={
                    "dimensions": pg_insert(Context).excluded.dimensions,
                    "dimensions_hash": pg_insert(Context).excluded.dimensions_hash,
                    "period_start": pg_insert(Context).excluded.period_start,
                    "period_end": pg_insert(Context).excluded.period_end,
                    "period_instant": pg_insert(Context).excluded.period_instant,
                },
            )
            .returning(Context.id, Context.context_ref)
        )
        result = (await session.execute(stmt)).all()
        return {ref: ctx_id for (ctx_id, ref) in result}

    async def _upsert_units(
        self, session: AsyncSession, filing_id: int, parsed: ParsedFiling
    ) -> dict[str, int]:
        if not parsed.units:
            return {}
        rows = [
            {
                "filing_id": filing_id,
                "unit_ref": u.unit_ref,
                "numerator_measures": u.numerator_measures,
                "denominator_measures": u.denominator_measures,
            }
            for u in parsed.units
        ]
        stmt = (
            pg_insert(Unit)
            .values(rows)
            .on_conflict_do_update(
                constraint="uq_units_filing_ref",
                set_={
                    "numerator_measures": pg_insert(Unit).excluded.numerator_measures,
                    "denominator_measures": pg_insert(Unit).excluded.denominator_measures,
                },
            )
            .returning(Unit.id, Unit.unit_ref)
        )
        result = (await session.execute(stmt)).all()
        return {ref: uid for (uid, ref) in result}

    async def _upsert_facts(
        self,
        session: AsyncSession,
        filing_id: int,
        parsed: ParsedFiling,
        ctx_ids: dict[str, int],
        unit_ids: dict[str, int],
    ) -> tuple[int, dict[tuple, int]]:
        if not parsed.facts:
            return 0, {}
        # De-dup by unique-key: same iXBRL doc can carry duplicate fact tags;
        # the table's UNIQUE constraint forces last-write-wins anyway.
        # why: denormalize the period fields onto the fact so cross-filing queries
        # (restatement detection, canonical projection) don't have to re-join contexts.
        contexts_by_ref = {c.context_ref: c for c in parsed.contexts}
        rows_by_key: dict[tuple, dict] = {}
        for f in parsed.facts:
            ctx_id = ctx_ids.get(f.context_ref)
            unit_id = unit_ids.get(f.unit_ref) if f.unit_ref else None
            ctx = contexts_by_ref.get(f.context_ref)
            key = (f.concept_qname, ctx_id, unit_id)
            rows_by_key[key] = _fact_to_row(filing_id, f, ctx_id, unit_id, ctx)
        rows = list(rows_by_key.values())

        stmt = (
            pg_insert(Fact)
            .values(rows)
            .on_conflict_do_update(
                constraint="uq_facts_filing_concept_ctx_unit_src",
                set_={
                    "value_numeric": pg_insert(Fact).excluded.value_numeric,
                    "value_text": pg_insert(Fact).excluded.value_text,
                    "decimals_text": pg_insert(Fact).excluded.decimals_text,
                    "scale": pg_insert(Fact).excluded.scale,
                    "format_qname": pg_insert(Fact).excluded.format_qname,
                    "transform_registry": pg_insert(Fact).excluded.transform_registry,
                    "period_start": pg_insert(Fact).excluded.period_start,
                    "period_end": pg_insert(Fact).excluded.period_end,
                    "period_instant": pg_insert(Fact).excluded.period_instant,
                    "is_hidden": pg_insert(Fact).excluded.is_hidden,
                    "is_dimensional": pg_insert(Fact).excluded.is_dimensional,
                    "xml_lang": pg_insert(Fact).excluded.xml_lang,
                    "footnote_text": pg_insert(Fact).excluded.footnote_text,
                },
            )
            .returning(Fact.id, Fact.concept_qname, Fact.context_id, Fact.unit_id)
        )
        result = (await session.execute(stmt)).all()
        fact_id_by_key: dict[tuple, int] = {
            (qname, ctx_id, unit_id): fact_id
            for (fact_id, qname, ctx_id, unit_id) in result
        }
        return len(result), fact_id_by_key

    async def _upsert_fact_dimensions(
        self,
        session: AsyncSession,
        parsed: ParsedFiling,
        ctx_ids: dict[str, int],
        fact_id_by_key: dict[tuple, int],
    ) -> int:
        contexts_by_ref = {c.context_ref: c for c in parsed.contexts}
        rows: list[dict] = []
        for f in parsed.facts:
            if not f.is_dimensional:
                continue
            ctx = contexts_by_ref.get(f.context_ref)
            if not ctx or not ctx.dimensions:
                continue
            ctx_id = ctx_ids.get(f.context_ref)
            # unit_id is part of the fact unique key; we don't store it per dim,
            # so any (qname, ctx_id, *) hit is fine. Prefer exact match if available.
            fact_id = None
            for (qname, cid, uid), fid in fact_id_by_key.items():
                if qname == f.concept_qname and cid == ctx_id:
                    fact_id = fid
                    break
            if fact_id is None:
                continue
            for d in ctx.dimensions:
                rows.append(
                    {
                        "fact_id": fact_id,
                        "axis_qname": d.axis_qname,
                        "member_qname": d.member_qname,
                        "typed_value": d.typed_value,
                        "is_default": False,
                    }
                )
        if not rows:
            return 0
        # No unique constraint on fact_dimensions; delete-then-insert keeps the
        # operation idempotent and avoids ballooning rows on re-runs.
        fact_ids = sorted({r["fact_id"] for r in rows})
        await session.execute(
            FactDimension.__table__.delete().where(FactDimension.fact_id.in_(fact_ids))
        )
        await session.execute(pg_insert(FactDimension).values(rows))
        return len(rows)


def _fact_to_row(
    filing_id: int,
    f: ParsedFact,
    ctx_id: int | None,
    unit_id: int | None,
    ctx,
) -> dict:
    return {
        "filing_id": filing_id,
        "context_id": ctx_id,
        "unit_id": unit_id,
        "concept_qname": f.concept_qname,
        "value_numeric": f.value_numeric,
        "value_text": f.value_text,
        "decimals_text": f.decimals_text,
        "decimals_inf": f.decimals_inf,
        "scale": f.scale,
        "format_qname": f.format_qname,
        "transform_registry": f.transform_registry,
        "period_start": ctx.period_start if ctx else None,
        "period_end": ctx.period_end if ctx else None,
        "period_instant": ctx.period_instant if ctx else None,
        "is_dimensional": f.is_dimensional,
        "is_hidden": f.is_hidden,
        "source": "source_ixbrl",
        "xml_lang": f.xml_lang,
        "footnote_text": f.footnote_text,
    }
