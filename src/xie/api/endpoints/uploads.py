"""Mode C user-upload endpoints (Phase 7.5).

POST /uploads        multipart: a single iXBRL .xhtml/.htm OR an ESEF Taxonomy
                     Package .zip. 50 MB cap, ZIP/xHTML sniff, per-IP throttle.
GET  /uploads        landing page with drag-and-drop form + last 10 uploads.
GET  /uploads/{sha}  redirect to /filings/upload-<sha[:12]>/facts.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xie.api.rate_limit import RateLimitExceeded, SlidingWindowLimiter
from xie.core.config import settings
from xie.core.logging import get_logger
from xie.db.models import Filing
from xie.db.session import AsyncSessionLocal, get_session
from xie.ingest.upload_pipeline import (
    ACCESSION_SHA_PREFIX,
    UnsupportedUploadType,
    UploadPipeline,
    UploadTooLarge,
    _synth_accession,
)
from xie.regulators.esef.taxonomy_package import UnsafeZipMember, ZipBombDetected

log = get_logger("xie.api.uploads")
router = APIRouter(tags=["uploads"])

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Per-IP throttle. One process, one limiter.
_limiter = SlidingWindowLimiter(
    limit=settings.upload_rate_limit_per_hour,
    window_seconds=3600.0,
)


def _client_ip(request: Request) -> str:
    # uvicorn is launched with --proxy-headers; request.client.host already
    # reflects X-Forwarded-For when the request came through the trusted CIDR.
    return request.client.host if request.client else "unknown"


@router.get("/uploads", response_class=HTMLResponse)
async def uploads_index(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    stmt = (
        select(Filing)
        .where(Filing.regulator == "UPLOAD")
        .order_by(Filing.ingested_at.desc().nullslast())
        .limit(10)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return templates.TemplateResponse(
        request,
        "upload.html",
        {
            "recent": rows,
            "max_mb": settings.upload_max_bytes // (1024 * 1024),
            "rate_per_hour": settings.upload_rate_limit_per_hour,
            "ttl_hours": settings.upload_ttl_hours,
        },
    )


@router.post("/uploads")
async def upload_filing(
    request: Request,
    file: UploadFile = File(...),
) -> RedirectResponse:
    try:
        await _limiter.check_and_record(_client_ip(request))
    except RateLimitExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"upload rate limit ({settings.upload_rate_limit_per_hour}/hour) exceeded",
            headers={"Retry-After": str(int(exc.retry_after))},
        ) from exc

    # Early-reject on the Content-Length header so a multi-GB declared payload
    # never gets read. Doesn't catch chunked-encoding requests (no CL header).
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > settings.upload_max_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"Content-Length exceeds {settings.upload_max_bytes // (1024 * 1024)} MB cap",
                )
        except ValueError:
            # Malformed Content-Length — let body parsing handle it.
            pass

    # Stream the body in chunks; bail the moment we cross the cap so we never
    # buffer >50 MB in memory even if the client sends more.
    chunks: list[bytes] = []
    total = 0
    chunk_size = 1 << 20
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > settings.upload_max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"upload exceeds {settings.upload_max_bytes // (1024 * 1024)} MB cap",
            )
        chunks.append(chunk)
    payload = b"".join(chunks)

    pipeline = UploadPipeline(session_maker=AsyncSessionLocal)
    try:
        result = await pipeline.ingest(payload=payload, original_filename=file.filename)
    except UploadTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except UnsupportedUploadType as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except (UnsafeZipMember, ZipBombDetected) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("upload_failed", filename=file.filename)
        raise HTTPException(status_code=400, detail=f"upload failed: {exc!s}") from exc

    return RedirectResponse(
        url=f"/uploads/{result.sha256}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/uploads/{sha256}")
async def upload_detail(
    sha256: str,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    if not _looks_like_sha(sha256):
        raise HTTPException(status_code=400, detail="bad sha256")
    accession = _synth_accession(sha256)
    stmt = (
        select(Filing.accession_no)
        .where(Filing.regulator == "UPLOAD", Filing.accession_no == accession)
        .limit(1)
    )
    found = (await session.execute(stmt)).scalar_one_or_none()
    if not found:
        raise HTTPException(status_code=404, detail="upload not found or TTL-expired")
    return RedirectResponse(
        url=f"/filings/{found}/facts", status_code=status.HTTP_302_FOUND
    )


def _looks_like_sha(token: str) -> bool:
    return len(token) == 64 and all(c in "0123456789abcdef" for c in token.lower())
