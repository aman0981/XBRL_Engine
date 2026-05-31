"""create fact_dimensions and fact_reconciliation tables

Revision ID: 20260523_0003
Revises: 20260523_0002
Create Date: 2026-05-23

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260523_0003"
down_revision: str | None = "20260523_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fact_dimensions",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "fact_id",
            sa.BigInteger,
            sa.ForeignKey("facts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("axis_qname", sa.String(255), nullable=False),
        sa.Column("member_qname", sa.String(255)),
        sa.Column("typed_value", sa.Text),
        sa.Column("is_default", sa.Boolean, server_default=sa.text("false")),
    )
    op.create_index("ix_fact_dims_axis_member", "fact_dimensions", ["axis_qname", "member_qname"])
    op.create_index("ix_fact_dims_fact", "fact_dimensions", ["fact_id"])

    op.create_table(
        "fact_reconciliation",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "filing_id",
            sa.BigInteger,
            sa.ForeignKey("filings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("concept_qname", sa.String(255), nullable=False),
        sa.Column("context_id", sa.BigInteger, sa.ForeignKey("contexts.id", ondelete="SET NULL")),
        sa.Column("unit_id", sa.BigInteger, sa.ForeignKey("units.id", ondelete="SET NULL")),
        sa.Column("companyfacts_value", sa.Numeric),
        sa.Column("source_ixbrl_value", sa.Numeric),
        sa.Column("companyfacts_decimals_text", sa.String(8)),
        sa.Column("source_decimals_text", sa.String(8)),
        sa.Column("mismatch_type", sa.String(20), nullable=False),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "mismatch_type IN ('only_in_a','only_in_b','value_diff','precision_diff')",
            name="ck_fact_recon_type",
        ),
    )
    op.create_index(
        "ix_fact_recon_filing_type",
        "fact_reconciliation",
        ["filing_id", "mismatch_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_fact_recon_filing_type", table_name="fact_reconciliation")
    op.drop_table("fact_reconciliation")
    op.drop_index("ix_fact_dims_fact", table_name="fact_dimensions")
    op.drop_index("ix_fact_dims_axis_member", table_name="fact_dimensions")
    op.drop_table("fact_dimensions")
