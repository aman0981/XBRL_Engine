"""ESEF Taxonomy Package (REC 2016-04-19) unpacker.

A taxonomy package is a ZIP with a `META-INF/taxonomyPackage.xml` manifest plus
the iXBRL report (xHTML) and the filer's extension taxonomy. We unzip, read the
manifest to locate the entry-point `*.xsd`, then return paths to the primary
iXBRL document and the extension schema.
"""
from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

from xie.core.config import settings
from xie.core.logging import get_logger

log = get_logger("xie.esef.taxonomy_package")

NS = {
    "tp": "http://xbrl.org/2016/taxonomy-package",
}


@dataclass(slots=True)
class UnpackedPackage:
    root_dir: Path
    primary_ixbrl: Path | None
    extension_xsd: Path | None
    manifest_xml: Path | None
    entry_points: list[str]


class UnsafeZipMember(ValueError):
    """A package ZIP contains a member whose path would escape the target dir."""


class ZipBombDetected(ValueError):
    """A package ZIP would decompress to more bytes than the configured ceiling."""


def unzip_package(
    zip_path: Path,
    target_dir: Path,
    max_uncompressed_bytes: int | None = None,
) -> UnpackedPackage:
    cap = max_uncompressed_bytes if max_uncompressed_bytes is not None else settings.upload_max_unzipped_bytes
    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        _validate_members(zf, target_dir)
        _validate_uncompressed_size(zf, cap)
        zf.extractall(target_dir)
    manifest = _find_manifest(target_dir)
    entry_points = _parse_entry_points(manifest) if manifest else []
    primary = _find_primary_ixbrl(target_dir)
    extension = _find_extension_xsd(target_dir)
    log.info(
        "esef_package_unzipped",
        zip=str(zip_path),
        primary=str(primary) if primary else None,
        extension=str(extension) if extension else None,
        entry_points=entry_points,
    )
    return UnpackedPackage(
        root_dir=target_dir,
        primary_ixbrl=primary,
        extension_xsd=extension,
        manifest_xml=manifest,
        entry_points=entry_points,
    )


def _validate_uncompressed_size(zf: zipfile.ZipFile, cap_bytes: int) -> None:
    """Zip-bomb guard.

    Sums each member's declared `file_size` from the ZIP central directory.
    `file_size` is the uncompressed size of one entry; if any single entry or
    the running total exceeds the cap, abort before extract.
    """
    total = 0
    for member in zf.infolist():
        size = member.file_size
        if size < 0:
            raise ZipBombDetected(
                f"package member {member.filename!r} has negative declared size {size}"
            )
        if size > cap_bytes:
            raise ZipBombDetected(
                f"package member {member.filename!r} declares {size} bytes uncompressed; "
                f"per-member cap is {cap_bytes}"
            )
        total += size
        if total > cap_bytes:
            raise ZipBombDetected(
                f"package would decompress to >{total} bytes; cap is {cap_bytes}"
            )


def _validate_members(zf: zipfile.ZipFile, target_dir: Path) -> None:
    """Reject zip-slip: any member whose resolved path would escape target_dir.

    CPython 3.6+ ZipFile.extractall already strips leading '/' and '..' segments,
    but symlink members and crafted relative paths can still slip past on some
    platforms. Belt-and-braces: resolve each member and verify it lives under
    target_dir.
    """
    base = target_dir.resolve()
    for member in zf.infolist():
        resolved = (target_dir / member.filename).resolve()
        try:
            resolved.relative_to(base)
        except ValueError as e:
            raise UnsafeZipMember(
                f"package member {member.filename!r} escapes target {base}"
            ) from e
        # Symlink members in a ZipFile carry the link target in their `extra`
        # field; refuse all of them outright.
        if member.create_system == 3 and (member.external_attr >> 16) & 0o120000 == 0o120000:
            raise UnsafeZipMember(f"package member {member.filename!r} is a symlink")


def _find_manifest(root: Path) -> Path | None:
    for candidate in root.rglob("taxonomyPackage.xml"):
        if candidate.parent.name == "META-INF":
            return candidate
    return None


def _find_primary_ixbrl(root: Path) -> Path | None:
    """Primary iXBRL = top-level reports/*.xhtml under the package root."""
    for ext in ("xhtml", "htm", "html"):
        for candidate in root.rglob(f"reports/**/*.{ext}"):
            return candidate
        for candidate in root.rglob(f"*.{ext}"):
            text = ""
            try:
                with candidate.open("r", encoding="utf-8", errors="ignore") as f:
                    text = f.read(2048)
            except OSError:
                continue
            if "xbrl.org/2013/inlineXBRL" in text:
                return candidate
    return None


def _find_extension_xsd(root: Path) -> Path | None:
    for candidate in root.rglob("*.xsd"):
        return candidate
    return None


def _parse_entry_points(manifest_path: Path) -> list[str]:
    try:
        tree = etree.parse(
            str(manifest_path),
            parser=etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False),
        )
    except (etree.XMLSyntaxError, OSError):
        return []
    out: list[str] = []
    for doc in tree.findall(".//tp:entryPointDocument", namespaces=NS):
        href = doc.get("href")
        if href:
            out.append(href)
    return out
