"""create contexts, units, facts, canonical_line_items

Revision ID: 20260523_0002
Revises: 20260523_0001
Create Date: 2026-05-23

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision: str = "20260523_0002"
down_revision: str | None = "20260523_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "contexts",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "filing_id",
            sa.BigInteger,
            sa.ForeignKey("filings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("context_ref", sa.String(255), nullable=False),
        sa.Column("entity_id", sa.String(25)),
        sa.Column("period_start", sa.Date),
        sa.Column("period_end", sa.Date),
        sa.Column("period_instant", sa.Date),
        sa.Column("dimensions", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("dimensions_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("filing_id", "context_ref", name="uq_contexts_filing_ref"),
    )
    op.create_index("ix_contexts_dims_hash", "contexts", ["dimensions_hash"])
    op.execute("CREATE INDEX ix_contexts_dims_gin ON contexts USING GIN (dimensions)")

    op.create_table(
        "units",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "filing_id",
            sa.BigInteger,
            sa.ForeignKey("filings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("unit_ref", sa.String(50), nullable=False),
        sa.Column("numerator_measures", ARRAY(sa.Text), nullable=False),
        sa.Column("denominator_measures", ARRAY(sa.Text)),
        sa.UniqueConstraint("filing_id", "unit_ref", name="uq_units_filing_ref"),
    )

    op.create_table(
        "facts",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "filing_id",
            sa.BigInteger,
            sa.ForeignKey("filings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("context_id", sa.BigInteger, sa.ForeignKey("contexts.id", ondelete="CASCADE")),
        sa.Column("unit_id", sa.BigInteger, sa.ForeignKey("units.id", ondelete="CASCADE")),
        sa.Column("concept_qname", sa.String(255), nullable=False),
        sa.Column("value_numeric", sa.Numeric),
        sa.Column("value_text", sa.Text),
        sa.Column("decimals_text", sa.String(8)),
        sa.Column("decimals_inf", sa.Boolean, server_default=sa.text("false")),
        sa.Column("scale", sa.Integer),
        sa.Column("format_qname", sa.String(50)),
        sa.Column("transform_registry", sa.String(8)),
        sa.Column("period_start", sa.Date),
        sa.Column("period_end", sa.Date),
        sa.Column("period_instant", sa.Date),
        sa.Column("is_dimensional", sa.Boolean, server_default=sa.text("false")),
        sa.Column("is_hidden", sa.Boolean, server_default=sa.text("false")),
        sa.Column("source", sa.String(15), nullable=False),
        sa.Column("xml_lang", sa.String(10)),
        sa.Column("footnote_text", sa.Text),
        sa.CheckConstraint(
            "source IN ('companyfacts','source_ixbrl')", name="ck_facts_source"
        ),
        sa.UniqueConstraint(
            "filing_id",
            "concept_qname",
            "context_id",
            "unit_id",
            "source",
            name="uq_facts_filing_concept_ctx_unit_src",
        ),
    )
    op.create_index(
        "ix_facts_concept_period", "facts", ["concept_qname", sa.text("period_end DESC")]
    )
    op.create_index("ix_facts_filing", "facts", ["filing_id"])
    op.create_index("ix_facts_source", "facts", ["source"])

    op.create_table(
        "canonical_line_items",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("regulator", sa.String(10), nullable=False),
        sa.Column("entity_id", sa.String(25), nullable=False),
        sa.Column(
            "filing_id",
            sa.BigInteger,
            sa.ForeignKey("filings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("canonical_name", sa.String(50), nullable=False),
        sa.Column("period_start", sa.Date),
        sa.Column("period_end", sa.Date),
        sa.Column("period_instant", sa.Date),
        sa.Column("fiscal_period", sa.String(4)),
        sa.Column("fiscal_year", sa.Integer),
        sa.Column("value", sa.Numeric),
        sa.Column("unit_id", sa.BigInteger, sa.ForeignKey("units.id", ondelete="SET NULL")),
        sa.Column("source_concept_qname", sa.String(255)),
        sa.Column(
            "source_fact_id", sa.BigInteger, sa.ForeignKey("facts.id", ondelete="SET NULL")
        ),
        sa.Column("mapping_confidence", sa.Numeric(4, 3)),
        sa.Column("mapping_method", sa.String(30)),
        sa.UniqueConstraint(
            "regulator",
            "entity_id",
            "canonical_name",
            "period_end",
            "period_instant",
            "fiscal_period",
            name="uq_canonical_unique_period",
        ),
    )
    op.create_index(
        "ix_canonical_entity_period",
        "canonical_line_items",
        ["regulator", "entity_id", "canonical_name", sa.text("period_end DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_canonical_entity_period", table_name="canonical_line_items")
    op.drop_table("canonical_line_items")
    op.drop_index("ix_facts_source", table_name="facts")
    op.drop_index("ix_facts_filing", table_name="facts")
    op.drop_index("ix_facts_concept_period", table_name="facts")
    op.drop_table("facts")
    op.drop_table("units")
    op.execute("DROP INDEX IF EXISTS ix_contexts_dims_gin")
    op.drop_index("ix_contexts_dims_hash", table_name="contexts")
    op.drop_table("contexts")
