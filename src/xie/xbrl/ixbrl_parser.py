"""iXBRL extraction with hardened lxml.

Parses an iXBRL xHTML document into typed records:
  - ParsedContext (one per xbrli:context)
  - ParsedUnit (one per xbrli:unit)
  - ParsedFact (ix:nonFraction / ix:nonNumeric, hidden + continuation joined)
  - DTSReference (link:schemaRef + link:linkbaseRef pointers for Arelle in Phase 3)

The parser does NOT touch the DB. The pipeline module handles persistence.

Hardening (from plan Security section):
  - resolve_entities=False  -- block XXE
  - no_network=True         -- prevent SSRF
  - huge_tree=False         -- bound memory
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from lxml import etree

from xie.core.logging import get_logger
from xie.xbrl.registry_dispatcher import TRVersion, tr_version_from_uri
from xie.xbrl.transforms import TransformError, apply_scale_and_sign, transform_value

log = get_logger("xie.xbrl.parser")


# --- Namespaces -----------------------------------------------------------
# Frozen map; XPath without prefix bindings returns no nodes silently — common pitfall.
NS = {
    "xhtml": "http://www.w3.org/1999/xhtml",
    "ix": "http://www.xbrl.org/2013/inlineXBRL",
    "xbrli": "http://www.xbrl.org/2003/instance",
    "xbrldi": "http://xbrl.org/2006/xbrldi",
    "link": "http://www.xbrl.org/2003/linkbase",
    "xlink": "http://www.w3.org/1999/xlink",
    "iso4217": "http://www.xbrl.org/2003/iso4217",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}


# --- Records --------------------------------------------------------------


@dataclass(slots=True)
class ParsedDimension:
    axis_qname: str
    member_qname: str | None
    typed_value: str | None


@dataclass(slots=True)
class ParsedContext:
    context_ref: str
    entity_id: str | None
    period_start: date | None
    period_end: date | None
    period_instant: date | None
    dimensions: list[ParsedDimension] = field(default_factory=list)

    @property
    def dimensions_jsonb(self) -> dict:
        out: dict[str, object] = {}
        for d in self.dimensions:
            if d.member_qname is not None:
                out[d.axis_qname] = d.member_qname
            else:
                out[d.axis_qname] = {"typedValue": d.typed_value}
        return out

    @property
    def dimensions_hash(self) -> str:
        canonical = json.dumps(self.dimensions_jsonb, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class ParsedUnit:
    unit_ref: str
    numerator_measures: list[str]
    denominator_measures: list[str] | None


@dataclass(slots=True)
class ParsedFact:
    concept_qname: str
    context_ref: str
    unit_ref: str | None
    value_numeric: Decimal | None
    value_text: str | None
    decimals_text: str | None
    decimals_inf: bool
    scale: int | None
    format_qname: str | None
    transform_registry: str | None
    is_hidden: bool
    is_dimensional: bool
    xml_lang: str | None
    footnote_text: str | None


@dataclass(slots=True)
class DTSReference:
    schema_refs: list[str]
    linkbase_refs: list[str]


@dataclass(slots=True)
class ParsedFiling:
    contexts: list[ParsedContext]
    units: list[ParsedUnit]
    facts: list[ParsedFact]
    dts: DTSReference


# --- Parser entry point ---------------------------------------------------


def parse_ixbrl(path: Path) -> ParsedFiling:
    """Parse a single primary iXBRL .htm file into typed records."""
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        huge_tree=False,
        load_dtd=False,
        remove_blank_text=False,
    )
    tree = etree.parse(str(path), parser)
    root = tree.getroot()

    contexts = _extract_contexts(root)
    units = _extract_units(root)
    facts = _extract_facts(root, contexts_by_ref={c.context_ref: c for c in contexts})
    dts = _extract_dts_refs(root)
    log.info(
        "ixbrl_parsed",
        path=str(path),
        contexts=len(contexts),
        units=len(units),
        facts=len(facts),
        schemaRefs=len(dts.schema_refs),
        linkbaseRefs=len(dts.linkbase_refs),
    )
    return ParsedFiling(contexts=contexts, units=units, facts=facts, dts=dts)


# --- Internals ------------------------------------------------------------


def _local(elem: etree._Element) -> str:
    return etree.QName(elem.tag).localname


def _ns_uri(elem: etree._Element) -> str:
    return etree.QName(elem.tag).namespace or ""


def _q(prefix: str, local: str) -> str:
    return f"{{{NS[prefix]}}}{local}"


def _attr(elem: etree._Element, name: str) -> str | None:
    return elem.get(name)


def _xlink_href(elem: etree._Element) -> str | None:
    return elem.get(_q("xlink", "href"))


def _text_content_joined(elem: etree._Element, root: etree._Element) -> str:
    """Inline text of a fact, including ix:continuation chains.

    `ix:continuation` is referenced via the fact's `continuedAt` attribute, which
    points to the @id of the next continuation element. Recurse until none.
    """
    parts: list[str] = []
    parts.append("".join(elem.itertext()).strip())
    cont_id = elem.get("continuedAt")
    while cont_id:
        nxt = root.find(f".//ix:continuation[@id='{cont_id}']", namespaces=NS)
        if nxt is None:
            break
        parts.append("".join(nxt.itertext()).strip())
        cont_id = nxt.get("continuedAt")
    return " ".join(p for p in parts if p)


def _qname_of_concept(elem: etree._Element, root: etree._Element) -> str:
    """ix:nonFraction / ix:nonNumeric `name` attribute carries the prefixed concept qname.

    Resolve the prefix using the element's own nsmap (lxml exposes it via .nsmap).
    """
    raw = elem.get("name", "")
    if ":" not in raw:
        return raw
    prefix, local = raw.split(":", 1)
    uri = elem.nsmap.get(prefix)
    if not uri:
        # fall back to inherited nsmap on the root
        uri = root.nsmap.get(prefix)
    if not uri:
        return raw
    return f"{prefix}:{local}"


def _parse_iso(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value[:10])


def _extract_contexts(root: etree._Element) -> list[ParsedContext]:
    out: list[ParsedContext] = []
    for ctx in root.iterfind(f".//{_q('xbrli', 'context')}"):
        period = ctx.find(_q("xbrli", "period"))
        period_start: date | None = None
        period_end: date | None = None
        period_instant: date | None = None
        if period is not None:
            start = period.find(_q("xbrli", "startDate"))
            end = period.find(_q("xbrli", "endDate"))
            instant = period.find(_q("xbrli", "instant"))
            if instant is not None and instant.text:
                period_instant = _parse_iso(instant.text)
            elif start is not None and end is not None:
                period_start = _parse_iso(start.text)
                period_end = _parse_iso(end.text)
        entity = ctx.find(_q("xbrli", "entity"))
        entity_id: str | None = None
        if entity is not None:
            ident = entity.find(_q("xbrli", "identifier"))
            if ident is not None and ident.text:
                entity_id = ident.text.strip()

        dims = _extract_context_dimensions(ctx)

        out.append(
            ParsedContext(
                context_ref=ctx.get("id", ""),
                entity_id=entity_id,
                period_start=period_start,
                period_end=period_end,
                period_instant=period_instant,
                dimensions=dims,
            )
        )
    return out


def _extract_context_dimensions(ctx: etree._Element) -> list[ParsedDimension]:
    """xbrli:scenario or xbrli:segment children carry xbrldi:explicitMember / typedMember."""
    dims: list[ParsedDimension] = []
    for container_name in ("segment", "scenario"):
        container = ctx.find(f".//{_q('xbrli', container_name)}")
        if container is None:
            continue
        for child in container:
            tag = etree.QName(child.tag)
            if tag.localname == "explicitMember":
                axis = child.get("dimension", "")
                member = (child.text or "").strip()
                dims.append(
                    ParsedDimension(
                        axis_qname=axis, member_qname=member or None, typed_value=None
                    )
                )
            elif tag.localname == "typedMember":
                axis = child.get("dimension", "")
                typed_value = "".join(child.itertext()).strip()
                dims.append(
                    ParsedDimension(
                        axis_qname=axis, member_qname=None, typed_value=typed_value or None
                    )
                )
    return dims


def _extract_units(root: etree._Element) -> list[ParsedUnit]:
    out: list[ParsedUnit] = []
    for unit in root.iterfind(f".//{_q('xbrli', 'unit')}"):
        measures = [
            (m.text or "").strip()
            for m in unit.iterfind(_q("xbrli", "measure"))
        ]
        divide = unit.find(_q("xbrli", "divide"))
        if divide is not None:
            num_box = divide.find(_q("xbrli", "unitNumerator"))
            den_box = divide.find(_q("xbrli", "unitDenominator"))
            numerator = (
                [
                    (m.text or "").strip()
                    for m in num_box.iterfind(_q("xbrli", "measure"))
                ]
                if num_box is not None
                else []
            )
            denominator = (
                [
                    (m.text or "").strip()
                    for m in den_box.iterfind(_q("xbrli", "measure"))
                ]
                if den_box is not None
                else None
            )
            out.append(
                ParsedUnit(
                    unit_ref=unit.get("id", ""),
                    numerator_measures=numerator,
                    denominator_measures=denominator,
                )
            )
        else:
            out.append(
                ParsedUnit(
                    unit_ref=unit.get("id", ""),
                    numerator_measures=measures,
                    denominator_measures=None,
                )
            )
    return out


def _extract_facts(
    root: etree._Element, contexts_by_ref: dict[str, ParsedContext]
) -> list[ParsedFact]:
    out: list[ParsedFact] = []

    # All ix:nonFraction nodes anywhere in the tree (visible + within ix:hidden).
    for elem in root.iterfind(f".//{_q('ix', 'nonFraction')}"):
        out.append(_parse_non_fraction(elem, root, contexts_by_ref))
    for elem in root.iterfind(f".//{_q('ix', 'nonNumeric')}"):
        out.append(_parse_non_numeric(elem, root, contexts_by_ref))
    return out


def _is_hidden(elem: etree._Element) -> bool:
    parent = elem.getparent()
    while parent is not None:
        if etree.QName(parent.tag).localname == "hidden":
            return True
        parent = parent.getparent()
    return False


def _parse_non_fraction(
    elem: etree._Element, root: etree._Element, contexts_by_ref: dict[str, ParsedContext]
) -> ParsedFact:
    qname = _qname_of_concept(elem, root)
    context_ref = elem.get("contextRef", "")
    unit_ref = elem.get("unitRef")
    raw_text = "".join(elem.itertext()).strip()

    format_qname_raw = elem.get("format")  # e.g. "ixt:numdotdecimal"
    format_prefix: str | None = None
    format_local: str = ""
    transform_registry: str | None = None
    if format_qname_raw:
        if ":" in format_qname_raw:
            format_prefix, format_local = format_qname_raw.split(":", 1)
            ns_uri = elem.nsmap.get(format_prefix) or root.nsmap.get(format_prefix)
            tr = tr_version_from_uri(ns_uri)
            transform_registry = tr.value if tr is not TRVersion.UNKNOWN else None
        else:
            format_local = format_qname_raw

    scale_attr = elem.get("scale")
    sign_attr = elem.get("sign")
    decimals_attr = elem.get("decimals")
    nil_attr = elem.get(_q("xsi", "nil"))

    value_numeric: Decimal | None = None
    if nil_attr == "true":
        value_numeric = None
    elif raw_text:
        try:
            transformed = transform_value(format_local, raw_text)
            if isinstance(transformed, Decimal):
                value_numeric = apply_scale_and_sign(
                    transformed,
                    scale=int(scale_attr) if scale_attr is not None else None,
                    sign=sign_attr,
                )
        except TransformError as exc:
            log.warning("transform_failed", concept=qname, format=format_qname_raw, value=raw_text, err=str(exc))
            value_numeric = None

    return ParsedFact(
        concept_qname=qname,
        context_ref=context_ref,
        unit_ref=unit_ref,
        value_numeric=value_numeric,
        value_text=raw_text or None,
        decimals_text=decimals_attr,
        decimals_inf=decimals_attr == "INF",
        scale=int(scale_attr) if scale_attr is not None else None,
        format_qname=format_qname_raw,
        transform_registry=transform_registry,
        is_hidden=_is_hidden(elem),
        is_dimensional=_context_is_dimensional(context_ref, contexts_by_ref),
        xml_lang=None,
        footnote_text=None,
    )


def _parse_non_numeric(
    elem: etree._Element, root: etree._Element, contexts_by_ref: dict[str, ParsedContext]
) -> ParsedFact:
    qname = _qname_of_concept(elem, root)
    context_ref = elem.get("contextRef", "")
    xml_lang = elem.get("{http://www.w3.org/XML/1998/namespace}lang")
    text_value = _text_content_joined(elem, root)
    return ParsedFact(
        concept_qname=qname,
        context_ref=context_ref,
        unit_ref=None,
        value_numeric=None,
        value_text=text_value or None,
        decimals_text=None,
        decimals_inf=False,
        scale=None,
        format_qname=elem.get("format"),
        transform_registry=None,
        is_hidden=_is_hidden(elem),
        is_dimensional=_context_is_dimensional(context_ref, contexts_by_ref),
        xml_lang=xml_lang,
        footnote_text=None,
    )


def _context_is_dimensional(
    context_ref: str, contexts_by_ref: dict[str, ParsedContext]
) -> bool:
    ctx = contexts_by_ref.get(context_ref)
    return bool(ctx and ctx.dimensions)


def _extract_dts_refs(root: etree._Element) -> DTSReference:
    schema_refs: list[str] = []
    linkbase_refs: list[str] = []
    for r in root.iterfind(f".//{_q('link', 'schemaRef')}"):
        href = _xlink_href(r)
        if href:
            schema_refs.append(href)
    for r in root.iterfind(f".//{_q('link', 'linkbaseRef')}"):
        href = _xlink_href(r)
        if href:
            linkbase_refs.append(href)
    return DTSReference(schema_refs=schema_refs, linkbase_refs=linkbase_refs)
