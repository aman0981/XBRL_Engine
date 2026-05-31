"""TTL sweep for Mode C user uploads (Phase 7.5).

Deletes:
  - `filings` rows (and CASCADEs to facts / contexts / units / canonical /
    fact_dimensions / fact_reconciliation / restatements / dqc_findings)
    where `regulator='UPLOAD'` and `ingested_at < now - XIE_UPLOAD_TTL_HOURS`.
  - The matching `data/raw/uploads/{sha256}/` directories on disk.

Schedule via cron:
    0 * * * * /opt/venv/bin/python -m scripts.cleanup_uploads

For local dev:
    python -m scripts.cleanup_uploads
"""
from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, select

from xie.core.config import settings
from xie.core.logging import configure_logging, get_logger
from xie.db.models import Filing
from xie.db.session import AsyncSessionLocal

log = get_logger("xie.scripts.cleanup_uploads")


async def sweep() -> dict[str, int]:
    """Delete expired uploads + orphan blobs. Returns counters for logging.

    Steps:
      1. Single DELETE ... RETURNING raw_sha256 — one round-trip, gives us
         the sha list for the file sweep.
      2. rmtree each returned sha dir.
      3. Orphan sweep: scan data/raw/uploads/, drop any subdir whose sha
         doesn't match a live UPLOAD row (catches parse-failure rollbacks
         that may have raced with this job).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.upload_ttl_hours)
    uploads_root = settings.raw_data_dir / "uploads"

    async with AsyncSessionLocal() as session:
        stmt = (
            delete(Filing)
            .where(
                Filing.regulator == "UPLOAD",
                Filing.ingested_at < cutoff,
            )
            .returning(Filing.raw_sha256)
        )
        shas = [row for (row,) in (await session.execute(stmt)).all() if row]
        await session.commit()

    deleted_dirs = 0
    for sha in shas:
        d = uploads_root / sha
        if d.is_dir():
            try:
                shutil.rmtree(d)
                deleted_dirs += 1
            except OSError as e:
                log.warning("cleanup_dir_failed", path=str(d), err=str(e))

    orphans = await _sweep_orphans(uploads_root)
    return {"rows": len(shas), "dirs": deleted_dirs, "orphans": orphans}


async def _sweep_orphans(uploads_root: Path) -> int:
    """Drop on-disk dirs whose sha doesn't match any UPLOAD row in the DB."""
    if not uploads_root.is_dir():
        return 0
    on_disk = {p.name for p in uploads_root.iterdir() if p.is_dir()}
    if not on_disk:
        return 0

    async with AsyncSessionLocal() as session:
        stmt = select(Filing.raw_sha256).where(Filing.regulator == "UPLOAD")
        in_db = {s for (s,) in (await session.execute(stmt)).all() if s}

    removed = 0
    for sha in on_disk - in_db:
        try:
            shutil.rmtree(uploads_root / sha)
            removed += 1
        except OSError as e:
            log.warning("orphan_cleanup_failed", sha=sha, err=str(e))
    return removed


def main() -> None:
    configure_logging()
    result = asyncio.run(sweep())
    log.info("upload_ttl_sweep_done", **result)


if __name__ == "__main__":
    main()
