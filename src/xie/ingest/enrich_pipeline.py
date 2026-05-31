"""Run Arelle enrichment on a filing and persist concepts/arcs/dqc_findings."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from xie.core.logging import get_logger
from xie.db.models import Concept, ConceptArc, DQCFinding, Filing, RoleDefinition
from xie.xbrl.arelle_worker import (
    ArelleEnricher,
    EnrichedArc,
    EnrichedConcept,
    EnrichedRole,
)

log = get_logger("xie.ingest.enrich")


@dataclass(slots=True)
class EnrichResult:
    filing_id: int
    accession_no: str
    concepts_upserted: int
    arcs_upserted: int
    findings_upserted: int
    roles_upserted: int


class EnrichPipeline:
    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        enricher: ArelleEnricher | None = None,
        work_offline: bool = False,
    ) -> None:
        self._session_maker = session_maker
        self._owns_enricher = enricher is None
        self._enricher = enricher  # lazy-init in run() so import cost is paid once
        self._work_offline = work_offline

    async def run(self, accession_no: str, primary_path: Path) -> EnrichResult:
        async with self._session_maker() as session:
            filing = await self._load_filing(session, accession_no)
            if filing is None:
                raise RuntimeError(f"no filings row for {accession_no}")

        # why: Arelle import + DTS load is single-threaded and blocking; run it
        # off the event loop so the connection pool is never tied up during load.
        result = await asyncio.to_thread(
            self._enrich_blocking, primary_path, filing.entity_id
        )

        async with self._session_maker() as session:
            concept_ids = await self._upsert_concepts(session, filing.regulator, result.concepts)
            arcs = await self._upsert_arcs(session, result.arcs, concept_ids)
            findings = await self._upsert_findings(
                session, filing.id, result.findings
            )
            roles = await self._upsert_roles(session, result.roles)
            await session.execute(
                Filing.__table__.update()
                .where(Filing.id == filing.id)
                .values(arelle_validation_status=("clean" if not result.findings else "issues"))
            )
            await session.commit()

        out = EnrichResult(
            filing_id=filing.id,
            accession_no=accession_no,
            concepts_upserted=len(concept_ids),
            arcs_upserted=arcs,
            findings_upserted=findings,
            roles_upserted=roles,
        )
        log.info("enrich_persist_done", **asdict(out))
        return out

    def _enrich_blocking(self, primary_path: Path, entity_id: str | None):
        if self._enricher is None:
            self._enricher = ArelleEnricher(work_offline=self._work_offline)
            self._owns_enricher = True
        try:
            return self._enricher.enrich(primary_path, entity_id=entity_id)
        finally:
            if self._owns_enricher:
                self._enricher.close()
                self._enricher = None

    async def _load_filing(self, session: AsyncSession, accession_no: str) -> Filing | None:
        return (
            await session.execute(
                select(Filing).where(Filing.accession_no == accession_no)
            )
        ).scalar_one_or_none()

    async def _upsert_concepts(
        self,
        session: AsyncSession,
        regulator: str,
        concepts: list[EnrichedConcept],
    ) -> dict[str, int]:
        if not concepts:
            return {}
        # Dedup by (qname, taxonomy_uri) — matches the UNIQUE constraint.
        rows_by_key: dict[tuple, dict] = {}
        for c in concepts:
            key = (c.qname, c.taxonomy_uri)
            rows_by_key[key] = {
                "qname": c.qname,
                "taxonomy_uri": c.taxonomy_uri,
                "regulator": regulator,
                "is_extension": c.is_extension,
                "extension_owner_entity_id": c.extension_owner_entity_id,
                "standard_label": c.standard_label,
                "documentation": c.documentation,
                "period_type": c.period_type,
                "balance_type": c.balance_type,
                "data_type": c.data_type,
            }
        rows = list(rows_by_key.values())

        # why: Postgres caps bind parameters at 32767. concepts × 10 cols hits the
        # limit fast on a real US-GAAP DTS (~18k concepts). Chunk to stay safe and
        # merge RETURNING rows across calls.
        CHUNK = 2000
        ids: dict[tuple, int] = {}
        for i in range(0, len(rows), CHUNK):
            batch = rows[i : i + CHUNK]
            stmt = (
                pg_insert(Concept)
                .values(batch)
                .on_conflict_do_update(
                    constraint="uq_concepts_qname_taxonomy",
                    set_={
                        "standard_label": pg_insert(Concept).excluded.standard_label,
                        "documentation": pg_insert(Concept).excluded.documentation,
                        "period_type": pg_insert(Concept).excluded.period_type,
                        "balance_type": pg_insert(Concept).excluded.balance_type,
                        "data_type": pg_insert(Concept).excluded.data_type,
                        "is_extension": pg_insert(Concept).excluded.is_extension,
                        "extension_owner_entity_id": pg_insert(
                            Concept
                        ).excluded.extension_owner_entity_id,
                    },
                )
                .returning(Concept.id, Concept.qname, Concept.taxonomy_uri)
            )
            for (cid, qname, taxonomy_uri) in (await session.execute(stmt)).all():
                ids[(qname, taxonomy_uri)] = cid
        return ids

    async def _upsert_arcs(
        self,
        session: AsyncSession,
        arcs: list[EnrichedArc],
        concept_ids: dict[tuple, int],
    ) -> int:
        if not arcs:
            return 0
        # arcs reference concepts by qname only; we need to find a concept_id
        # for each. Build a qname-only lookup (first match wins on duplicates).
        by_qname: dict[str, int] = {}
        for (qname, _taxonomy), cid in concept_ids.items():
            by_qname.setdefault(qname, cid)

        rows: list[dict] = []
        for a in arcs:
            f = by_qname.get(a.from_qname)
            t = by_qname.get(a.to_qname)
            if f is None or t is None:
                continue
            rows.append(
                {
                    "linkbase_type": a.linkbase_type,
                    "extended_link_role": a.extended_link_role,
                    "from_concept_id": f,
                    "to_concept_id": t,
                    "arcrole": a.arcrole,
                    "arc_order": a.arc_order,
                    "weight": a.weight,
                    "preferred_label": a.preferred_label,
                }
            )
        if not rows:
            return 0
        # No unique constraint: clear-then-insert per (linkbase_type, ELR) span.
        # Cheap approach for now: delete arcs whose from-concept is in the new set, then insert.
        from_ids = sorted({r["from_concept_id"] for r in rows})
        await session.execute(
            ConceptArc.__table__.delete().where(
                ConceptArc.from_concept_id.in_(from_ids),
                ConceptArc.linkbase_type.in_(
                    sorted({r["linkbase_type"] for r in rows})
                ),
            )
        )
        # Chunk inserts to avoid Postgres parameter limit (~32k bind params).
        CHUNK = 1000
        for i in range(0, len(rows), CHUNK):
            await session.execute(pg_insert(ConceptArc).values(rows[i : i + CHUNK]))
        return len(rows)

    async def _upsert_roles(
        self,
        session: AsyncSession,
        roles: list[EnrichedRole],
    ) -> int:
        if not roles:
            return 0
        rows = [
            {
                "uri": r.uri,
                "definition": r.definition,
                "used_on": ",".join(r.used_on) if r.used_on else None,
            }
            for r in roles
        ]
        CHUNK = 2000
        count = 0
        for i in range(0, len(rows), CHUNK):
            batch = rows[i : i + CHUNK]
            stmt = (
                pg_insert(RoleDefinition)
                .values(batch)
                .on_conflict_do_update(
                    constraint="uq_role_definitions_uri",
                    set_={
                        "definition": pg_insert(RoleDefinition).excluded.definition,
                        "used_on": pg_insert(RoleDefinition).excluded.used_on,
                    },
                )
            )
            result = await session.execute(stmt)
            count += result.rowcount or len(batch)
        return count

    async def _upsert_findings(
        self,
        session: AsyncSession,
        filing_id: int,
        findings: list,
    ) -> int:
        await session.execute(
            DQCFinding.__table__.delete().where(DQCFinding.filing_id == filing_id)
        )
        if not findings:
            return 0
        rows = [
            {
                "filing_id": filing_id,
                "rule_id": f.rule_id,
                "severity": f.severity,
                "message": f.message,
                "concept_qname": f.concept_qname,
                "fact_id": None,
            }
            for f in findings
        ]
        await session.execute(pg_insert(DQCFinding).values(rows))
        return len(rows)
