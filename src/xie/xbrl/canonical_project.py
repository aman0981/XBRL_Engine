"""Project canonical_line_items from facts (Mode A and/or Mode B).

Phase 1.5 had this logic inline in `companyfacts_pipeline.py`. Phase 4 lifts it
into a shared module so the same projection runs after Mode B ingestion too,
and adds calc-linkbase awareness: when a concept is a root of a calculation
arc in the filing's DTS, the mapper records `mapping_method='calc_linkbase_root'`
with a slightly higher confidence.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from xie.core.logging import get_logger
from xie.db.models import CanonicalLineItem, ConceptArc, Fact, Filing, Unit
from xie.xbrl.canonical_map import REGULATOR_FALLBACKS, SEC_FALLBACKS, Canonical

log = get_logger("xie.xbrl.canonical_project")


CALC_ROOT_BOOST = Decimal("0.005")  # bump confidence when calc-linkbase confirms


async def project_canonical_for_entity(
    session: AsyncSession,
    entity_id: str,
    sources: tuple[str, ...] = ("companyfacts", "source_ixbrl"),
    regulator: str = "SEC",
) -> int:
    chains = REGULATOR_FALLBACKS.get(regulator, SEC_FALLBACKS)
    # why: the uq_canonical_unique_period constraint uses NULL period_end and
    # period_instant. PostgreSQL treats NULL ≠ NULL, so the same natural key
    # with NULL period would duplicate on each re-run. Snapshot-rebuild instead.
    await session.execute(
        CanonicalLineItem.__table__.delete().where(
            CanonicalLineItem.regulator == regulator,
            CanonicalLineItem.entity_id == entity_id,
        )
    )
    rows = 0
    calc_roots = await _calc_root_qnames(session)
    for canonical, chain in chains.items():
        inserted = await _project_one(
            session, entity_id, canonical, chain, calc_roots, sources, regulator
        )
        rows += inserted
    await session.commit()
    log.info(
        "canonical_projection_done",
        regulator=regulator,
        entity_id=entity_id,
        rows=rows,
        sources=sources,
    )
    return rows


async def _calc_root_qnames(session: AsyncSession) -> set[str]:
    """Concepts that are 'from' (parent / total) of any calc-linkbase arc."""
    stmt = (
        select(ConceptArc.from_concept_id)
        .where(ConceptArc.linkbase_type == "calculation")
        .distinct()
    )
    rows = (await session.execute(stmt)).scalars().all()
    if not rows:
        return set()
    # Join back to concepts to read qname.
    from xie.db.models import Concept

    qstmt = select(Concept.qname).where(Concept.id.in_(rows))
    return {q for (q,) in (await session.execute(qstmt)).all()}


async def _project_one(
    session: AsyncSession,
    entity_id: str,
    canonical: Canonical,
    chain: list,
    calc_roots: set[str],
    sources: tuple[str, ...],
    regulator: str,
) -> int:
    qname_to_entry = {e.qname: e for e in chain}
    rank_by_qname = {e.qname: i for i, e in enumerate(chain)}
    unit_filter = _unit_filter(canonical, regulator)

    stmt = (
        select(
            Fact.concept_qname,
            Fact.value_numeric,
            Fact.period_start,
            Fact.period_end,
            Fact.period_instant,
            Fact.id,
            Fact.unit_id,
            Fact.source,
            Filing.fiscal_year,
            Filing.fiscal_period,
            Filing.id.label("filing_id"),
            Unit.unit_ref,
        )
        .join(Filing, Filing.id == Fact.filing_id)
        .outerjoin(Unit, Unit.id == Fact.unit_id)
        .where(
            Filing.regulator == regulator,
            Filing.entity_id == entity_id,
            Fact.source.in_(sources),
            Fact.concept_qname.in_(list(qname_to_entry.keys())),
            Fact.is_dimensional.is_(False),
        )
    )
    result = (await session.execute(stmt)).mappings().all()

    # Resolve each (period, fiscal_period) bucket: pick best-ranked qname AND
    # prefer source='source_ixbrl' over 'companyfacts' on ties (more accurate).
    best: dict[tuple, dict] = {}
    for row in result:
        if not unit_filter(row["unit_ref"]):
            continue
        period_key = (row["period_end"], row["period_instant"], row["fiscal_period"])
        rank = rank_by_qname[row["concept_qname"]]
        # source_ixbrl ranks lower (better) by sort order
        src_rank = 0 if row["source"] == "source_ixbrl" else 1
        score = (rank, src_rank)
        current = best.get(period_key)
        if current is None or score < current["_score"]:
            best[period_key] = {**row, "_score": score, "_rank": rank}

    if not best:
        return 0

    upsert_rows: list[dict] = []
    for _key, row in best.items():
        entry = qname_to_entry[row["concept_qname"]]
        confidence = Decimal(str(entry.confidence))
        method = entry.method
        if row["concept_qname"] in calc_roots:
            method = "calc_linkbase_root"
            confidence = min(Decimal("1.000"), confidence + CALC_ROOT_BOOST)
        upsert_rows.append(
            {
                "regulator": regulator,
                "entity_id": entity_id,
                "filing_id": row["filing_id"],
                "canonical_name": canonical.value,
                "period_start": row["period_start"],
                "period_end": row["period_end"],
                "period_instant": row["period_instant"],
                "fiscal_period": row["fiscal_period"],
                "fiscal_year": row["fiscal_year"],
                "value": row["value_numeric"],
                "unit_id": row["unit_id"],
                "source_concept_qname": row["concept_qname"],
                "source_fact_id": row["id"],
                "mapping_confidence": confidence,
                "mapping_method": method,
            }
        )

    stmt_up = (
        pg_insert(CanonicalLineItem)
        .values(upsert_rows)
        .on_conflict_do_update(
            constraint="uq_canonical_unique_period",
            set_={
                "value": pg_insert(CanonicalLineItem).excluded.value,
                "source_concept_qname": pg_insert(
                    CanonicalLineItem
                ).excluded.source_concept_qname,
                "source_fact_id": pg_insert(CanonicalLineItem).excluded.source_fact_id,
                "mapping_confidence": pg_insert(
                    CanonicalLineItem
                ).excluded.mapping_confidence,
                "mapping_method": pg_insert(CanonicalLineItem).excluded.mapping_method,
                "filing_id": pg_insert(CanonicalLineItem).excluded.filing_id,
                "fiscal_year": pg_insert(CanonicalLineItem).excluded.fiscal_year,
                "unit_id": pg_insert(CanonicalLineItem).excluded.unit_id,
            },
        )
    )
    result_up = await session.execute(stmt_up)
    return result_up.rowcount or len(upsert_rows)


def _unit_filter(canonical: Canonical, regulator: str = "SEC"):
    if canonical is Canonical.EPS:
        return lambda ref: ref is not None and "per" in ref
    # SEC filers expose money in USD-ish units; ESEF filers in EUR/GBP/etc.
    # The "no 'per' in unit" heuristic excludes per-share units uniformly.
    return lambda ref: ref is not None and "per" not in ref
