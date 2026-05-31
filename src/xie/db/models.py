from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from xie.db.base import Base


class Filing(Base):
    __tablename__ = "filings"
    __table_args__ = (
        UniqueConstraint("regulator", "accession_no", name="uq_filings_regulator_accession"),
        # why: amends_accession_no is FK -> filings.accession_no; Postgres
        # requires a standalone UNIQUE on accession_no to back that FK.
        # Migration 0001 creates it; the model now matches.
        UniqueConstraint("accession_no", name="uq_filings_accession_no"),
        CheckConstraint("regulator IN ('SEC', 'ESEF', 'UPLOAD')", name="ck_filings_regulator"),
        CheckConstraint(
            "ingestion_mode IN ('companyfacts','source_ixbrl','both','user_upload')",
            name="ck_filings_ingestion_mode",
        ),
        Index("ix_filings_entity_period", "regulator", "entity_id", "period_of_report"),
        Index("ix_filings_ticker_form_filed", "ticker", "form_type", "filing_date"),
        Index("ix_filings_amends", "amends_accession_no"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    regulator: Mapped[str] = mapped_column(String(10), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(25), nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(20))
    accession_no: Mapped[str] = mapped_column(String(30), nullable=False)
    amends_accession_no: Mapped[str | None] = mapped_column(
        String(30), ForeignKey("filings.accession_no")
    )
    form_type: Mapped[str | None] = mapped_column(String(20))
    filing_date: Mapped[date | None] = mapped_column(Date)
    period_of_report: Mapped[date | None] = mapped_column(Date)
    fiscal_year: Mapped[int | None] = mapped_column(Integer)
    fiscal_period: Mapped[str | None] = mapped_column(String(4))
    ixbrl_url: Mapped[str | None] = mapped_column(Text)
    raw_sha256: Mapped[str | None] = mapped_column(String(64))
    raw_path: Mapped[str | None] = mapped_column(Text)
    ingestion_mode: Mapped[str | None] = mapped_column(String(15))
    ingestion_status: Mapped[str] = mapped_column(String(20), default="pending")
    arelle_validation_status: Mapped[str | None] = mapped_column(String(20))
    arelle_errors: Mapped[dict | None] = mapped_column(JSONB)
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Context(Base):
    __tablename__ = "contexts"
    __table_args__ = (
        UniqueConstraint("filing_id", "context_ref", name="uq_contexts_filing_ref"),
        Index("ix_contexts_dims_hash", "dimensions_hash"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    filing_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("filings.id", ondelete="CASCADE"), nullable=False
    )
    context_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(25))
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    period_instant: Mapped[date | None] = mapped_column(Date)
    dimensions: Mapped[dict] = mapped_column(JSONB, default=dict)
    dimensions_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class Unit(Base):
    __tablename__ = "units"
    __table_args__ = (
        UniqueConstraint("filing_id", "unit_ref", name="uq_units_filing_ref"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    filing_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("filings.id", ondelete="CASCADE"), nullable=False
    )
    unit_ref: Mapped[str] = mapped_column(String(50), nullable=False)
    numerator_measures: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    denominator_measures: Mapped[list[str] | None] = mapped_column(ARRAY(Text))


class Fact(Base):
    __tablename__ = "facts"
    __table_args__ = (
        CheckConstraint(
            "source IN ('companyfacts','source_ixbrl')", name="ck_facts_source"
        ),
        UniqueConstraint(
            "filing_id",
            "concept_qname",
            "context_id",
            "unit_id",
            "source",
            name="uq_facts_filing_concept_ctx_unit_src",
        ),
        Index("ix_facts_concept_period", "concept_qname", "period_end"),
        Index("ix_facts_filing", "filing_id"),
        Index("ix_facts_source", "source"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    filing_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("filings.id", ondelete="CASCADE"), nullable=False
    )
    context_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("contexts.id", ondelete="CASCADE")
    )
    unit_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("units.id", ondelete="CASCADE")
    )
    concept_qname: Mapped[str] = mapped_column(String(255), nullable=False)
    value_numeric: Mapped[Decimal | None] = mapped_column(Numeric)
    value_text: Mapped[str | None] = mapped_column(Text)
    decimals_text: Mapped[str | None] = mapped_column(String(8))
    decimals_inf: Mapped[bool] = mapped_column(Boolean, default=False)
    scale: Mapped[int | None] = mapped_column(Integer)
    format_qname: Mapped[str | None] = mapped_column(String(50))
    transform_registry: Mapped[str | None] = mapped_column(String(8))
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    period_instant: Mapped[date | None] = mapped_column(Date)
    is_dimensional: Mapped[bool] = mapped_column(Boolean, default=False)
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(15), nullable=False)
    xml_lang: Mapped[str | None] = mapped_column(String(10))
    footnote_text: Mapped[str | None] = mapped_column(Text)


class RoleDefinition(Base):
    __tablename__ = "role_definitions"
    __table_args__ = (
        UniqueConstraint("uri", name="uq_role_definitions_uri"),
        Index("ix_role_definitions_uri", "uri"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    definition: Mapped[str | None] = mapped_column(Text)
    used_on: Mapped[str | None] = mapped_column(Text)


class Concept(Base):
    __tablename__ = "concepts"
    __table_args__ = (
        UniqueConstraint("qname", "taxonomy_uri", name="uq_concepts_qname_taxonomy"),
        CheckConstraint(
            "qname ~ '^[A-Za-z_][A-Za-z0-9_.-]*:[A-Za-z_][A-Za-z0-9_.-]*$'",
            name="ck_concepts_qname_pattern",
        ),
        Index("ix_concepts_qname", "qname"),
        Index("ix_concepts_regulator_extension", "regulator", "is_extension"),
        Index("ix_concepts_anchor", "anchor_concept_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    qname: Mapped[str] = mapped_column(String(255), nullable=False)
    taxonomy_uri: Mapped[str | None] = mapped_column(Text)
    regulator: Mapped[str | None] = mapped_column(String(10))
    is_extension: Mapped[bool] = mapped_column(Boolean, default=False)
    extension_owner_entity_id: Mapped[str | None] = mapped_column(String(25))
    anchor_concept_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("concepts.id")
    )
    standard_label: Mapped[str | None] = mapped_column(Text)
    documentation: Mapped[str | None] = mapped_column(Text)
    period_type: Mapped[str | None] = mapped_column(String(10))
    balance_type: Mapped[str | None] = mapped_column(String(10))
    data_type: Mapped[str | None] = mapped_column(String(80))


class ConceptArc(Base):
    __tablename__ = "concept_arcs"
    __table_args__ = (
        CheckConstraint(
            "linkbase_type IN ('presentation','calculation','definition','label')",
            name="ck_concept_arcs_type",
        ),
        Index("ix_concept_arcs_type_role", "linkbase_type", "extended_link_role"),
        Index("ix_concept_arcs_from", "from_concept_id", "linkbase_type"),
        Index("ix_concept_arcs_to", "to_concept_id", "linkbase_type"),
        Index("ix_concept_arcs_arcrole", "arcrole"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    linkbase_type: Mapped[str] = mapped_column(String(15), nullable=False)
    extended_link_role: Mapped[str | None] = mapped_column(Text)
    from_concept_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("concepts.id", ondelete="CASCADE"), nullable=False
    )
    to_concept_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("concepts.id", ondelete="CASCADE"), nullable=False
    )
    arcrole: Mapped[str | None] = mapped_column(Text)
    arc_order: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    weight: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    preferred_label: Mapped[str | None] = mapped_column(Text)
    taxonomy_uri: Mapped[str | None] = mapped_column(Text)


class DQCFinding(Base):
    __tablename__ = "dqc_findings"
    __table_args__ = (
        Index("ix_dqc_findings_filing_severity", "filing_id", "severity"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    filing_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("filings.id", ondelete="CASCADE"), nullable=False
    )
    rule_id: Mapped[str | None] = mapped_column(String(40))
    severity: Mapped[str | None] = mapped_column(String(10))
    message: Mapped[str | None] = mapped_column(Text)
    concept_qname: Mapped[str | None] = mapped_column(String(255))
    fact_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("facts.id", ondelete="SET NULL")
    )


class FactDimension(Base):
    __tablename__ = "fact_dimensions"
    __table_args__ = (
        Index("ix_fact_dims_axis_member", "axis_qname", "member_qname"),
        Index("ix_fact_dims_fact", "fact_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    fact_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("facts.id", ondelete="CASCADE"), nullable=False
    )
    axis_qname: Mapped[str] = mapped_column(String(255), nullable=False)
    member_qname: Mapped[str | None] = mapped_column(String(255))
    typed_value: Mapped[str | None] = mapped_column(Text)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)


class FactReconciliation(Base):
    __tablename__ = "fact_reconciliation"
    __table_args__ = (
        CheckConstraint(
            "mismatch_type IN ('only_in_a','only_in_b','value_diff','precision_diff')",
            name="ck_fact_recon_type",
        ),
        Index("ix_fact_recon_filing_type", "filing_id", "mismatch_type"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    filing_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("filings.id", ondelete="CASCADE"), nullable=False
    )
    concept_qname: Mapped[str] = mapped_column(String(255), nullable=False)
    context_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("contexts.id", ondelete="SET NULL")
    )
    unit_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("units.id", ondelete="SET NULL")
    )
    companyfacts_value: Mapped[Decimal | None] = mapped_column(Numeric)
    source_ixbrl_value: Mapped[Decimal | None] = mapped_column(Numeric)
    companyfacts_decimals_text: Mapped[str | None] = mapped_column(String(8))
    source_decimals_text: Mapped[str | None] = mapped_column(String(8))
    mismatch_type: Mapped[str] = mapped_column(String(20), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Restatement(Base):
    __tablename__ = "restatements"
    __table_args__ = (
        UniqueConstraint(
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
        Index("ix_restatements_entity_period", "regulator", "entity_id", "period_end"),
        Index("ix_restatements_concept_period", "concept_qname", "period_end"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    regulator: Mapped[str] = mapped_column(String(10), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(25), nullable=False)
    concept_qname: Mapped[str] = mapped_column(String(255), nullable=False)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    period_instant: Mapped[date | None] = mapped_column(Date)
    context_dimensions_hash: Mapped[str | None] = mapped_column(String(64))
    unit_signature: Mapped[str | None] = mapped_column(Text)
    original_filing_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("filings.id", ondelete="SET NULL")
    )
    original_value: Mapped[Decimal | None] = mapped_column(Numeric)
    original_decimals_text: Mapped[str | None] = mapped_column(String(8))
    original_reported_at: Mapped[date | None] = mapped_column(Date)
    restated_filing_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("filings.id", ondelete="SET NULL")
    )
    restated_value: Mapped[Decimal | None] = mapped_column(Numeric)
    restated_decimals_text: Mapped[str | None] = mapped_column(String(8))
    restated_reported_at: Mapped[date | None] = mapped_column(Date)
    delta: Mapped[Decimal | None] = mapped_column(Numeric)
    delta_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    is_amendment: Mapped[bool] = mapped_column(Boolean, default=False)
    is_concept_migration: Mapped[bool] = mapped_column(Boolean, default=False)
    is_precision_change: Mapped[bool] = mapped_column(Boolean, default=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CanonicalLineItem(Base):
    __tablename__ = "canonical_line_items"
    __table_args__ = (
        UniqueConstraint(
            "regulator",
            "entity_id",
            "canonical_name",
            "period_end",
            "period_instant",
            "fiscal_period",
            name="uq_canonical_unique_period",
        ),
        Index(
            "ix_canonical_entity_period",
            "regulator",
            "entity_id",
            "canonical_name",
            "period_end",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    regulator: Mapped[str] = mapped_column(String(10), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(25), nullable=False)
    filing_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("filings.id", ondelete="CASCADE"), nullable=False
    )
    canonical_name: Mapped[str] = mapped_column(String(50), nullable=False)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    period_instant: Mapped[date | None] = mapped_column(Date)
    fiscal_period: Mapped[str | None] = mapped_column(String(4))
    fiscal_year: Mapped[int | None] = mapped_column(Integer)
    value: Mapped[Decimal | None] = mapped_column(Numeric)
    unit_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("units.id", ondelete="SET NULL")
    )
    source_concept_qname: Mapped[str | None] = mapped_column(String(255))
    source_fact_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("facts.id", ondelete="SET NULL")
    )
    mapping_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    mapping_method: Mapped[str | None] = mapped_column(String(30))
