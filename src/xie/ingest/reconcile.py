"""Mode A (companyfacts) vs Mode B (source iXBRL) reconciliation.

Joins fact rows for a single filing by (concept_qname, period signature, unit signature),
then categorizes each pair into a mismatch_type and persists into `fact_reconciliation`.

The categories from the plan:
  - only_in_a     -- SEC's companyfacts has it but our lxml parse dropped/missed it
  - only_in_b     -- iXBRL has it but SEC's companyfacts strips it (footnotes, hidden, dimensional)
  - value_diff    -- both sides present, numeric values disagree
  - precision_diff -- numeric values agree but `decimals` attributes differ

This is the *headline differentiator* of the engine (per plan: portfolio-grade moment).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Iterable

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from xie.core.logging import get_logger
from xie.db.models import Context, Fact, FactReconciliation, Filing, Unit

log = get_logger("xie.ingest.reconcile")


@dataclass(slots=True)
class ReconcileResult:
    filing_id: int
    accession_no: str
    pairs_examined: int
    only_in_a: int
    only_in_b: int
    value_diff: int
    precision_diff: int


def _period_signature(
    period_start, period_end, period_instant
) -> tuple:
    if period_instant is not None:
        return ("instant", period_instant)
    return ("duration", period_start, period_end)


def _unit_signature(unit_ref: str | None) -> str:
    return unit_ref or ""


@dataclass(slots=True)
class _FactSlim:
    fact_id: int
    concept_qname: str
    value_numeric: Decimal | None
    decimals_text: str | None
    context_id: int | None
    unit_id: int | None
    period_sig: tuple
    unit_sig: str


class Reconciler:
    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self._session_maker = session_maker

    async def reconcile_filing(self, accession_no: str) -> ReconcileResult:
        async with self._session_maker() as session:
            filing_id = await self._filing_id(session, accession_no)
            if filing_id is None:
                raise RuntimeError(f"unknown accession_no: {accession_no}")
            mode_a = await self._load_side(
                session, filing_id, source="companyfacts"
            )
            mode_b = await self._load_side(
                session, filing_id, source="source_ixbrl"
            )

            # Clear prior reconciliation rows for this filing — idempotent re-run.
            await session.execute(
                FactReconciliation.__table__.delete().where(
                    FactReconciliation.filing_id == filing_id
                )
            )

            pairs, only_a, only_b, vdiff, pdiff = self._diff(mode_a, mode_b)
            inserts: list[dict] = []
            for cat, items in (
                ("only_in_a", only_a),
                ("only_in_b", only_b),
                ("value_diff", vdiff),
                ("precision_diff", pdiff),
            ):
                for record in items:
                    inserts.append({**record, "filing_id": filing_id, "mismatch_type": cat})
            if inserts:
                await session.execute(pg_insert(FactReconciliation).values(inserts))
            await session.commit()

        result = ReconcileResult(
            filing_id=filing_id,
            accession_no=accession_no,
            pairs_examined=pairs,
            only_in_a=len(only_a),
            only_in_b=len(only_b),
            value_diff=len(vdiff),
            precision_diff=len(pdiff),
        )
        log.info("reconcile_done", **asdict(result))
        return result

    async def _filing_id(self, session: AsyncSession, accession_no: str) -> int | None:
        return (
            await session.execute(
                select(Filing.id).where(Filing.accession_no == accession_no)
            )
        ).scalar_one_or_none()

    async def _load_side(
        self, session: AsyncSession, filing_id: int, source: str
    ) -> list[_FactSlim]:
        stmt = (
            select(
                Fact.id,
                Fact.concept_qname,
                Fact.value_numeric,
                Fact.decimals_text,
                Fact.context_id,
                Fact.unit_id,
                Context.period_start,
                Context.period_end,
                Context.period_instant,
                Unit.unit_ref,
            )
            .join(Context, Context.id == Fact.context_id, isouter=True)
            .join(Unit, Unit.id == Fact.unit_id, isouter=True)
            .where(Fact.filing_id == filing_id, Fact.source == source)
        )
        rows = (await session.execute(stmt)).all()
        out: list[_FactSlim] = []
        for r in rows:
            (fid, qname, val, decs, cid, uid, p_start, p_end, p_inst, unit_ref) = r
            out.append(
                _FactSlim(
                    fact_id=fid,
                    concept_qname=qname,
                    value_numeric=val,
                    decimals_text=decs,
                    context_id=cid,
                    unit_id=uid,
                    period_sig=_period_signature(p_start, p_end, p_inst),
                    unit_sig=_unit_signature(unit_ref),
                )
            )
        return out

    def _diff(
        self, mode_a: Iterable[_FactSlim], mode_b: Iterable[_FactSlim]
    ) -> tuple[int, list[dict], list[dict], list[dict], list[dict]]:
        # Mode B can have many facts sharing the same (qname, period, unit) because
        # dimensional facts repeat by axis-member. Group both sides by the same key,
        # then pair the first-seen fact on each side.
        def key(f: _FactSlim) -> tuple:
            return (f.concept_qname, f.period_sig, f.unit_sig)

        # For Mode B, pick the non-dimensional rep when possible. We don't have
        # is_dimensional in _FactSlim — defer that polish; first-seen is fine.
        a_by_key: dict[tuple, _FactSlim] = {}
        for f in mode_a:
            a_by_key.setdefault(key(f), f)
        b_by_key: dict[tuple, _FactSlim] = {}
        for f in mode_b:
            b_by_key.setdefault(key(f), f)

        only_a: list[dict] = []
        only_b: list[dict] = []
        value_diff: list[dict] = []
        precision_diff: list[dict] = []

        all_keys = set(a_by_key) | set(b_by_key)
        for k in all_keys:
            a = a_by_key.get(k)
            b = b_by_key.get(k)
            if a is None and b is not None:
                only_b.append(
                    {
                        "concept_qname": b.concept_qname,
                        "context_id": b.context_id,
                        "unit_id": b.unit_id,
                        "companyfacts_value": None,
                        "source_ixbrl_value": b.value_numeric,
                        "companyfacts_decimals_text": None,
                        "source_decimals_text": b.decimals_text,
                    }
                )
            elif a is not None and b is None:
                only_a.append(
                    {
                        "concept_qname": a.concept_qname,
                        "context_id": a.context_id,
                        "unit_id": a.unit_id,
                        "companyfacts_value": a.value_numeric,
                        "source_ixbrl_value": None,
                        "companyfacts_decimals_text": a.decimals_text,
                        "source_decimals_text": None,
                    }
                )
            elif a is not None and b is not None:
                if a.value_numeric is None or b.value_numeric is None:
                    continue
                if a.value_numeric != b.value_numeric:
                    value_diff.append(
                        {
                            "concept_qname": a.concept_qname,
                            "context_id": b.context_id,
                            "unit_id": b.unit_id,
                            "companyfacts_value": a.value_numeric,
                            "source_ixbrl_value": b.value_numeric,
                            "companyfacts_decimals_text": a.decimals_text,
                            "source_decimals_text": b.decimals_text,
                        }
                    )
                elif a.decimals_text != b.decimals_text and (
                    a.decimals_text is not None or b.decimals_text is not None
                ):
                    precision_diff.append(
                        {
                            "concept_qname": a.concept_qname,
                            "context_id": b.context_id,
                            "unit_id": b.unit_id,
                            "companyfacts_value": a.value_numeric,
                            "source_ixbrl_value": b.value_numeric,
                            "companyfacts_decimals_text": a.decimals_text,
                            "source_decimals_text": b.decimals_text,
                        }
                    )
        return len(all_keys), only_a, only_b, value_diff, precision_diff
