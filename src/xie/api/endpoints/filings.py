"""Filing-detail HTMX views: facts table + Mode A vs Mode B reconciliation."""
from __future__ import annotations

import base64
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from xie.db.models import (
    Concept,
    Context,
    DQCFinding,
    Fact,
    FactReconciliation,
    Filing,
    Unit,
)
from xie.db.session import get_session
from xie.xbrl.linkbase import list_statement_roles, reconstruct_statement

router = APIRouter(tags=["filings"])

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

FACTS_PAGE_DEFAULT = 200
FACTS_PAGE_MAX = 1000


@router.get("/filings/{accession_no}/facts", response_class=HTMLResponse)
async def filing_facts(
    accession_no: str,
    request: Request,
    limit: int = FACTS_PAGE_DEFAULT,
    source: str = "source_ixbrl",
    hidden: bool | None = None,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    limit = min(max(limit, 1), FACTS_PAGE_MAX)
    filing = await _get_filing(session, accession_no)
    counts = await _fact_counts(session, filing.id)
    dqc = await _dqc_summary(session, filing.id)
    facts = await _facts_page(session, filing.id, source, hidden, limit)
    return templates.TemplateResponse(
        request,
        "filing_facts.html",
        {
            "filing": filing,
            "counts": counts,
            "dqc": dqc,
            "facts": facts,
            "source": source,
            "limit": limit,
            "hidden": hidden,
        },
    )


@router.get("/filings/{accession_no}/reconciliation", response_class=HTMLResponse)
async def filing_reconciliation(
    accession_no: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    filing = await _get_filing(session, accession_no)
    summary, rows = await _reconciliation_rows(session, filing.id)
    return templates.TemplateResponse(
        request,
        "filing_reconciliation.html",
        {"filing": filing, "summary": summary, "rows": rows},
    )


@router.get("/filings/{accession_no}/statements", response_class=HTMLResponse)
async def filing_statements(
    accession_no: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    filing = await _get_filing(session, accession_no)
    roles = await list_statement_roles(session, regulator=filing.regulator)
    for r in roles:
        r["role_b64"] = base64.urlsafe_b64encode(r["uri"].encode()).decode().rstrip("=")
    return templates.TemplateResponse(
        request,
        "filing_statements.html",
        {"filing": filing, "roles": roles},
    )


@router.get(
    "/filings/{accession_no}/statements/{role_b64}", response_class=HTMLResponse
)
async def filing_statement(
    accession_no: str,
    role_b64: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    filing = await _get_filing(session, accession_no)
    try:
        padding = "=" * (-len(role_b64) % 4)
        role_uri = base64.urlsafe_b64decode(role_b64 + padding).decode()
    except Exception as e:
        raise HTTPException(status_code=400, detail="bad role token") from e
    statement = await reconstruct_statement(session, filing.id, role_uri)
    return templates.TemplateResponse(
        request,
        "filing_statement.html",
        {"filing": filing, "statement": statement},
    )


async def _get_filing(session: AsyncSession, accession_no: str) -> Filing:
    filing = (
        await session.execute(select(Filing).where(Filing.accession_no == accession_no))
    ).scalar_one_or_none()
    if not filing:
        raise HTTPException(status_code=404, detail=f"unknown accession: {accession_no}")
    return filing


async def _fact_counts(session: AsyncSession, filing_id: int) -> dict[str, int]:
    stmt = (
        select(Fact.source, func.count())
        .where(Fact.filing_id == filing_id)
        .group_by(Fact.source)
    )
    out = {"companyfacts": 0, "source_ixbrl": 0, "hidden": 0, "dimensional": 0}
    for src, n in (await session.execute(stmt)).all():
        out[src] = n
    out["hidden"] = (
        await session.execute(
            select(func.count())
            .select_from(Fact)
            .where(Fact.filing_id == filing_id, Fact.is_hidden.is_(True))
        )
    ).scalar_one()
    out["dimensional"] = (
        await session.execute(
            select(func.count())
            .select_from(Fact)
            .where(Fact.filing_id == filing_id, Fact.is_dimensional.is_(True))
        )
    ).scalar_one()
    return out


async def _facts_page(
    session: AsyncSession,
    filing_id: int,
    source: str,
    hidden: bool | None,
    limit: int,
) -> list[dict]:
    stmt = (
        select(
            Fact.concept_qname,
            Fact.value_numeric,
            Fact.value_text,
            Fact.decimals_text,
            Fact.scale,
            Fact.format_qname,
            Fact.transform_registry,
            Fact.is_hidden,
            Fact.is_dimensional,
            Context.period_start,
            Context.period_end,
            Context.period_instant,
            Unit.unit_ref,
            Concept.standard_label,
            Concept.period_type,
            Concept.balance_type,
        )
        .join(Context, Context.id == Fact.context_id, isouter=True)
        .join(Unit, Unit.id == Fact.unit_id, isouter=True)
        .join(Concept, Concept.qname == Fact.concept_qname, isouter=True)
        .where(Fact.filing_id == filing_id, Fact.source == source)
        .order_by(Fact.concept_qname, Context.period_end.desc())
        .limit(limit)
    )
    if hidden is not None:
        stmt = stmt.where(Fact.is_hidden.is_(hidden))
    rows = (await session.execute(stmt)).mappings().all()
    return [dict(r) for r in rows]


async def _dqc_summary(session: AsyncSession, filing_id: int) -> dict[str, int]:
    out = {"error": 0, "warning": 0, "info": 0}
    stmt = (
        select(DQCFinding.severity, func.count())
        .where(DQCFinding.filing_id == filing_id)
        .group_by(DQCFinding.severity)
    )
    for sev, n in (await session.execute(stmt)).all():
        out[sev or "info"] = n
    return out


async def _reconciliation_rows(
    session: AsyncSession, filing_id: int
) -> tuple[dict, list[dict]]:
    stmt_summary = (
        select(FactReconciliation.mismatch_type, func.count())
        .where(FactReconciliation.filing_id == filing_id)
        .group_by(FactReconciliation.mismatch_type)
    )
    summary = {
        cat: 0 for cat in ("only_in_a", "only_in_b", "value_diff", "precision_diff")
    }
    for cat, n in (await session.execute(stmt_summary)).all():
        summary[cat] = n

    stmt_rows = (
        select(
            FactReconciliation.mismatch_type,
            FactReconciliation.concept_qname,
            FactReconciliation.companyfacts_value,
            FactReconciliation.source_ixbrl_value,
            FactReconciliation.companyfacts_decimals_text,
            FactReconciliation.source_decimals_text,
            Context.period_start,
            Context.period_end,
            Context.period_instant,
            Unit.unit_ref,
        )
        .join(Context, Context.id == FactReconciliation.context_id, isouter=True)
        .join(Unit, Unit.id == FactReconciliation.unit_id, isouter=True)
        .where(FactReconciliation.filing_id == filing_id)
        .order_by(FactReconciliation.mismatch_type, FactReconciliation.concept_qname)
        .limit(500)
    )
    rows = (await session.execute(stmt_rows)).mappings().all()
    return summary, [dict(r) for r in rows]
