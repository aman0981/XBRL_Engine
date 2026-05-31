"""SEC companyfacts API client + JSON parser.

Endpoint: data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json
Returns every XBRL-tagged numeric fact ever filed by the CIK, pre-parsed by SEC.

Shape:
  { "cik": 320193, "entityName": "Apple Inc.",
    "facts": {
      "us-gaap": { "Revenues": { "label": "...", "description": "...",
                                  "units": { "USD": [ {end, val, accn, fy, fp, form, filed, frame?}, ... ] } } },
      "dei":     { ... } } }
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Iterator

from xie.core.logging import get_logger

if TYPE_CHECKING:
    from xie.regulators.sec.client import SECClient

log = get_logger("xie.sec.companyfacts")

COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"


@dataclass(slots=True)
class CompanyFact:
    """One pre-parsed numeric fact from SEC companyfacts JSON."""

    qname: str                  # e.g. "us-gaap:Revenues"
    taxonomy: str               # "us-gaap" | "dei" | "ifrs-full" | "srt"
    concept: str                # e.g. "Revenues"
    unit: str                   # e.g. "USD" or "USD/shares"
    value: Decimal
    accession_no: str
    form: str
    filed: date
    fiscal_year: int | None
    fiscal_period: str | None   # FY | Q1 | Q2 | Q3 | CY (calendar)
    period_start: date | None   # set for duration concepts
    period_end: date | None     # set for duration concepts
    period_instant: date | None  # set for instant concepts
    frame: str | None           # e.g. "CY2024" or "CY2024Q3I"


@dataclass(slots=True)
class CompanyFactsPayload:
    cik10: str
    entity_name: str
    facts: list[CompanyFact]


def _parse_iso(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def parse_companyfacts(payload: dict) -> CompanyFactsPayload:
    """Turn the raw companyfacts JSON into a flat list of CompanyFact records.

    Concepts with no `start` are instants (e.g. balance-sheet items: Assets, Liabilities).
    Concepts with `start` and `end` are durations (e.g. Revenues, NetIncomeLoss).
    """
    raw_cik = payload.get("cik")
    cik10 = str(raw_cik).zfill(10) if raw_cik is not None else ""
    entity_name = payload.get("entityName", "")
    facts: list[CompanyFact] = []

    for taxonomy, concepts in (payload.get("facts") or {}).items():
        for concept, body in concepts.items():
            qname = f"{taxonomy}:{concept}"
            units = body.get("units") or {}
            for unit, rows in units.items():
                for row in rows:
                    facts.extend(
                        _row_to_fact(taxonomy, concept, qname, unit, row)
                    )
    log.info(
        "companyfacts_parsed", cik=cik10, entity=entity_name, facts=len(facts)
    )
    return CompanyFactsPayload(cik10=cik10, entity_name=entity_name, facts=facts)


def _row_to_fact(
    taxonomy: str, concept: str, qname: str, unit: str, row: dict
) -> Iterator[CompanyFact]:
    val = row.get("val")
    if val is None:
        return
    period_start = _parse_iso(row.get("start"))
    period_end = _parse_iso(row.get("end"))
    period_instant: date | None = None
    if period_start is None and period_end is not None:
        # instant concept: SEC encodes instant in `end` only.
        period_instant = period_end
        period_end = None
    yield CompanyFact(
        qname=qname,
        taxonomy=taxonomy,
        concept=concept,
        unit=unit,
        value=Decimal(str(val)),
        accession_no=str(row.get("accn", "")),
        form=str(row.get("form", "")),
        filed=_parse_iso(row.get("filed")) or date.min,
        fiscal_year=row.get("fy"),
        fiscal_period=row.get("fp"),
        period_start=period_start,
        period_end=period_end,
        period_instant=period_instant,
        frame=row.get("frame"),
    )


class CompanyFactsClient:
    """Fetches data.sec.gov/api/xbrl/companyfacts/CIK*.json."""

    def __init__(self, client: SECClient) -> None:
        self._client = client

    async def fetch(self, cik10: str) -> CompanyFactsPayload:
        url = COMPANYFACTS_URL.format(cik10=cik10)
        log.info("companyfacts_fetch", url=url)
        payload = await self._client.get_json(url)
        return parse_companyfacts(payload)
