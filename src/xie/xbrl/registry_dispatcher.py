"""Maps an iXBRL `format` namespace URI to a Transform Registry version (TR3/TR4/TR5/TR6)."""
from __future__ import annotations

from enum import StrEnum

# Namespace URIs for each Transform Registry release. SEC filings have used TR1-5;
# TR5 is current (REC 2022-02-16). TR6 is a Public Working Draft 2024+ for future tracking.
NAMESPACES: dict[str, str] = {
    # TR1 (deprecated)
    "http://www.xbrl.org/inlineXBRL/transformation/2010-04-20": "TR1",
    # TR2 (deprecated)
    "http://www.xbrl.org/inlineXBRL/transformation/2011-07-31": "TR2",
    # TR3
    "http://www.xbrl.org/inlineXBRL/transformation/2015-02-26": "TR3",
    # TR4
    "http://www.xbrl.org/inlineXBRL/transformation/2020-02-12": "TR4",
    # TR5 (current REC)
    "http://www.xbrl.org/inlineXBRL/transformation/2022-02-16": "TR5",
    # TR6 PWD
    "http://www.xbrl.org/inlineXBRL/transformation/2024-12-04": "TR6",
}


# Convenience prefix-style aliases the SEC and EDGAR Filer Manual emit.
PREFIX_TO_TR = {
    "ixt": "TR3",  # default unprefixed/historic
    "ixt3": "TR3",
    "ixt4": "TR4",
    "ixt5": "TR5",
    "ixt6": "TR6",
}


class TRVersion(StrEnum):
    TR1 = "TR1"
    TR2 = "TR2"
    TR3 = "TR3"
    TR4 = "TR4"
    TR5 = "TR5"
    TR6 = "TR6"
    UNKNOWN = "UNKNOWN"


def tr_version_from_uri(namespace_uri: str | None) -> TRVersion:
    if not namespace_uri:
        return TRVersion.UNKNOWN
    label = NAMESPACES.get(namespace_uri.strip())
    return TRVersion(label) if label else TRVersion.UNKNOWN


def tr_version_from_prefix(prefix: str | None) -> TRVersion:
    if not prefix:
        return TRVersion.UNKNOWN
    label = PREFIX_TO_TR.get(prefix.lower())
    return TRVersion(label) if label else TRVersion.UNKNOWN
