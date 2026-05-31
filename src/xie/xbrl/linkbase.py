"""Presentation-linkbase tree builder + fact value lookup for statement reconstruction."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xie.db.models import Concept, ConceptArc, Context, Fact, RoleDefinition, Unit


@dataclass(slots=True)
class StatementNode:
    qname: str
    label: str | None
    period_type: str | None
    balance_type: str | None
    arc_order: Decimal | None
    depth: int
    children: list["StatementNode"] = field(default_factory=list)


@dataclass(slots=True)
class StatementColumn:
    context_id: int
    label: str
    period_start: object
    period_end: object
    period_instant: object


@dataclass(slots=True)
class StatementFactValue:
    value_numeric: Decimal | None
    unit_ref: str | None
    decimals_text: str | None


@dataclass(slots=True)
class StatementRenderRow:
    qname: str
    label: str | None
    period_type: str | None
    balance_type: str | None
    depth: int
    values: dict[int, StatementFactValue]  # context_id -> value


@dataclass(slots=True)
class ReconstructedStatement:
    role_uri: str
    role_definition: str | None
    columns: list[StatementColumn]
    rows: list[StatementRenderRow]


async def list_statement_roles(
    session: AsyncSession, regulator: str = "SEC"
) -> list[dict]:
    """List ELRs that have presentation arcs and a role_definition entry.

    Filters to roles whose definition looks like a statement (heuristic: contains
    'Statement' or 'Balance' or 'Cash Flow' or 'Operations' or 'Stockholders').
    """
    stmt = (
        select(
            RoleDefinition.uri,
            RoleDefinition.definition,
            RoleDefinition.used_on,
        )
        .where(RoleDefinition.used_on.contains("presentationLink"))
        .order_by(RoleDefinition.definition)
    )
    result = (await session.execute(stmt)).mappings().all()

    statements: list[dict] = []
    for r in result:
        defn = (r["definition"] or "").lower()
        if not any(k in defn for k in ("statement", "balance", "cash flow", "operations", "equity")):
            continue
        statements.append(dict(r))
    return statements


async def reconstruct_statement(
    session: AsyncSession, filing_id: int, role_uri: str
) -> ReconstructedStatement:
    role_definition = (
        await session.execute(
            select(RoleDefinition.definition).where(RoleDefinition.uri == role_uri)
        )
    ).scalar_one_or_none()

    tree_rows = await _load_presentation_arcs(session, role_uri)
    columns = await _load_primary_columns(session, filing_id)
    facts_index = await _load_fact_values(session, filing_id, role_uri)

    tree = _build_tree(tree_rows)
    rows = _flatten_tree(tree, facts_index)
    return ReconstructedStatement(
        role_uri=role_uri,
        role_definition=role_definition,
        columns=columns,
        rows=rows,
    )


async def _load_presentation_arcs(
    session: AsyncSession, role_uri: str
) -> list[dict]:
    From = Concept.__table__.alias("c_from")
    To = Concept.__table__.alias("c_to")
    stmt = (
        select(
            From.c.qname.label("from_qname"),
            From.c.standard_label.label("from_label"),
            From.c.period_type.label("from_period_type"),
            From.c.balance_type.label("from_balance_type"),
            To.c.qname.label("to_qname"),
            To.c.standard_label.label("to_label"),
            To.c.period_type.label("to_period_type"),
            To.c.balance_type.label("to_balance_type"),
            ConceptArc.arc_order,
            ConceptArc.preferred_label,
        )
        .join(From, From.c.id == ConceptArc.from_concept_id)
        .join(To, To.c.id == ConceptArc.to_concept_id)
        .where(
            ConceptArc.linkbase_type == "presentation",
            ConceptArc.extended_link_role == role_uri,
        )
    )
    return [dict(r) for r in (await session.execute(stmt)).mappings().all()]


async def _load_primary_columns(
    session: AsyncSession, filing_id: int
) -> list[StatementColumn]:
    """Pick filing contexts without dimensions; these form the period columns."""
    stmt = (
        select(
            Context.id,
            Context.period_start,
            Context.period_end,
            Context.period_instant,
        )
        .where(
            Context.filing_id == filing_id,
            Context.dimensions == {},
        )
        .order_by(Context.period_end.desc().nullslast(), Context.period_instant.desc().nullslast())
    )
    cols: list[StatementColumn] = []
    for r in (await session.execute(stmt)).all():
        ctx_id, p_start, p_end, p_inst = r
        if p_inst is not None:
            label = f"@{p_inst.isoformat()}"
        elif p_start and p_end:
            label = f"{p_start.isoformat()} → {p_end.isoformat()}"
        else:
            label = f"ctx#{ctx_id}"
        cols.append(
            StatementColumn(
                context_id=ctx_id,
                label=label,
                period_start=p_start,
                period_end=p_end,
                period_instant=p_inst,
            )
        )
    # cap to first 5 non-dim contexts; statements usually have ≤4 columns
    return cols[:5]


async def _load_fact_values(
    session: AsyncSession,
    filing_id: int,
    role_uri: str,
) -> dict[tuple[str, int], StatementFactValue]:
    """Map (concept_qname, context_id) -> latest fact value (Mode B preferred)."""
    stmt = (
        select(
            Fact.concept_qname,
            Fact.context_id,
            Fact.value_numeric,
            Fact.decimals_text,
            Fact.source,
            Unit.unit_ref,
        )
        .join(Unit, Unit.id == Fact.unit_id, isouter=True)
        .where(
            Fact.filing_id == filing_id,
            Fact.is_dimensional.is_(False),
        )
    )
    out: dict[tuple[str, int], StatementFactValue] = {}
    for r in (await session.execute(stmt)).all():
        qname, ctx_id, val, dec, source, unit_ref = r
        if ctx_id is None or val is None:
            continue
        key = (qname, ctx_id)
        # Prefer source_ixbrl over companyfacts when both exist.
        if key in out and source == "companyfacts":
            continue
        out[key] = StatementFactValue(
            value_numeric=val, unit_ref=unit_ref, decimals_text=dec
        )
    return out


def _build_tree(rows: list[dict]) -> list[StatementNode]:
    # Index nodes by qname; capture parent->children edges.
    nodes: dict[str, StatementNode] = {}
    children_of: dict[str, list[tuple[Decimal | None, str]]] = {}
    all_targets: set[str] = set()

    for r in rows:
        parent_q = r["from_qname"]
        child_q = r["to_qname"]
        if parent_q not in nodes:
            nodes[parent_q] = StatementNode(
                qname=parent_q,
                label=r["from_label"],
                period_type=r["from_period_type"],
                balance_type=r["from_balance_type"],
                arc_order=None,
                depth=0,
            )
        if child_q not in nodes:
            nodes[child_q] = StatementNode(
                qname=child_q,
                label=r["to_label"],
                period_type=r["to_period_type"],
                balance_type=r["to_balance_type"],
                arc_order=r["arc_order"],
                depth=0,
            )
        children_of.setdefault(parent_q, []).append((r["arc_order"], child_q))
        all_targets.add(child_q)

    roots = [q for q in nodes if q not in all_targets]

    def attach(q: str, depth: int) -> None:
        node = nodes[q]
        node.depth = depth
        kids = sorted(children_of.get(q, []), key=lambda t: (t[0] is None, t[0] or 0))
        for _order, child_q in kids:
            child_node = nodes[child_q]
            node.children.append(child_node)
            attach(child_q, depth + 1)

    for root in roots:
        attach(root, 0)
    return [nodes[r] for r in roots]


def _flatten_tree(
    roots: list[StatementNode],
    facts_index: dict[tuple[str, int], StatementFactValue],
) -> list[StatementRenderRow]:
    rows: list[StatementRenderRow] = []

    def walk(node: StatementNode) -> None:
        values = {
            ctx_id: facts_index[(node.qname, ctx_id)]
            for (q, ctx_id) in facts_index
            if q == node.qname
        }
        rows.append(
            StatementRenderRow(
                qname=node.qname,
                label=node.label,
                period_type=node.period_type,
                balance_type=node.balance_type,
                depth=node.depth,
                values=values,
            )
        )
        for child in node.children:
            walk(child)

    for root in roots:
        walk(root)
    return rows
