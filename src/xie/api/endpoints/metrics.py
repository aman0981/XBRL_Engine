from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, generate_latest

router = APIRouter(tags=["metrics"])

registry = CollectorRegistry()

filings_ingested_total = Counter(
    "xie_filings_ingested_total",
    "Filings ingested",
    labelnames=("regulator", "mode", "status"),
    registry=registry,
)

facts_extracted_total = Counter(
    "xie_facts_extracted_total",
    "Facts extracted",
    labelnames=("regulator", "source"),
    registry=registry,
)

filing_parse_seconds = Histogram(
    "xie_filing_parse_seconds",
    "Filing parse latency",
    labelnames=("regulator", "mode"),
    registry=registry,
)


@router.get("/metrics")
def metrics() -> Response:
    return Response(content=generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
