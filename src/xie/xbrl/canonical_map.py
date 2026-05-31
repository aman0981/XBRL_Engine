"""Regulator-aware canonical line-item mapping.

Phase 1.5 scope: SEC US-GAAP ordered fallback chains for 5 canonical names.
Calc-linkbase-aware variants (e.g. "Revenues vs RevenueFromContractWithCustomerExcludingAssessedTax")
land in Phase 4 once linkbase ingestion exists. IFRS chains land in Phase 6.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Canonical(StrEnum):
    REVENUE = "Revenue"
    NET_INCOME = "NetIncome"
    TOTAL_ASSETS = "TotalAssets"
    OPERATING_CASH_FLOW = "OperatingCashFlow"
    EPS = "EPS"


@dataclass(frozen=True, slots=True)
class FallbackEntry:
    qname: str
    confidence: float
    method: str = "fallback_chain"


# Order matters: first match wins, highest preference at the top.
# Confidence reflects "how reliably this qname captures the canonical concept".
SEC_FALLBACKS: dict[Canonical, list[FallbackEntry]] = {
    Canonical.REVENUE: [
        FallbackEntry("us-gaap:Revenues", 0.98),
        FallbackEntry("us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", 0.97),
        FallbackEntry("us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax", 0.95),
        FallbackEntry("us-gaap:SalesRevenueNet", 0.92),
        FallbackEntry("us-gaap:SalesRevenueGoodsNet", 0.85),
    ],
    Canonical.NET_INCOME: [
        FallbackEntry("us-gaap:NetIncomeLoss", 0.99),
        FallbackEntry("us-gaap:ProfitLoss", 0.95),
        FallbackEntry("us-gaap:NetIncomeLossAvailableToCommonStockholdersBasic", 0.85),
    ],
    Canonical.TOTAL_ASSETS: [
        FallbackEntry("us-gaap:Assets", 0.99),
    ],
    Canonical.OPERATING_CASH_FLOW: [
        FallbackEntry("us-gaap:NetCashProvidedByUsedInOperatingActivities", 0.98),
        FallbackEntry(
            "us-gaap:NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
            0.92,
        ),
    ],
    Canonical.EPS: [
        FallbackEntry("us-gaap:EarningsPerShareDiluted", 0.97),
        FallbackEntry("us-gaap:EarningsPerShareBasic", 0.95),
    ],
}


# IFRS fallback chains for ESEF filers. IFRS 18 (2025+) introduces new presentation
# categories but the underlying concept qnames remain in ifrs-full namespace.
IFRS_FALLBACKS: dict[Canonical, list[FallbackEntry]] = {
    Canonical.REVENUE: [
        FallbackEntry("ifrs-full:Revenue", 0.98),
        FallbackEntry("ifrs-full:RevenueFromContractsWithCustomers", 0.97),
        FallbackEntry("ifrs-full:RevenueFromSaleOfGoods", 0.85),
    ],
    Canonical.NET_INCOME: [
        FallbackEntry("ifrs-full:ProfitLoss", 0.99),
        FallbackEntry("ifrs-full:ProfitLossAttributableToOwnersOfParent", 0.95),
    ],
    Canonical.TOTAL_ASSETS: [
        FallbackEntry("ifrs-full:Assets", 0.99),
    ],
    Canonical.OPERATING_CASH_FLOW: [
        FallbackEntry("ifrs-full:CashFlowsFromUsedInOperatingActivities", 0.98),
        FallbackEntry("ifrs-full:NetCashFlowsFromUsedInOperatingActivities", 0.95),
    ],
    Canonical.EPS: [
        FallbackEntry("ifrs-full:DilutedEarningsLossPerShare", 0.97),
        FallbackEntry("ifrs-full:BasicEarningsLossPerShare", 0.95),
    ],
}


REGULATOR_FALLBACKS: dict[str, dict[Canonical, list[FallbackEntry]]] = {
    "SEC": SEC_FALLBACKS,
    "ESEF": IFRS_FALLBACKS,
}


def find_canonical(
    qname: str, regulator: str = "SEC"
) -> tuple[Canonical, FallbackEntry] | None:
    """Reverse-lookup: which canonical does this qname satisfy?

    Returns (Canonical, entry) so the caller knows the assigned confidence + method.
    None if the qname isn't in any fallback chain for the given regulator.
    """
    chains = REGULATOR_FALLBACKS.get(regulator, SEC_FALLBACKS)
    for canonical, chain in chains.items():
        for entry in chain:
            if entry.qname == qname:
                return canonical, entry
    return None


def fallback_qnames(canonical: Canonical, regulator: str = "SEC") -> list[str]:
    chains = REGULATOR_FALLBACKS.get(regulator, SEC_FALLBACKS)
    return [e.qname for e in chains[canonical]]
