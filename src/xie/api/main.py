from contextlib import asynccontextmanager

from fastapi import FastAPI

from xie.api.endpoints import companies, filings, health, metrics, uploads
from xie.core.config import settings
from xie.core.logging import configure_logging, get_logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    log = get_logger("xie.api")
    log.info("startup", app=settings.app_name, env=settings.env)
    yield
    log.info("shutdown")


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(health.router)
app.include_router(metrics.router)
app.include_router(companies.router)
app.include_router(filings.router)
app.include_router(uploads.router)
