"""Cross-filing restatement detection.

Algorithm (SCD-Type-2-aware bitemporal model):
  1. For an entity, group facts by the natural key
     (concept_qname, period_start, period_end, period_instant,
      context_dimensions_hash, unit_signature).
  2. Within each group, order rows by the *filing_date* of the filing they were
     reported in. Consecutive pairs (prev, next) where the value differs at
     `compare_at_precision` represent a restatement event.
  3. Persist into `restatements` with delta + delta_pct + amendment flags.

Mode A (companyfacts) is the primary input — it carries facts from many filings
for the same period, so consecutive pairs of differing values surface naturally.
Mode B contributes when the same accession's source iXBRL has been parsed.

`compare_at_precision`: if two facts disagree by less than the coarser of their
`decimals` exponents, treat that as a precision artifact only (`is_precision_change=true`),
not a real restatement. Example: 401,672,000 reported at decimals=-3 (rounded to 402,000,000)
vs reported elsewhere as 401,672,000 at decimals=-6 — same value, different precision.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from xie.core.logging import get_logger
from xie.db.models import Restatement

log = get_logger("xie.xbrl.restatement")


_NATURAL_NULL = "NULL_PLACEHOLDER"


@dataclass(slots=True)
class RestatementSummary:
    cik10: str
    pairs_examined: int
    restatements_written: int
    precision_only: int


def compare_at_precision(
    a_value: Decimal | None,
    a_decimals: str | None,
    b_value: Decimal | None,
    b_decimals: str | None,
) -> tuple[bool, bool]:
    """Return (values_differ_materially, precision_change_only).

    - decimals='INF' means infinitely precise (no rounding).
    - decimals='-6' means precise to nearest million (round value to 10^6).
    - When the two sides have different decimals, round both to the *coarser*
      (less negative) before comparing. If they then match, the difference is
      precision-only.
    """
    if a_value is None or b_value is None:
        return a_value != b_value, False
    if a_value == b_value:
        return False, False
    a_exp = _decimals_exp(a_decimals)
    b_exp = _decimals_exp(b_decimals)
    if a_exp is None or b_exp is None:
        return True, False
    # INF means "exact, do not round". If either side is INF the values must match
    # exactly; we already ruled out equality above, so any difference is material.
    if a_exp == _INF_EXP or b_exp == _INF_EXP:
        return True, False
    # SEC `decimals=-6` means precise to nearest 1,000,000 — i.e. *coarser* than -3.
    # The smaller (more negative) integer is the coarser precision; round both to it.
    coarse = min(a_exp, b_exp)
    rounded_a = _round_to_exp(a_value, coarse)
    rounded_b = _round_to_exp(b_value, coarse)
    if rounded_a == rounded_b:
        return False, True
    return True, False


_INF_EXP = "INF"


def _decimals_exp(decimals_text: str | None) -> int | str | None:
    if not decimals_text:
        return None
    if decimals_text.upper() == "INF":
        return _INF_EXP
    try:
        return int(decimals_text)
    except (TypeError, ValueError):
        return None


def _round_to_exp(value: Decimal, decimals_exp: int) -> Decimal:
    """decimals=-6 means round to nearest 10^6, i.e. quantize to 1e6."""
    if decimals_exp <= 0:
        magnitude = Decimal("1e{}".format(-decimals_exp))
        return (value / magnitude).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * magnitude
    quantum = Decimal("1e-{}".format(decimals_exp))
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


_CTE_SQL = """
WITH ranked AS (
  SELECT
    f.id            AS fact_id,
    f.filing_id     AS filing_id,
    fl.accession_no AS accession_no,
    fl.filing_date  AS filing_date,
    fl.amends_accession_no AS amends_accession_no,
    fl.form_type    AS form_type,
    f.concept_qname AS concept_qname,
    f.period_start  AS period_start,
    f.period_end    AS period_end,
    f.period_instant AS period_instant,
    f.value_numeric AS value_numeric,
    f.decimals_text AS decimals_text,
    COALESCE(c.dimensions_hash, :no_hash) AS dim_hash,
    COALESCE(u.unit_ref, :no_unit) AS unit_sig,
    ROW_NUMBER() OVER (
      PARTITION BY
        f.concept_qname,
        COALESCE(f.period_start, DATE '0001-01-01'),
        COALESCE(f.period_end, DATE '0001-01-01'),
        COALESCE(f.period_instant, DATE '0001-01-01'),
        COALESCE(c.dimensions_hash, :no_hash),
        COALESCE(u.unit_ref, :no_unit)
      ORDER BY fl.filing_date, fl.id
    ) AS rn
  FROM facts f
  JOIN filings fl ON fl.id = f.filing_id
  LEFT JOIN contexts c ON c.id = f.context_id
  LEFT JOIN units u    ON u.id = f.unit_id
  WHERE fl.regulator = :regulator
    AND fl.entity_id = :cik
    AND f.value_numeric IS NOT NULL
    AND f.is_dimensional = FALSE
)
SELECT
  a.fact_id        AS orig_fact_id,
  a.filing_id      AS orig_filing_id,
  a.filing_date    AS orig_reported_at,
  a.value_numeric  AS orig_value,
  a.decimals_text  AS orig_decimals,
  a.accession_no   AS orig_accession_no,
  a.form_type      AS orig_form_type,
  b.fact_id        AS rest_fact_id,
  b.filing_id      AS rest_filing_id,
  b.filing_date    AS rest_reported_at,
  b.value_numeric  AS rest_value,
  b.decimals_text  AS rest_decimals,
  b.accession_no   AS rest_accession_no,
  b.form_type      AS rest_form_type,
  b.amends_accession_no AS rest_amends_accession_no,
  a.concept_qname,
  a.period_start,
  a.period_end,
  a.period_instant,
  a.dim_hash,
  a.unit_sig
FROM ranked a
JOIN ranked b ON
       a.concept_qname = b.concept_qname
   AND COALESCE(a.period_start, DATE '0001-01-01')   = COALESCE(b.period_start, DATE '0001-01-01')
   AND COALESCE(a.period_end, DATE '0001-01-01')     = COALESCE(b.period_end, DATE '0001-01-01')
   AND COALESCE(a.period_instant, DATE '0001-01-01') = COALESCE(b.period_instant, DATE '0001-01-01')
   AND a.dim_hash = b.dim_hash
   AND a.unit_sig = b.unit_sig
   AND a.rn = b.rn - 1
WHERE a.value_numeric IS DISTINCT FROM b.value_numeric
"""


async def detect_restatements_for_entity(
    session_maker: async_sessionmaker[AsyncSession],
    cik10: str,
    regulator: str = "SEC",
) -> RestatementSummary:
    pairs_examined = 0
    precision_only = 0
    inserts: list[dict] = []

    async with session_maker() as session:
        result = await session.execute(
            text(_CTE_SQL),
            {"regulator": regulator, "cik": cik10, "no_hash": _NATURAL_NULL, "no_unit": _NATURAL_NULL},
        )
        for row in result.mappings():
            pairs_examined += 1
            material, precision_change = compare_at_precision(
                row["orig_value"],
                row["orig_decimals"],
                row["rest_value"],
                row["rest_decimals"],
            )
            if not material and precision_change:
                precision_only += 1
            if not material and not precision_change:
                continue
            inserts.append(_row_to_insert(regulator, cik10, row, precision_change))

        # Clear prior detections for this entity so re-runs are idempotent.
        await session.execute(
            Restatement.__table__.delete().where(
                Restatement.regulator == regulator, Restatement.entity_id == cik10
            )
        )
        if inserts:
            await session.execute(pg_insert(Restatement).values(inserts))
        await session.commit()

    summary = RestatementSummary(
        cik10=cik10,
        pairs_examined=pairs_examined,
        restatements_written=len(inserts),
        precision_only=precision_only,
    )
    log.info(
        "restatements_done",
        cik=cik10,
        pairs=pairs_examined,
        written=len(inserts),
        precision_only=precision_only,
    )
    return summary


def _row_to_insert(
    regulator: str, cik10: str, row: dict, precision_change: bool
) -> dict:
    orig_val: Decimal = row["orig_value"]
    rest_val: Decimal = row["rest_value"]
    delta = rest_val - orig_val
    delta_pct = (delta / orig_val * Decimal(100)) if orig_val != 0 else None
    rest_form = (row["rest_form_type"] or "").upper()
    is_amendment = rest_form.endswith("/A") or bool(row["rest_amends_accession_no"])

    dim_hash = row["dim_hash"]
    unit_sig = row["unit_sig"]
    if dim_hash == _NATURAL_NULL:
        dim_hash = None
    if unit_sig == _NATURAL_NULL:
        unit_sig = None

    return {
        "regulator": regulator,
        "entity_id": cik10,
        "concept_qname": row["concept_qname"],
        "period_start": row["period_start"],
        "period_end": row["period_end"],
        "period_instant": row["period_instant"],
        "context_dimensions_hash": dim_hash,
        "unit_signature": unit_sig,
        "original_filing_id": row["orig_filing_id"],
        "original_value": orig_val,
        "original_decimals_text": row["orig_decimals"],
        "original_reported_at": row["orig_reported_at"],
        "restated_filing_id": row["rest_filing_id"],
        "restated_value": rest_val,
        "restated_decimals_text": row["rest_decimals"],
        "restated_reported_at": row["rest_reported_at"],
        "delta": delta,
        "delta_pct": delta_pct.quantize(Decimal("0.0001"))
        if delta_pct is not None
        else None,
        "is_amendment": is_amendment,
        "is_concept_migration": False,  # concept_lineage seed needed; Phase 7 polish
        "is_precision_change": precision_change,
    }
