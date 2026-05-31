"""create role_definitions table

Stores presentation/calculation/definition extended-link-role URIs and their
human-readable definition strings (e.g. "1001000 - Statement - CONSOLIDATED
STATEMENTS OF OPERATIONS").

Revision ID: 20260524_0005
Revises: 20260524_0004
Create Date: 2026-05-24

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260524_0005"
down_revision: str | None = "20260524_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "role_definitions",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("uri", sa.Text, nullable=False),
        sa.Column("definition", sa.Text),
        sa.Column("used_on", sa.Text),  # comma-separated linkbase types
        sa.UniqueConstraint("uri", name="uq_role_definitions_uri"),
    )
    op.create_index("ix_role_definitions_uri", "role_definitions", ["uri"])


def downgrade() -> None:
    op.drop_index("ix_role_definitions_uri", table_name="role_definitions")
    op.drop_table("role_definitions")
