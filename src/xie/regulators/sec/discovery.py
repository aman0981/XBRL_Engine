"""SEC EDGAR submissions discovery: paginated submissions JSON -> filing list."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_t
from typing import TYPE_CHECKING

from xie.core.logging import get_logger

if TYPE_CHECKING:
    from xie.regulators.sec.client import SECClient

log = get_logger("xie.sec.discovery")

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
PAGE_URL = "https://data.sec.gov/submissions/{filename}"


@dataclass(slots=True)
class FilingHit:
    cik10: str
    accession_no: str
    form_type: str
    filing_date: date_t
    period_of_report: date_t | None
    primary_document: str
    is_inline_xbrl: bool

    @property
    def accession_nodash(self) -> str:
        return self.accession_no.replace("-", "")

    def primary_document_url(self) -> str:
        return (
            f"https://www.sec.gov/Archives/edgar/data/"
            f"{int(self.cik10)}/{self.accession_nodash}/{self.primary_document}"
        )

    def index_json_url(self) -> str:
        return (
            f"https://www.sec.gov/Archives/edgar/data/"
            f"{int(self.cik10)}/{self.accession_nodash}/index.json"
        )


def _parse_date(value: str | None) -> date_t | None:
    if not value:
        return None
    return date_t.fromisoformat(value)


def _rows_to_hits(cik10: str, rows: dict) -> list[FilingHit]:
    """rows: parallel-array dict per SEC submissions schema."""
    accessions = rows.get("accessionNumber") or []
    forms = rows.get("form") or []
    filing_dates = rows.get("filingDate") or []
    periods = rows.get("reportDate") or []
    primaries = rows.get("primaryDocument") or []
    is_xbrl = rows.get("isInlineXBRL") or []

    out: list[FilingHit] = []
    for i, accession in enumerate(accessions):
        out.append(
            FilingHit(
                cik10=cik10,
                accession_no=accession,
                form_type=forms[i],
                filing_date=_parse_date(filing_dates[i]) or date_t.min,
                period_of_report=_parse_date(periods[i]),
                primary_document=primaries[i],
                is_inline_xbrl=bool(is_xbrl[i]) if i < len(is_xbrl) else False,
            )
        )
    return out


class SubmissionsDiscovery:
    """Walks data.sec.gov/submissions/CIK{cik10}.json including overflow pages."""

    def __init__(self, client: SECClient) -> None:
        self._client = client

    async def list_filings(
        self,
        cik10: str,
        forms: set[str] | None = None,
        limit: int | None = None,
    ) -> list[FilingHit]:
        url = SUBMISSIONS_URL.format(cik10=cik10)
        log.info("submissions_fetch", url=url)
        payload = await self._client.get_json(url)

        hits: list[FilingHit] = _rows_to_hits(cik10, payload.get("filings", {}).get("recent", {}))

        files = payload.get("filings", {}).get("files", []) or []
        for f in files:
            page_url = PAGE_URL.format(filename=f["name"])
            log.info("submissions_page_fetch", url=page_url)
            page = await self._client.get_json(page_url)
            hits.extend(_rows_to_hits(cik10, page))

        if forms is not None:
            hits = [h for h in hits if h.form_type in forms]
        hits.sort(key=lambda h: h.filing_date, reverse=True)
        if limit is not None:
            hits = hits[:limit]
        return hits
