"""create concepts, concept_arcs, dqc_findings

Revision ID: 20260524_0004
Revises: 20260523_0003
Create Date: 2026-05-24

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260524_0004"
down_revision: str | None = "20260523_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "concepts",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("qname", sa.String(255), nullable=False),
        sa.Column("taxonomy_uri", sa.Text),
        sa.Column("regulator", sa.String(10)),
        sa.Column("is_extension", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("extension_owner_entity_id", sa.String(25)),
        sa.Column("anchor_concept_id", sa.BigInteger, sa.ForeignKey("concepts.id")),
        sa.Column("standard_label", sa.Text),
        sa.Column("documentation", sa.Text),
        sa.Column("period_type", sa.String(10)),
        sa.Column("balance_type", sa.String(10)),
        sa.Column("data_type", sa.String(80)),
        sa.UniqueConstraint("qname", "taxonomy_uri", name="uq_concepts_qname_taxonomy"),
        sa.CheckConstraint(
            "qname ~ '^[A-Za-z_][A-Za-z0-9_.-]*:[A-Za-z_][A-Za-z0-9_.-]*$'",
            name="ck_concepts_qname_pattern",
        ),
    )
    op.create_index("ix_concepts_qname", "concepts", ["qname"])
    op.create_index("ix_concepts_regulator_extension", "concepts", ["regulator", "is_extension"])
    op.create_index("ix_concepts_anchor", "concepts", ["anchor_concept_id"])

    op.create_table(
        "concept_arcs",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("linkbase_type", sa.String(15), nullable=False),
        sa.Column("extended_link_role", sa.Text),
        sa.Column(
            "from_concept_id",
            sa.BigInteger,
            sa.ForeignKey("concepts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "to_concept_id",
            sa.BigInteger,
            sa.ForeignKey("concepts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("arcrole", sa.Text),
        sa.Column("arc_order", sa.Numeric(10, 4)),
        sa.Column("weight", sa.Numeric(10, 4)),
        sa.Column("preferred_label", sa.Text),
        sa.Column("taxonomy_uri", sa.Text),
        sa.CheckConstraint(
            "linkbase_type IN ('presentation','calculation','definition','label')",
            name="ck_concept_arcs_type",
        ),
    )
    op.create_index(
        "ix_concept_arcs_type_role",
        "concept_arcs",
        ["linkbase_type", "extended_link_role"],
    )
    op.create_index("ix_concept_arcs_from", "concept_arcs", ["from_concept_id", "linkbase_type"])
    op.create_index("ix_concept_arcs_to", "concept_arcs", ["to_concept_id", "linkbase_type"])
    op.create_index("ix_concept_arcs_arcrole", "concept_arcs", ["arcrole"])

    op.create_table(
        "dqc_findings",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "filing_id",
            sa.BigInteger,
            sa.ForeignKey("filings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rule_id", sa.String(40)),
        sa.Column("severity", sa.String(10)),
        sa.Column("message", sa.Text),
        sa.Column("concept_qname", sa.String(255)),
        sa.Column("fact_id", sa.BigInteger, sa.ForeignKey("facts.id", ondelete="SET NULL")),
    )
    op.create_index(
        "ix_dqc_findings_filing_severity",
        "dqc_findings",
        ["filing_id", "severity"],
    )


def downgrade() -> None:
    op.drop_index("ix_dqc_findings_filing_severity", table_name="dqc_findings")
    op.drop_table("dqc_findings")
    op.drop_index("ix_concept_arcs_arcrole", table_name="concept_arcs")
    op.drop_index("ix_concept_arcs_to", table_name="concept_arcs")
    op.drop_index("ix_concept_arcs_from", table_name="concept_arcs")
    op.drop_index("ix_concept_arcs_type_role", table_name="concept_arcs")
    op.drop_table("concept_arcs")
    op.drop_index("ix_concepts_anchor", table_name="concepts")
    op.drop_index("ix_concepts_regulator_extension", table_name="concepts")
    op.drop_index("ix_concepts_qname", table_name="concepts")
    op.drop_table("concepts")
