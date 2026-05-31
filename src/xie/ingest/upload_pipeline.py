"""Mode C — user-uploaded iXBRL or ESEF Taxonomy Package.

Flow:
  1. Reject if size > XIE_UPLOAD_MAX_BYTES (50 MB default).
  2. Sniff first 16 bytes: `PK\\x03\\x04` -> ZIP; `<html` / `<?xml` -> xHTML;
     anything else -> 415.
  3. SHA-256 the bytes; persist to `data/raw/uploads/{sha256}/`.
  4. Synthesize `accession_no = 'upload-<sha256[:12]>'`; upsert a Filing row
     with `regulator='UPLOAD'`, `ingestion_mode='user_upload'`.
  5. Route to ModeBPipeline.parse_and_persist on the primary iXBRL (the file
     itself for xHTML; the unpacked primary for ZIP).
"""
from __future__ import annotations

import asyncio
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from xie.core.config import settings
from xie.core.hashing import sha256_file
from xie.core.logging import get_logger
from xie.db.models import Filing
from xie.ingest.modeb_pipeline import ModeBPipeline
from xie.regulators.esef.taxonomy_package import unzip_package

log = get_logger("xie.ingest.upload")


class UploadKind(StrEnum):
    IXBRL = "ixbrl"
    PACKAGE = "package"


class UnsupportedUploadType(ValueError):
    """First-bytes sniff didn't match any supported iXBRL container."""


class UploadTooLarge(ValueError):
    """Upload bytes exceed XIE_UPLOAD_MAX_BYTES."""


_ZIP_MAGIC = b"PK\x03\x04"
_SNIFF_BYTES = 64


def sniff(head: bytes) -> UploadKind:
    """Decide whether the upload is a Taxonomy Package ZIP or raw iXBRL xHTML."""
    if head[:4] == _ZIP_MAGIC:
        return UploadKind.PACKAGE
    lowered = head[:_SNIFF_BYTES].lstrip().lower()
    if lowered.startswith(b"<?xml") or lowered.startswith(b"<html") or lowered.startswith(
        b"<!doctype"
    ):
        return UploadKind.IXBRL
    raise UnsupportedUploadType(
        "first bytes match neither ZIP magic nor xHTML/XML prolog"
    )


@dataclass(slots=True)
class UploadIngestResult:
    sha256: str
    accession_no: str
    filing_id: int
    kind: str
    primary_path: str
    contexts: int
    units: int
    facts: int


class UploadPipeline:
    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self._session_maker = session_maker
        self._modeb = ModeBPipeline(session_maker=session_maker)
        self._uploads_root = settings.raw_data_dir / "uploads"

    async def ingest(
        self, payload: bytes, original_filename: str | None
    ) -> UploadIngestResult:
        if len(payload) > settings.upload_max_bytes:
            raise UploadTooLarge(
                f"upload is {len(payload)} bytes; cap is {settings.upload_max_bytes}"
            )
        kind = sniff(payload)

        # Persist before parsing so a parse failure still leaves the user's
        # file on disk for follow-up debugging.
        import hashlib

        sha = hashlib.sha256(payload).hexdigest()
        target_dir = self._uploads_root / sha
        target_dir.mkdir(parents=True, exist_ok=True)
        if kind is UploadKind.PACKAGE:
            blob_path = target_dir / "package.zip"
        else:
            suffix = ".xhtml" if original_filename is None else Path(original_filename).suffix or ".xhtml"
            blob_path = target_dir / f"primary{suffix}"
        if not blob_path.exists():
            blob_path.write_bytes(payload)

        # why: every step after blob persist can fail (unsafe zip, parse
        # error, DB outage). On any failure, sweep the on-disk dir so the
        # TTL job doesn't have to (and so the orphan never holds bytes
        # against the volume quota).
        try:
            primary_path = await asyncio.to_thread(
                _resolve_primary, kind, blob_path, target_dir
            )
            accession_no = _synth_accession(sha)
            filing_id = await self._upsert_filing(
                accession=accession_no,
                sha=sha,
                primary_path=primary_path,
                original_filename=original_filename,
            )
            modeb = await self._modeb.parse_and_persist(
                accession_no=accession_no, primary_path=primary_path
            )
        except Exception:
            shutil.rmtree(target_dir, ignore_errors=True)
            log.warning("upload_rolled_back_due_to_error", sha=sha)
            raise

        result = UploadIngestResult(
            sha256=sha,
            accession_no=accession_no,
            filing_id=filing_id,
            kind=kind.value,
            primary_path=str(primary_path),
            contexts=modeb.contexts_upserted,
            units=modeb.units_upserted,
            facts=modeb.facts_upserted,
        )
        log.info("upload_ingested", **asdict(result))
        return result

    async def _upsert_filing(
        self,
        accession: str,
        sha: str,
        primary_path: Path,
        original_filename: str | None,
    ) -> int:
        values = {
            "regulator": "UPLOAD",
            "entity_id": _synth_accession(sha),  # no LEI / CIK; use the synthetic id
            "ticker": None,
            "accession_no": accession,
            "form_type": "USER_UPLOAD",
            "filing_date": None,
            "period_of_report": None,
            "fiscal_year": None,
            "fiscal_period": None,
            "ixbrl_url": original_filename,
            "raw_sha256": sha,
            "raw_path": str(primary_path),
            "ingestion_mode": "user_upload",
            "ingestion_status": "raw_uploaded",
            "ingested_at": datetime.now(timezone.utc),
        }
        async with self._session_maker() as session:
            stmt = (
                pg_insert(Filing)
                .values(**values)
                .on_conflict_do_update(
                    constraint="uq_filings_regulator_accession",
                    set_={
                        "raw_path": values["raw_path"],
                        "raw_sha256": values["raw_sha256"],
                        "ingestion_status": values["ingestion_status"],
                        "ingested_at": values["ingested_at"],
                    },
                )
                .returning(Filing.id)
            )
            row_id = (await session.execute(stmt)).scalar_one()
            await session.commit()
            return row_id


ACCESSION_SHA_PREFIX = 16  # 16 hex chars = 64 bits of entropy; well under 30-char column


def _synth_accession(sha256: str) -> str:
    return f"upload-{sha256[:ACCESSION_SHA_PREFIX]}"


def _resolve_primary(kind: UploadKind, blob_path: Path, target_dir: Path) -> Path:
    if kind is UploadKind.IXBRL:
        return blob_path
    unpacked_dir = target_dir / "unpacked"
    pkg = unzip_package(blob_path, unpacked_dir)
    if pkg.primary_ixbrl is None:
        raise UnsupportedUploadType(
            "uploaded ZIP is a taxonomy package but contains no primary iXBRL report"
        )
    return pkg.primary_ixbrl
