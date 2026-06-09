from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware

from xie.api.endpoints import companies, filings, health, metrics, uploads
from xie.core.config import settings
from xie.core.logging import configure_logging, get_logger

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

log = get_logger("xie.api")

_ERROR_HEADINGS = {
    404: "Page not found",
    413: "File too large",
    415: "Unsupported file type",
    429: "Too many requests",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    log = get_logger("xie.api")
    log.info("startup", app=settings.app_name, env=settings.env)
    yield
    log.info("shutdown")


app = FastAPI(title=settings.app_name, lifespan=lifespan)
# Compress CSS/JS/HTML on the wire (~70% smaller). Safe with the existing ETag
# revalidation; no effect on the dev asset bind-mount.
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.exception_handler(StarletteHTTPException)
async def _error_handler(request: Request, exc: StarletteHTTPException):
    """Styled HTML error page for browser GETs; JSON otherwise (API contract).

    Content-negotiated: only GET requests that accept text/html get the page.
    API clients and tests (Accept: */*) keep the default JSON error body.
    Server errors (>=500) are logged; client errors (4xx) are left to access logs.
    The handler must never itself raise, so HTML rendering falls back to JSON.
    """
    if exc.status_code >= 500:
        log.error(
            "http_error",
            status=exc.status_code,
            method=request.method,
            path=request.url.path,
            detail=str(exc.detail),
        )
    accept = request.headers.get("accept", "")
    if request.method == "GET" and "text/html" in accept:
        try:
            return _TEMPLATES.TemplateResponse(
                request,
                "error.html",
                {
                    "status": exc.status_code,
                    "heading": _ERROR_HEADINGS.get(exc.status_code, "Something went wrong"),
                    "detail": exc.detail,
                },
                status_code=exc.status_code,
            )
        except Exception:  # never let the error page itself 500 the response
            log.exception("error_page_render_failed", status=exc.status_code)
    return await http_exception_handler(request, exc)


app.include_router(health.router)
app.include_router(metrics.router)
app.include_router(companies.router)
app.include_router(filings.router)
app.include_router(uploads.router)
