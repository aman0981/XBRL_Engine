"""create filings table

Revision ID: 20260523_0001
Revises:
Create Date: 2026-05-23

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "20260523_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "filings",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("regulator", sa.String(10), nullable=False),
        sa.Column("entity_id", sa.String(25), nullable=False),
        sa.Column("ticker", sa.String(20)),
        sa.Column("accession_no", sa.String(30), nullable=False),
        sa.Column("amends_accession_no", sa.String(30)),
        sa.Column("form_type", sa.String(20)),
        sa.Column("filing_date", sa.Date),
        sa.Column("period_of_report", sa.Date),
        sa.Column("fiscal_year", sa.Integer),
        sa.Column("fiscal_period", sa.String(4)),
        sa.Column("ixbrl_url", sa.Text),
        sa.Column("raw_sha256", sa.String(64)),
        sa.Column("raw_path", sa.Text),
        sa.Column("ingestion_mode", sa.String(15)),
        sa.Column("ingestion_status", sa.String(20), server_default="pending"),
        sa.Column("arelle_validation_status", sa.String(20)),
        sa.Column("arelle_errors", JSONB),
        sa.Column("ingested_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("regulator", "accession_no", name="uq_filings_regulator_accession"),
        sa.UniqueConstraint("accession_no", name="uq_filings_accession_no"),
        sa.CheckConstraint(
            "regulator IN ('SEC', 'ESEF', 'UPLOAD')", name="ck_filings_regulator"
        ),
        sa.CheckConstraint(
            "ingestion_mode IN ('companyfacts','source_ixbrl','both','user_upload')",
            name="ck_filings_ingestion_mode",
        ),
        sa.ForeignKeyConstraint(
            ["amends_accession_no"],
            ["filings.accession_no"],
            name="fk_filings_amends_accession",
        ),
    )
    op.create_index(
        "ix_filings_entity_period",
        "filings",
        ["regulator", "entity_id", sa.text("period_of_report DESC")],
    )
    op.create_index(
        "ix_filings_ticker_form_filed",
        "filings",
        ["ticker", "form_type", sa.text("filing_date DESC")],
    )
    op.create_index("ix_filings_amends", "filings", ["amends_accession_no"])


def downgrade() -> None:
    op.drop_index("ix_filings_amends", table_name="filings")
    op.drop_index("ix_filings_ticker_form_filed", table_name="filings")
    op.drop_index("ix_filings_entity_period", table_name="filings")
    op.drop_table("filings")
