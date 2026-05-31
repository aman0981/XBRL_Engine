"""create restatements table

Revision ID: 20260524_0006
Revises: 20260524_0005
Create Date: 2026-05-24

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260524_0006"
down_revision: str | None = "20260524_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "restatements",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("regulator", sa.String(10), nullable=False),
        sa.Column("entity_id", sa.String(25), nullable=False),
        sa.Column("concept_qname", sa.String(255), nullable=False),
        sa.Column("period_start", sa.Date),
        sa.Column("period_end", sa.Date),
        sa.Column("period_instant", sa.Date),
        sa.Column("context_dimensions_hash", sa.String(64)),
        sa.Column("unit_signature", sa.Text),
        sa.Column(
            "original_filing_id",
            sa.BigInteger,
            sa.ForeignKey("filings.id", ondelete="SET NULL"),
        ),
        sa.Column("original_value", sa.Numeric),
        sa.Column("original_decimals_text", sa.String(8)),
        sa.Column("original_reported_at", sa.Date),
        sa.Column(
            "restated_filing_id",
            sa.BigInteger,
            sa.ForeignKey("filings.id", ondelete="SET NULL"),
        ),
        sa.Column("restated_value", sa.Numeric),
        sa.Column("restated_decimals_text", sa.String(8)),
        sa.Column("restated_reported_at", sa.Date),
        sa.Column("delta", sa.Numeric),
        sa.Column("delta_pct", sa.Numeric(10, 4)),
        sa.Column("is_amendment", sa.Boolean, server_default=sa.text("false")),
        sa.Column("is_concept_migration", sa.Boolean, server_default=sa.text("false")),
        sa.Column("is_precision_change", sa.Boolean, server_default=sa.text("false")),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "regulator",
            "entity_id",
            "concept_qname",
            "period_start",
            "period_end",
            "period_instant",
            "context_dimensions_hash",
            "unit_signature",
            "original_filing_id",
            "restated_filing_id",
            name="uq_restatements_natural",
        ),
    )
    op.create_index(
        "ix_restatements_entity_period",
        "restatements",
        ["regulator", "entity_id", sa.text("period_end DESC")],
    )
    op.create_index(
        "ix_restatements_concept_period",
        "restatements",
        ["concept_qname", sa.text("period_end DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_restatements_concept_period", table_name="restatements")
    op.drop_index("ix_restatements_entity_period", table_name="restatements")
    op.drop_table("restatements")
