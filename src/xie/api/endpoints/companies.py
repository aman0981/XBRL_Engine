"""Companies HTMX view backed by canonical_line_items (Phase 1.5: Mode A)."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xie.core.logging import get_logger
from xie.db.models import CanonicalLineItem, Filing, Restatement
from xie.db.session import AsyncSessionLocal, get_session
from xie.ingest.companyfacts_pipeline import CompanyFactsPipeline
from xie.regulators.sec.client import SECClient
from xie.regulators.sec.tickers import TickerResolver
from xie.xbrl.canonical_map import Canonical

log = get_logger("xie.api.companies")

router = APIRouter(tags=["companies"])

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", {})


@router.get("/companies/lookup")
async def lookup(ticker: str) -> RedirectResponse:
    return RedirectResponse(url=f"/companies/{ticker.upper()}", status_code=303)


@router.get("/companies/{ticker_or_cik}", response_class=HTMLResponse)
async def company(
    ticker_or_cik: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    cik10, ticker = await _resolve(ticker_or_cik)
    regulator = "ESEF" if _looks_like_lei(cik10) else "SEC"
    entity_name = await _entity_name(session, cik10)
    canonical_rows = await _annual_canonical(session, cik10, regulator)
    rows, periods, mapping_meta = _shape_for_template(canonical_rows)
    chart, kpis = _company_viz(rows, periods, mapping_meta)
    return templates.TemplateResponse(
        request,
        "company.html",
        {
            "cik10": cik10,
            "ticker": ticker,
            "regulator": regulator,
            "entity_name": entity_name,
            "rows": rows,
            "periods": periods,
            "mapping_meta": mapping_meta,
            "chart": chart,
            "kpis": kpis,
        },
    )


@router.get(
    "/companies/{ticker_or_cik}/restatements", response_class=HTMLResponse
)
async def company_restatements(
    ticker_or_cik: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    cik10, ticker = await _resolve(ticker_or_cik)
    regulator = "ESEF" if _looks_like_lei(cik10) else "SEC"
    rows = (
        await session.execute(
            select(
                Restatement.concept_qname,
                Restatement.period_start,
                Restatement.period_end,
                Restatement.period_instant,
                Restatement.original_value,
                Restatement.restated_value,
                Restatement.original_decimals_text,
                Restatement.restated_decimals_text,
                Restatement.original_reported_at,
                Restatement.restated_reported_at,
                Restatement.delta,
                Restatement.delta_pct,
                Restatement.is_amendment,
                Restatement.is_precision_change,
            )
            .where(
                Restatement.regulator == regulator,
                Restatement.entity_id == cik10,
                Restatement.is_precision_change.is_(False),
            )
            .order_by(Restatement.period_end.desc().nullslast(), Restatement.concept_qname)
            .limit(500)
        )
    ).mappings().all()
    return templates.TemplateResponse(
        request,
        "company_restatements.html",
        {
            "cik10": cik10,
            "ticker": ticker,
            "rows": [dict(r) for r in rows],
        },
    )


@router.post("/companies/{ticker_or_cik}/ingest")
async def company_ingest_on_demand(
    ticker_or_cik: str,
    background_tasks: BackgroundTasks,
) -> RedirectResponse:
    """On-demand Mode A bootstrap. Plan Locked Decision #18.

    Mode A end-to-end on a single CIK takes ~15-20s; this is more than a request
    can hold, so we run it as a background task. The user gets redirected back
    to the company page and can refresh once it completes.
    """
    cik10, _ticker = await _resolve(ticker_or_cik)
    background_tasks.add_task(_ingest_companyfacts_in_bg, cik10)
    return RedirectResponse(
        url=f"/companies/{ticker_or_cik.upper()}?ingesting=1", status_code=303
    )


async def _ingest_companyfacts_in_bg(cik10: str) -> None:
    from dataclasses import asdict

    pipeline = CompanyFactsPipeline(session_maker=AsyncSessionLocal)
    try:
        result = await pipeline.fetch_by_cik(cik10)
        log.info("on_demand_ingest_done", **asdict(result))
    except Exception as exc:
        log.error("on_demand_ingest_failed", cik=cik10, err=str(exc))
    finally:
        await pipeline.aclose()


def _looks_like_lei(token: str) -> bool:
    # ISO 17442 LEI: 20 chars [A-Z0-9].
    return len(token) == 20 and all(
        c.isdigit() or (c.isalpha() and c.isupper()) for c in token
    )


async def _resolve(ticker_or_cik: str) -> tuple[str, str | None]:
    """Resolve token to (entity_id, ticker_display).

    - All-digit token <= 10 chars  → SEC CIK10 (zero-padded)
    - 20-char [A-Z0-9]             → ESEF LEI (returned as-is)
    - Otherwise                    → SEC ticker (resolved via company_tickers.json)
    """
    raw = ticker_or_cik.strip()
    if _looks_like_lei(raw):
        return raw, None
    if raw.isdigit():
        return raw.zfill(10), None
    client = SECClient()
    try:
        cik10 = await TickerResolver(client).resolve(raw)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"unknown ticker: {raw}") from e
    finally:
        await client.aclose()
    return cik10, raw.upper()


async def _entity_name(session: AsyncSession, entity_id: str) -> str | None:
    """Resolve a display ticker (SEC) — ESEF rows have no ticker, return None."""
    stmt = (
        select(Filing.ticker)
        .where(Filing.entity_id == entity_id, Filing.ticker.is_not(None))
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _annual_canonical(
    session: AsyncSession, entity_id: str, regulator: str
) -> list[dict]:
    """Return canonical FY rows for an entity, scoped by regulator."""
    stmt = (
        select(
            CanonicalLineItem.canonical_name,
            CanonicalLineItem.period_end,
            CanonicalLineItem.period_instant,
            CanonicalLineItem.fiscal_period,
            CanonicalLineItem.fiscal_year,
            CanonicalLineItem.value,
            CanonicalLineItem.source_concept_qname,
            CanonicalLineItem.mapping_method,
            CanonicalLineItem.mapping_confidence,
            CanonicalLineItem.regulator,
        )
        .where(
            CanonicalLineItem.regulator == regulator,
            CanonicalLineItem.entity_id == entity_id,
            CanonicalLineItem.fiscal_period == "FY",
        )
    )
    return (await session.execute(stmt)).mappings().all()


def _shape_for_template(
    canonical_rows: list[dict],
) -> tuple[dict[str, dict], list[str], dict[str, dict]]:
    """Pivot: { canonical_name: { period_label: value } } + sorted period list."""
    rows: dict[str, dict] = defaultdict(dict)
    period_set: set[str] = set()
    mapping_meta: dict[str, dict] = {}
    for r in canonical_rows:
        label = _period_label(r)
        rows[r["canonical_name"]][label] = r["value"]
        period_set.add(label)
        if r["canonical_name"] not in mapping_meta:
            mapping_meta[r["canonical_name"]] = {
                "source_concept_qname": r["source_concept_qname"],
                "mapping_method": r["mapping_method"],
                "mapping_confidence": r["mapping_confidence"],
            }
    periods = sorted(period_set, reverse=True)[:5]  # last 5 years
    # Order canonical rows in a stable, business-meaningful sequence.
    ordered = {
        c.value: rows.get(c.value, {})
        for c in [
            Canonical.REVENUE,
            Canonical.NET_INCOME,
            Canonical.TOTAL_ASSETS,
            Canonical.OPERATING_CASH_FLOW,
            Canonical.EPS,
        ]
        if c.value in rows
    }
    return ordered, periods, {c: mapping_meta.get(c, {}) for c in ordered}


def _period_label(row: dict) -> str:
    """Bucket a canonical row by the fiscal year of its OWN period.

    why: companyfacts canonical rows carry the *source filing's* fiscal_year, so
    a single 10-K's comparatives all share one fiscal_year (e.g. 2025). Labelling
    by that value collapses every column to "FY2025" and splits instant (balance
    sheet) from duration (flow) facts into separate columns. Deriving the year
    from the period date instead yields distinct, correct columns and groups the
    flow + stock figures of the same year together.
    """
    period_date = row["period_end"] or row["period_instant"]
    if period_date:
        return f"FY{period_date.year}"
    return f"FY{row['fiscal_year']}"


# Display labels for KPI cards / chart legend (enum values are PascalCase).
_DISPLAY_NAMES = {
    "Revenue": "Revenue",
    "NetIncome": "Net Income",
    "TotalAssets": "Total Assets",
    "OperatingCashFlow": "Operating Cash Flow",
    "EPS": "EPS (diluted)",
}
# EPS is per-share (~1e0) and would be invisible on a currency axis (~1e11);
# it keeps its KPI card + sparkline but is excluded from the shared line chart.
_CHART_EXCLUDE = {"EPS"}


def _short_label(label: str | None) -> str | None:
    """'FY2024 (2024-09-28)' -> 'FY2024'."""
    return label.split(" (")[0] if label else label


def _to_float(value) -> float | None:
    return float(value) if value is not None else None


def _compact(n: float | None) -> str:
    """Human-readable magnitude: 416_161_000_000 -> '416.16 B'."""
    if n is None:
        return "—"
    a = abs(n)
    for div, suf in ((1e12, " T"), (1e9, " B"), (1e6, " M"), (1e3, " K")):
        if a >= div:
            return f"{n / div:,.2f}{suf}"
    return f"{n:,.2f}"


def _company_viz(
    rows: dict[str, dict], periods: list[str], mapping_meta: dict[str, dict]
) -> tuple[dict, list[dict]]:
    """Build chart series + KPI cards from the already-capped pivot.

    Iterates ONLY over `periods` (capped to 5 by _shape_for_template) so the
    page never surfaces more than the 5 most-recent fiscal years.
    """
    ascending = list(reversed(periods))  # chart x-axis oldest -> newest
    categories = [_short_label(p) for p in ascending]
    series: list[dict] = []
    kpis: list[dict] = []
    for name, values in rows.items():
        data = [_to_float(values.get(p)) for p in ascending]
        if not any(v is not None for v in data):
            continue
        if name not in _CHART_EXCLUDE:
            series.append({"name": _DISPLAY_NAMES.get(name, name), "data": data})
        latest = latest_label = prev = None
        for p in periods:  # newest-first
            v = _to_float(values.get(p))
            if v is None:
                continue
            if latest is None:
                latest, latest_label = v, p
            elif prev is None:
                prev = v
                break
        yoy = ((latest - prev) / abs(prev) * 100) if (latest is not None and prev) else None
        kpis.append(
            {
                "name": _DISPLAY_NAMES.get(name, name),
                "value": latest,
                "value_display": _compact(latest),
                "fy": _short_label(latest_label),
                "yoy": yoy,
                "spark": [v for v in data if v is not None],
                "method": mapping_meta.get(name, {}).get("mapping_method"),
            }
        )
    chart = {
        "categories": categories,
        "series": series,
        "aria": "Canonical financial values by fiscal year",
    }
    return chart, kpis
