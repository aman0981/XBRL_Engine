"""Typer CLI.

Examples:
    python -m xie.ingest.cli fetch --ticker AAPL --form 10-K --limit 1
    python -m xie.ingest.cli bootstrap-companyfacts --ticker AAPL
    python -m xie.ingest.cli parse-filing --accession 0000320193-25-000079
    python -m xie.ingest.cli reconcile --accession 0000320193-25-000079
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from xie.core.config import settings
from xie.core.logging import configure_logging
from xie.db.models import Filing
from xie.db.session import AsyncSessionLocal
from xie.ingest.companyfacts_pipeline import CompanyFactsPipeline
from xie.ingest.enrich_pipeline import EnrichPipeline
from xie.ingest.esef_pipeline import ESEFIngestPipeline
from xie.ingest.modeb_pipeline import ModeBPipeline
from xie.ingest.reconcile import Reconciler
from xie.ingest.sec_pipeline import SECIngestPipeline
from xie.regulators.sec.client import SECClient
from xie.regulators.sec.tickers import TickerResolver
from xie.xbrl.canonical_project import project_canonical_for_entity
from xie.xbrl.restatement import detect_restatements_for_entity

console = Console()
app = typer.Typer(help="XBRL Intelligence Engine ingestion CLI")


@app.callback()
def _root() -> None:
    configure_logging()


@app.command()
def fetch(
    ticker: str = typer.Option("AAPL", help="Stock ticker, e.g. AAPL"),
    form: str = typer.Option("10-K", help="Form type, e.g. 10-K, 10-Q"),
    limit: int = typer.Option(1, help="Max filings to fetch"),
    persist: bool = typer.Option(
        False,
        "--persist",
        help="Upsert into filings table (requires running Postgres). "
        "Default downloads blobs only.",
    ),
) -> None:
    """Fetch source iXBRL filings for a ticker and persist raw blobs + manifest."""
    session_maker = AsyncSessionLocal if persist else None
    results = asyncio.run(_run(ticker, form, limit, session_maker))

    if not results:
        console.print(f"[yellow]No {form} filings found for {ticker}.[/yellow]")
        raise typer.Exit(code=1)

    table = Table(title=f"{ticker} {form} (top {len(results)})")
    table.add_column("CIK")
    table.add_column("Accession")
    table.add_column("Filed")
    table.add_column("Docs")
    table.add_column("sha256[0:12]")
    table.add_column("Path")
    table.add_column("filings.id")
    for r in results:
        sha = (r.raw_sha256 or "")[:12]
        table.add_row(
            r.cik10,
            r.accession_no,
            r.filing_date,
            str(r.document_count),
            sha,
            str(r.primary_path),
            str(r.persisted_filing_id) if r.persisted_filing_id else "-",
        )
    console.print(table)


async def _run(ticker: str, form: str, limit: int, session_maker) -> list:
    pipeline = SECIngestPipeline(session_maker=session_maker)
    try:
        return await pipeline.fetch_by_ticker(ticker=ticker, form=form, limit=limit)
    finally:
        await pipeline.aclose()


@app.command("bootstrap-companyfacts")
def bootstrap_companyfacts(
    ticker: str = typer.Option(None, help="Stock ticker, e.g. AAPL"),
    cik: str = typer.Option(None, help="Explicit CIK10, alternative to --ticker"),
) -> None:
    """Mode A bootstrap: fetch SEC companyfacts JSON, persist facts + canonical view.

    Requires a running Postgres + applied migrations.
    """
    if not ticker and not cik:
        console.print("[red]Provide --ticker or --cik.[/red]")
        raise typer.Exit(code=2)
    result = asyncio.run(_run_companyfacts(ticker=ticker, cik=cik))
    table = Table(title=f"Mode A ingest: {result.entity_name or result.cik10}")
    table.add_column("CIK")
    table.add_column("Entity")
    table.add_column("Filings")
    table.add_column("Facts")
    table.add_column("Canonical rows")
    table.add_row(
        result.cik10,
        result.entity_name,
        str(result.filings_upserted),
        str(result.facts_upserted),
        str(result.canonical_rows_upserted),
    )
    console.print(table)


async def _run_companyfacts(ticker: str | None, cik: str | None):
    client = SECClient()
    try:
        if cik:
            cik10 = cik.zfill(10)
        else:
            resolver = TickerResolver(client)
            cik10 = await resolver.resolve(ticker)
        pipeline = CompanyFactsPipeline(session_maker=AsyncSessionLocal, client=client)
        try:
            return await pipeline.fetch_by_cik(cik10)
        finally:
            # pipeline does not own the client; close client below
            pass
    finally:
        await client.aclose()


@app.command("parse-filing")
def parse_filing(
    accession: str = typer.Option(..., help="SEC accession number, e.g. 0000320193-25-000079"),
    primary: str = typer.Option(
        None,
        help="Override path to the primary iXBRL .htm. Default reads filings.raw_path.",
    ),
) -> None:
    """Mode B: parse the primary iXBRL for a previously-fetched filing into facts + dims."""
    result = asyncio.run(_run_parse(accession, primary))
    table = Table(title=f"Mode B parse: {accession}")
    for col in ("filing_id", "contexts", "units", "facts", "dimensions"):
        table.add_column(col)
    table.add_row(
        str(result.filing_id),
        str(result.contexts_upserted),
        str(result.units_upserted),
        str(result.facts_upserted),
        str(result.fact_dimensions_upserted),
    )
    console.print(table)


async def _run_parse(accession: str, primary_override: str | None):
    async with AsyncSessionLocal() as session:
        stmt = select(Filing.raw_path).where(Filing.accession_no == accession)
        raw_path = (await session.execute(stmt)).scalar_one_or_none()
    if primary_override:
        path = Path(primary_override)
    elif raw_path:
        path = Path(raw_path)
    else:
        raise typer.BadParameter(
            f"no filings row for {accession} and no --primary supplied. "
            f"Run `fetch --persist` first or pass --primary."
        )
    if not path.exists():
        raise typer.BadParameter(f"primary iXBRL not found: {path}")
    pipeline = ModeBPipeline(session_maker=AsyncSessionLocal)
    return await pipeline.parse_and_persist(accession_no=accession, primary_path=path)


@app.command("reconcile")
def reconcile(
    accession: str = typer.Option(..., help="SEC accession number"),
) -> None:
    """Diff Mode A (companyfacts) vs Mode B (source iXBRL) for one filing."""
    result = asyncio.run(_run_reconcile(accession))
    table = Table(title=f"Reconciliation: {accession}")
    for col in ("pairs", "only_in_a", "only_in_b", "value_diff", "precision_diff"):
        table.add_column(col)
    table.add_row(
        str(result.pairs_examined),
        str(result.only_in_a),
        str(result.only_in_b),
        str(result.value_diff),
        str(result.precision_diff),
    )
    console.print(table)


async def _run_reconcile(accession: str):
    reconciler = Reconciler(session_maker=AsyncSessionLocal)
    return await reconciler.reconcile_filing(accession_no=accession)


@app.command("enrich-filing")
def enrich_filing(
    accession: str = typer.Option(..., help="SEC accession number"),
    primary: str = typer.Option(
        None,
        help="Override path to the primary iXBRL .htm. Default reads filings.raw_path.",
    ),
    offline: bool = typer.Option(
        False,
        "--offline",
        help="Run Arelle with webCache.workOffline=True (Mode C / user-upload SSRF guard).",
    ),
) -> None:
    """Phase 3: load DTS via Arelle, persist concepts + linkbase arcs + Arelle findings."""
    result = asyncio.run(_run_enrich(accession, primary, offline=offline))
    table = Table(title=f"Arelle enrich: {accession}")
    for col in ("filing_id", "concepts", "arcs", "findings", "roles"):
        table.add_column(col)
    table.add_row(
        str(result.filing_id),
        str(result.concepts_upserted),
        str(result.arcs_upserted),
        str(result.findings_upserted),
        str(result.roles_upserted),
    )
    console.print(table)


@app.command("canonicalize")
def canonicalize(
    ticker: str = typer.Option(None, help="Ticker (resolved to CIK10)"),
    cik: str = typer.Option(None, help="Explicit CIK10"),
) -> None:
    """Re-project canonical_line_items for an entity from facts (Mode A + B)."""
    if not ticker and not cik:
        raise typer.BadParameter("Provide --ticker or --cik")
    rows = asyncio.run(_run_canonicalize(ticker, cik))
    console.print(f"[green]canonical rows projected:[/green] {rows}")


async def _run_canonicalize(ticker: str | None, cik: str | None) -> int:
    """Re-project canonicals for an entity, auto-detecting SEC vs ESEF."""
    if cik and len(cik) == 20 and all(c.isdigit() or (c.isalpha() and c.isupper()) for c in cik):
        # 20-char [A-Z0-9] = ESEF LEI
        async with AsyncSessionLocal() as session:
            return await project_canonical_for_entity(
                session, cik, sources=("source_ixbrl",), regulator="ESEF"
            )
    client = SECClient()
    try:
        if cik:
            cik10 = cik.zfill(10)
        else:
            cik10 = await TickerResolver(client).resolve(ticker)
    finally:
        await client.aclose()
    async with AsyncSessionLocal() as session:
        return await project_canonical_for_entity(
            session,
            cik10,
            sources=("companyfacts", "source_ixbrl"),
            regulator="SEC",
        )


async def _run_enrich(accession: str, primary_override: str | None, offline: bool = False):
    async with AsyncSessionLocal() as session:
        from sqlalchemy import select as _select
        from xie.db.models import Filing as _Filing
        raw_path = (
            await session.execute(
                _select(_Filing.raw_path).where(_Filing.accession_no == accession)
            )
        ).scalar_one_or_none()
    if primary_override:
        path = Path(primary_override)
    elif raw_path:
        path = Path(raw_path)
    else:
        raise typer.BadParameter(
            f"no raw_path for {accession}; pass --primary."
        )
    pipeline = EnrichPipeline(session_maker=AsyncSessionLocal, work_offline=offline)
    return await pipeline.run(accession_no=accession, primary_path=path)


@app.command("fetch-esef")
def fetch_esef(
    lei: str = typer.Option(None, help="20-char LEI of the EU issuer"),
    country: str = typer.Option(None, help="ISO 3166 alpha-2 country code (e.g. FR, DE)"),
    limit: int = typer.Option(1, help="Max filings to fetch"),
) -> None:
    """Discover an ESEF filing via filings.xbrl.org, download package, parse iXBRL."""
    if not lei and not country:
        raise typer.BadParameter("Provide --lei or --country")
    results = asyncio.run(_run_fetch_esef(lei, country, limit))
    if not results:
        console.print("[yellow]No filings matched.[/yellow]")
        raise typer.Exit(code=1)
    table = Table(title=f"ESEF ingest ({len(results)} filings)")
    for col in ("LEI", "Entity", "Accession", "Facts", "Canonical"):
        table.add_column(col)
    for r in results:
        table.add_row(
            r.lei,
            r.entity_name or "—",
            r.accession_no,
            str(r.facts_persisted or 0),
            str(r.canonical_rows),
        )
    console.print(table)


async def _run_fetch_esef(lei: str | None, country: str | None, limit: int):
    pipeline = ESEFIngestPipeline(session_maker=AsyncSessionLocal)
    if lei:
        return await pipeline.fetch_by_lei(lei=lei, limit=limit)
    return await pipeline.fetch_by_country(country=country, limit=limit)


@app.command("detect-restatements")
def detect_restatements(
    ticker: str = typer.Option(None, help="Ticker (resolves to CIK10)"),
    cik: str = typer.Option(None, help="Explicit CIK10"),
) -> None:
    """Cross-filing restatement detection for one entity."""
    if not ticker and not cik:
        raise typer.BadParameter("Provide --ticker or --cik")
    result = asyncio.run(_run_detect_restatements(ticker, cik))
    table = Table(title=f"Restatements: {result.cik10}")
    for col in ("pairs_examined", "restatements_written", "precision_only"):
        table.add_column(col)
    table.add_row(
        str(result.pairs_examined),
        str(result.restatements_written),
        str(result.precision_only),
    )
    console.print(table)


async def _run_detect_restatements(ticker: str | None, cik: str | None):
    client = SECClient()
    try:
        if cik:
            cik10 = cik.zfill(10)
        else:
            cik10 = await TickerResolver(client).resolve(ticker)
    finally:
        await client.aclose()
    return await detect_restatements_for_entity(AsyncSessionLocal, cik10)


if __name__ == "__main__":
    app()
