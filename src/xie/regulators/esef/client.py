"""ESEF discovery via the `xbrl-filings-api` PyPI library.

filings.xbrl.org is a JSON:API hosted by XBRL International. Each filing's
`package_url` points to a Taxonomy Package ZIP (REC 2016-04-19) containing the
iXBRL report + the filer's extension taxonomy.

Library docs: https://lsalmela.github.io/xbrl-filings-api/

Library compatibility note: pinned to ==1.0 in requirements.txt. The mapping in
`_filing_to_hit` uses defensive `getattr(...)` with field-name fallbacks
because the library's attribute names have churned across releases
(e.g. `last_end_date` vs `reporting_date`, `xhtml_url` vs `inline_xbrl_url`).
"""
from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from datetime import date as date_t
from typing import Iterable
from urllib.parse import urlparse

import xbrl_filings_api as xf

from xie.core.config import settings
from xie.core.logging import get_logger

log = get_logger("xie.esef.client")


class UntrustedDownloadHost(ValueError):
    """SSRF guard: package_url host is not in the allowlist."""


# why: package_url is sourced from filings.xbrl.org JSON:API. If that API were
# ever compromised it could redirect us to attacker-controlled hosts. Restrict.
ALLOWED_DOWNLOAD_HOSTS = frozenset({
    "filings.xbrl.org",
})


def _check_host(url: str) -> None:
    host = urlparse(url).hostname or ""
    if host not in ALLOWED_DOWNLOAD_HOSTS:
        raise UntrustedDownloadHost(
            f"refusing to download from non-whitelisted host: {host!r}"
        )


@dataclass(slots=True)
class ESEFFilingHit:
    api_id: str
    lei: str | None
    entity_name: str | None
    country: str | None
    period_end: date_t | None
    fiscal_year: int | None
    package_url: str | None
    package_sha256: str | None
    viewer_url: str | None
    inline_xbrl_url: str | None


def _flat(value):
    """xbrl-filings-api fields are typed but coerce loosely; tolerate None / missing."""
    return value if value not in (None, "") else None


def _filing_to_hit(filing: xf.Filing) -> ESEFFilingHit:
    entity = getattr(filing, "entity", None)
    lei = _flat(getattr(entity, "identifier", None)) if entity is not None else None
    entity_name = _flat(getattr(entity, "name", None)) if entity is not None else None
    last_end = _flat(getattr(filing, "last_end_date", None)) or _flat(
        getattr(filing, "reporting_date", None)
    )
    fy = last_end.year if last_end is not None else None
    return ESEFFilingHit(
        api_id=str(filing.api_id),
        lei=lei,
        entity_name=entity_name,
        country=_flat(getattr(filing, "country", None)),
        period_end=last_end,
        fiscal_year=fy,
        package_url=_flat(getattr(filing, "package_url", None)),
        package_sha256=_flat(getattr(filing, "package_sha256", None)),
        viewer_url=_flat(getattr(filing, "viewer_url", None)),
        inline_xbrl_url=_flat(getattr(filing, "xhtml_url", None)),
    )


def _search_blocking(
    lei: str | None,
    country: str | None,
    limit: int,
) -> list[ESEFFilingHit]:
    filters: dict = {}
    if lei:
        filters["entity.identifier"] = lei
    if country:
        filters["country"] = country
    if not filters:
        raise ValueError("provide lei or country to scope the ESEF search")
    log.info("esef_search", filters=filters, limit=limit)
    filing_set = xf.get_filings(
        filters=filters,
        sort=["-last_end_date"],
        limit=limit,
        flags=xf.GET_ENTITY,
    )
    return [_filing_to_hit(f) for f in filing_set]


async def search_filings(
    lei: str | None = None,
    country: str | None = None,
    limit: int = 5,
) -> list[ESEFFilingHit]:
    """Run the (synchronous) xbrl-filings-api search off the event loop."""
    return await asyncio.to_thread(_search_blocking, lei, country, limit)


def _download_blocking(hit: ESEFFilingHit, target_path) -> int:
    if not hit.package_url:
        raise ValueError(f"filing {hit.api_id} has no package_url")
    _check_host(hit.package_url)

    import urllib.request

    target_path.parent.mkdir(parents=True, exist_ok=True)
    log.info("esef_download_start", api_id=hit.api_id, url=hit.package_url)
    headers = {"User-Agent": settings.sec_user_agent}
    req = urllib.request.Request(hit.package_url, headers=headers)
    # why: stream the response so a multi-hundred-MB ESEF package doesn't
    # buffer the entire body in RAM before hitting disk.
    with urllib.request.urlopen(req, timeout=120) as resp, open(target_path, "wb") as out:
        shutil.copyfileobj(resp, out, 1 << 20)  # 1 MiB chunks
    size = target_path.stat().st_size
    log.info("esef_download_done", api_id=hit.api_id, path=str(target_path), bytes=size)
    return size


async def download_package(hit: ESEFFilingHit, target_path) -> int:
    return await asyncio.to_thread(_download_blocking, hit, target_path)
