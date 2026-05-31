"""In-process Arelle wrapper.

Loads a filing's DTS, walks concept metadata + linkbase relationships, captures
Arelle's built-in validation messages.

Hot-path note: each DTS load is 5-15 s on first reach. Subsequent loads from the
same `Cntlr` reuse the in-memory cache, so we batch enrichment per process.

The plan calls for a long-lived sidecar with a Postgres SKIP LOCKED queue. For
the MVP we run synchronously from the CLI: same code, simpler invocation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from xie.core.config import settings
from xie.core.logging import get_logger

log = get_logger("xie.xbrl.arelle_worker")


PRESENTATION_ARCROLE = "http://www.xbrl.org/2003/arcrole/parent-child"
CALCULATION_ARCROLE = "http://www.xbrl.org/2003/arcrole/summation-item"
DEFINITION_ARCROLES = (
    "http://xbrl.org/int/dim/arcrole/dimension-domain",
    "http://xbrl.org/int/dim/arcrole/domain-member",
    "http://xbrl.org/int/dim/arcrole/hypercube-dimension",
    "http://xbrl.org/int/dim/arcrole/all",
    "http://xbrl.org/int/dim/arcrole/notAll",
    "http://xbrl.org/int/dim/arcrole/dimension-default",
    "http://www.esma.europa.eu/xbrl/esef/arcrole/wider-narrower",
)


@dataclass(slots=True)
class EnrichedConcept:
    qname: str                  # prefix:local
    taxonomy_uri: str | None    # namespace URI of the concept
    standard_label: str | None
    documentation: str | None
    period_type: str | None     # duration | instant
    balance_type: str | None    # debit | credit | None
    data_type: str | None
    is_extension: bool
    extension_owner_entity_id: str | None


@dataclass(slots=True)
class EnrichedArc:
    linkbase_type: str          # presentation | calculation | definition | label
    extended_link_role: str
    from_qname: str
    to_qname: str
    arcrole: str
    arc_order: Decimal | None
    weight: Decimal | None
    preferred_label: str | None


@dataclass(slots=True)
class EnrichedFinding:
    rule_id: str | None
    severity: str               # error | warning | info
    message: str
    concept_qname: str | None


@dataclass(slots=True)
class EnrichedRole:
    uri: str
    definition: str | None
    used_on: list[str]          # e.g. ["presentation", "calculation"]


@dataclass(slots=True)
class EnrichmentResult:
    concepts: list[EnrichedConcept] = field(default_factory=list)
    arcs: list[EnrichedArc] = field(default_factory=list)
    findings: list[EnrichedFinding] = field(default_factory=list)
    roles: list[EnrichedRole] = field(default_factory=list)


def _qname_to_str(qname) -> str:
    """Arelle QName -> 'prefix:local' with our prefix conventions.

    Arelle QName has .prefix, .localName, .namespaceURI. The prefix may be None
    for the default namespace; we coerce to a synthesized prefix in that case.
    """
    prefix = getattr(qname, "prefix", None) or ""
    local = getattr(qname, "localName", None) or ""
    if prefix:
        return f"{prefix}:{local}"
    return local


def _is_extension(qname, entity_id: str | None) -> tuple[bool, str | None]:
    """Heuristic: a concept whose namespace is the filer's own (not us-gaap/dei/srt/ifrs).

    Arelle exposes QName.namespaceURI. We treat namespaces starting with
    http://fasb.org/us-gaap, http://xbrl.sec.gov/dei, http://fasb.org/srt,
    http://xbrl.ifrs.org/, http://www.esma.europa.eu/, http://www.xbrl.org/ as standard.
    """
    ns = getattr(qname, "namespaceURI", "") or ""
    standard_prefixes = (
        "http://fasb.org/us-gaap",
        "http://fasb.org/srt",
        "http://xbrl.sec.gov/dei",
        "http://xbrl.sec.gov/country",
        "http://xbrl.sec.gov/currency",
        "http://xbrl.sec.gov/exch",
        "http://xbrl.sec.gov/naics",
        "http://xbrl.sec.gov/stpr",
        "http://xbrl.ifrs.org/",
        "http://www.esma.europa.eu/",
        "http://www.xbrl.org/",
    )
    if any(ns.startswith(p) for p in standard_prefixes):
        return False, None
    return True, entity_id


class ArelleEnricher:
    """One Cntlr per process; load(model_xbrl) -> walk concepts/arcs/findings -> close()."""

    def __init__(
        self,
        cache_dir: Path | None = None,
        work_offline: bool = False,
    ) -> None:
        # Lazy import: arelle pulls numpy/pillow/openpyxl on import.
        from arelle import Cntlr

        self._Cntlr = Cntlr.Cntlr
        self._cache_dir = cache_dir or settings.arelle_cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._cntlr = self._Cntlr(logFileName="logToBuffer")
        # why: SEC/ESEF flows need network on the first DTS load (taxonomy
        # schemas live at fasb.org/xbrl.org); subsequent loads hit the local
        # cache. Mode C uploads pass work_offline=True so a user-supplied
        # schemaRef can never trigger outbound HTTP from the server.
        self._cntlr.webCache.workOffline = work_offline
        self._cntlr.webCache.cacheDir = str(self._cache_dir)

    def close(self) -> None:
        self._cntlr.close()

    def enrich(
        self, primary_path: Path, entity_id: str | None = None
    ) -> EnrichmentResult:
        log.info("arelle_load_start", path=str(primary_path))
        model_xbrl = self._cntlr.modelManager.load(str(primary_path))
        if model_xbrl is None:
            raise RuntimeError(f"Arelle failed to load {primary_path}")
        try:
            concepts = list(self._walk_concepts(model_xbrl, entity_id))
            arcs = list(self._walk_arcs(model_xbrl))
            findings = list(self._collect_findings(model_xbrl))
            roles = list(self._collect_roles(model_xbrl))
            log.info(
                "arelle_enrich_done",
                concepts=len(concepts),
                arcs=len(arcs),
                findings=len(findings),
                roles=len(roles),
            )
            return EnrichmentResult(
                concepts=concepts, arcs=arcs, findings=findings, roles=roles
            )
        finally:
            self._cntlr.modelManager.close(model_xbrl)

    def _walk_concepts(
        self, model_xbrl, entity_id: str | None
    ) -> Iterable[EnrichedConcept]:
        for qname, concept in model_xbrl.qnameConcepts.items():
            qstr = _qname_to_str(qname)
            if not qstr or ":" not in qstr:
                continue
            is_ext, owner = _is_extension(qname, entity_id)
            std_label = None
            doc = None
            try:
                std_label = concept.label() or None
            except Exception:
                pass
            try:
                # arelle's concept.label('documentation') returns doc string when defined
                doc = concept.label(
                    preferredLabel="http://www.xbrl.org/2003/role/documentation"
                ) or None
                if doc == std_label:  # avoid duplicating label as documentation
                    doc = None
            except Exception:
                pass
            yield EnrichedConcept(
                qname=qstr,
                taxonomy_uri=getattr(qname, "namespaceURI", None),
                standard_label=std_label,
                documentation=doc,
                period_type=getattr(concept, "periodType", None),
                balance_type=getattr(concept, "balance", None),
                data_type=str(concept.typeQname) if concept.typeQname is not None else None,
                is_extension=is_ext,
                extension_owner_entity_id=owner if is_ext else None,
            )

    def _walk_arcs(self, model_xbrl) -> Iterable[EnrichedArc]:
        yield from self._walk_arcrole(model_xbrl, "presentation", PRESENTATION_ARCROLE)
        yield from self._walk_arcrole(model_xbrl, "calculation", CALCULATION_ARCROLE)
        for arcrole in DEFINITION_ARCROLES:
            yield from self._walk_arcrole(model_xbrl, "definition", arcrole)

    def _walk_arcrole(
        self, model_xbrl, linkbase_type: str, arcrole: str
    ) -> Iterable[EnrichedArc]:
        rel_set = model_xbrl.relationshipSet(arcrole)
        if rel_set is None:
            return
        for elr in rel_set.linkRoleUris:
            elr_set = model_xbrl.relationshipSet(arcrole, elr)
            if elr_set is None:
                continue
            for rel in elr_set.modelRelationships:
                src = rel.fromModelObject
                tgt = rel.toModelObject
                if src is None or tgt is None:
                    continue
                src_q = _qname_to_str(src.qname)
                tgt_q = _qname_to_str(tgt.qname)
                if ":" not in src_q or ":" not in tgt_q:
                    continue
                yield EnrichedArc(
                    linkbase_type=linkbase_type,
                    extended_link_role=elr,
                    from_qname=src_q,
                    to_qname=tgt_q,
                    arcrole=arcrole,
                    arc_order=Decimal(str(rel.order)) if rel.order is not None else None,
                    weight=(
                        Decimal(str(rel.weight))
                        if getattr(rel, "weight", None) is not None
                        else None
                    ),
                    preferred_label=getattr(rel, "preferredLabel", None),
                )

    def _collect_roles(self, model_xbrl) -> Iterable[EnrichedRole]:
        """Extract role-type definitions for every ELR seen in the DTS.

        Arelle exposes them via model_xbrl.roleTypes -> dict[uri, list[modelRoleType]].
        Each ModelRoleType has .definition and .usedOns (list of QNames identifying
        which link arcrole+arc element pair uses the role).
        """
        role_types = getattr(model_xbrl, "roleTypes", {}) or {}
        for uri, entries in role_types.items():
            definition: str | None = None
            used_on: list[str] = []
            for rt in entries:
                if rt.definition and not definition:
                    definition = str(rt.definition)
                for uo in getattr(rt, "usedOns", None) or []:
                    local = getattr(uo, "localName", None) or str(uo)
                    used_on.append(local)
            yield EnrichedRole(
                uri=str(uri),
                definition=definition,
                used_on=sorted(set(used_on)),
            )

    def _collect_findings(self, model_xbrl) -> Iterable[EnrichedFinding]:
        # Arelle accumulates log messages into modelXbrl.errors (list of message codes)
        # and detailed entries in modelXbrl.logCollection / modelManager.log etc.
        # The simplest signal: model_xbrl.errors as rule_ids.
        seen: set[str] = set()
        for err in getattr(model_xbrl, "errors", []) or []:
            rule_id = str(err)
            if rule_id in seen:
                continue
            seen.add(rule_id)
            yield EnrichedFinding(
                rule_id=rule_id,
                severity="error",
                message=rule_id,
                concept_qname=None,
            )
