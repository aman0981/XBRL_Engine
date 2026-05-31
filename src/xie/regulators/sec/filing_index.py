"""SEC filing document-set fetcher: index.json + downloads, sha256 manifest."""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from xie.core.logging import get_logger

if TYPE_CHECKING:
    from xie.regulators.sec.client import SECClient

log = get_logger("xie.sec.index")


@dataclass(slots=True)
class FilingDocument:
    name: str
    type: str
    size: int
    url: str
    sha256: str
    local_path: Path


@dataclass(slots=True)
class FilingBundle:
    accession_no: str
    accession_nodash: str
    cik10: str
    base_url: str
    local_dir: Path
    documents: list[FilingDocument]
    primary_document: str

    @property
    def primary_path(self) -> Path:
        return self.local_dir / self.primary_document

    @property
    def primary_sha256(self) -> str | None:
        for doc in self.documents:
            if doc.name == self.primary_document:
                return doc.sha256
        return None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _index_url(cik10: str, accession_nodash: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik10)}/{accession_nodash}/index.json"


def _archive_base(cik10: str, accession_nodash: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik10)}/{accession_nodash}"


class FilingIndexFetcher:
    """Fetches a filing's index.json, downloads documents, persists sha256 manifest."""

    def __init__(self, client: SECClient, raw_root: Path) -> None:
        self._client = client
        self._raw_root = raw_root

    async def fetch(
        self,
        cik10: str,
        accession_no: str,
        primary_document: str,
    ) -> FilingBundle:
        accession_nodash = accession_no.replace("-", "")
        base = _archive_base(cik10, accession_nodash)
        target_dir = self._raw_root / "sec" / cik10 / accession_nodash
        target_dir.mkdir(parents=True, exist_ok=True)

        index = await self._client.get_json(_index_url(cik10, accession_nodash))
        items = index.get("directory", {}).get("item", [])

        documents: list[FilingDocument] = []
        for item in items:
            if item.get("type") == "folder":
                continue
            name = item["name"]
            url = f"{base}/{name}"
            local_path = target_dir / name
            sha = await self._download_one(url, local_path)
            documents.append(
                FilingDocument(
                    name=name,
                    type=item.get("type", ""),
                    size=int(item.get("size", 0) or 0),
                    url=url,
                    sha256=sha,
                    local_path=local_path,
                )
            )

        bundle = FilingBundle(
            accession_no=accession_no,
            accession_nodash=accession_nodash,
            cik10=cik10,
            base_url=base,
            local_dir=target_dir,
            documents=documents,
            primary_document=primary_document,
        )
        self._write_manifest(bundle)
        log.info(
            "filing_bundle_downloaded",
            accession=accession_no,
            cik=cik10,
            files=len(documents),
        )
        return bundle

    async def _download_one(self, url: str, local_path: Path) -> str:
        if local_path.exists():
            data = local_path.read_bytes()
            return _sha256(data)
        data = await self._client.get_bytes(url)
        local_path.write_bytes(data)
        return _sha256(data)

    @staticmethod
    def _write_manifest(bundle: FilingBundle) -> None:
        manifest = {
            "accession_no": bundle.accession_no,
            "cik10": bundle.cik10,
            "primary_document": bundle.primary_document,
            "documents": [
                {"name": d.name, "type": d.type, "size": d.size, "sha256": d.sha256}
                for d in bundle.documents
            ],
        }
        (bundle.local_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
