"""Authoritative relational model (blueprint Section 4).

Table names carry the logical schema as a prefix (``registry_``, ``contracts_`` ...) so the
same model runs on SQLite for the MVP and maps 1:1 onto PostgreSQL schemas later.
All primary keys are UUID strings; human-readable numbers are unique only within scope.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (JSON, Boolean, DateTime, ForeignKey, ForeignKeyConstraint, Integer, Numeric,
                        String, Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def uid() -> str:
    return str(uuid.uuid4())


def now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- identity
class User(Base):
    __tablename__ = "identity_users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    full_name: Mapped[str] = mapped_column(String(200))
    roles: Mapped[list] = mapped_column(JSON, default=list)          # global roles
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    clearance: Mapped[str] = mapped_column(String(20), default="CONFIDENTIAL")


class ProjectMembership(Base):
    __tablename__ = "identity_project_memberships"
    __table_args__ = (UniqueConstraint("user_id", "project_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("identity_users.id"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects_projects.id"))
    roles: Mapped[list] = mapped_column(JSON, default=list)          # project-scoped roles


# ---------------------------------------------------------------- registry
class Subcontractor(Base):
    __tablename__ = "registry_subcontractors"
    __table_args__ = (UniqueConstraint("country_code", "registration_number", name="uq_legal_identity"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    legal_name: Mapped[str] = mapped_column(String(300))
    registration_number: Mapped[str] = mapped_column(String(100))
    country_code: Mapped[str] = mapped_column(String(2))
    entity_status: Mapped[str] = mapped_column(String(30), default="ACTIVE")
    disciplines: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Qualification(Base):
    __tablename__ = "registry_qualifications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    subcontractor_id: Mapped[str] = mapped_column(ForeignKey("registry_subcontractors.id"))
    qualification_type: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30), default="PENDING")
    valid_to: Mapped[str | None] = mapped_column(String(10), nullable=True)


# ---------------------------------------------------------------- projects
class Project(Base):
    __tablename__ = "projects_projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_code: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(300))
    sector_code: Mapped[str] = mapped_column(String(40), default="PROCESS")
    location: Mapped[str] = mapped_column(String(200), default="")
    timezone: Mapped[str] = mapped_column(String(60), default="Asia/Kolkata")
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE")


class MainContract(Base):
    __tablename__ = "projects_main_contracts"
    # (id, project_id) is unique so subcontracts can reference it with a composite FK.
    __table_args__ = (UniqueConstraint("id", "project_id", name="uq_main_contract_project"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects_projects.id"))
    contract_reference: Mapped[str] = mapped_column(String(100))
    employer_name: Mapped[str] = mapped_column(String(300))
    effective_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="EXECUTED")


class ProjectSubcontractor(Base):
    """Project-specific eligibility. Not a substitute for the subcontract record."""
    __tablename__ = "projects_project_subcontractors"
    __table_args__ = (UniqueConstraint("project_id", "subcontractor_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects_projects.id"))
    subcontractor_id: Mapped[str] = mapped_column(ForeignKey("registry_subcontractors.id"))
    eligibility_status: Mapped[str] = mapped_column(String(30), default="ELIGIBLE")
    package: Mapped[str] = mapped_column(String(200), default="")
    approved_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------- contracts
class Subcontract(Base):
    __tablename__ = "contracts_subcontracts"
    __table_args__ = (
        # CRITICAL invariant (CON-01): the main contract must belong to the same project.
        ForeignKeyConstraint(["main_contract_id", "project_id"],
                             ["projects_main_contracts.id", "projects_main_contracts.project_id"],
                             name="fk_subcontract_main_contract_same_project"),
        UniqueConstraint("project_id", "subcontract_number"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects_projects.id"))
    main_contract_id: Mapped[str] = mapped_column(String(36), nullable=False)   # CON-02: never null
    subcontractor_id: Mapped[str] = mapped_column(ForeignKey("registry_subcontractors.id"))
    subcontract_number: Mapped[str] = mapped_column(String(60))
    scope_summary: Mapped[str] = mapped_column(Text)
    discipline: Mapped[str] = mapped_column(String(60), default="PIPING")
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    contract_value: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    status: Mapped[str] = mapped_column(String(40), default="DRAFT")
    created_by: Mapped[str] = mapped_column(String(36))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    baseline: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # frozen execution baseline
    release_exception: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Amendment(Base):
    __tablename__ = "contracts_amendments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    subcontract_id: Mapped[str] = mapped_column(ForeignKey("contracts_subcontracts.id"))
    amendment_number: Mapped[int] = mapped_column(Integer)
    field: Mapped[str] = mapped_column(String(60))
    old_value: Mapped[str] = mapped_column(Text)
    new_value: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(String(36))
    approved_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ContractClause(Base):
    __tablename__ = "contracts_clauses"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    document_revision_id: Mapped[str] = mapped_column(ForeignKey("documents_revisions.id"))
    main_contract_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    clause_reference: Mapped[str] = mapped_column(String(40))
    heading: Mapped[str] = mapped_column(String(300))
    text: Mapped[str] = mapped_column(Text)
    clause_type: Mapped[str] = mapped_column(String(40))
    source_passage_id: Mapped[str] = mapped_column(ForeignKey("documents_passages.id"))


class FlowdownMapping(Base):
    __tablename__ = "contracts_flowdown_mappings"
    __table_args__ = (UniqueConstraint("subcontract_id", "source_clause_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    subcontract_id: Mapped[str] = mapped_column(ForeignKey("contracts_subcontracts.id"))
    main_contract_id: Mapped[str] = mapped_column(String(36))
    source_clause_id: Mapped[str] = mapped_column(ForeignKey("contracts_clauses.id"))
    proposed_disposition: Mapped[str] = mapped_column(String(40))     # machine proposal
    proposed_rationale: Mapped[str] = mapped_column(Text)
    proposal_method: Mapped[str] = mapped_column(String(60), default="rules-v1")
    disposition: Mapped[str | None] = mapped_column(String(40), nullable=True)  # human decision
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    subcontract_treatment: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_level: Mapped[str] = mapped_column(String(10))
    review_status: Mapped[str] = mapped_column(String(20), default="PROPOSED")  # PROPOSED/APPROVED
    reviewed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------- documents
class Document(Base):
    __tablename__ = "documents_documents"
    __table_args__ = (UniqueConstraint("project_id", "document_number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects_projects.id"))
    main_contract_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    subcontract_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    document_type: Mapped[str] = mapped_column(String(40))
    document_number: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(300))
    source_party: Mapped[str] = mapped_column(String(60), default="EMPLOYER")
    security_classification: Mapped[str] = mapped_column(String(20), default="CONFIDENTIAL")
    governing_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    revisions: Mapped[list["DocumentRevision"]] = relationship(back_populates="document",
                                                                order_by="DocumentRevision.received_at")


class DocumentRevision(Base):
    __tablename__ = "documents_revisions"
    __table_args__ = (UniqueConstraint("document_id", "content_hash"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents_documents.id"))
    revision_label: Mapped[str] = mapped_column(String(20))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    object_key: Mapped[str] = mapped_column(String(200))
    filename: Mapped[str] = mapped_column(String(300))
    size_bytes: Mapped[int] = mapped_column(Integer)
    issued_at: Mapped[str | None] = mapped_column(String(10), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    effective_from: Mapped[str | None] = mapped_column(String(10), nullable=True)
    uploaded_by: Mapped[str] = mapped_column(String(36))
    source_channel: Mapped[str] = mapped_column(String(30), default="MANUAL_UPLOAD")
    # Ingestion state is separate from business status and contractual applicability.
    ingestion_state: Mapped[str] = mapped_column(String(30), default="RECEIVED")
    business_status: Mapped[str] = mapped_column(String(30), default="SUBMITTED")
    supersedes_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    conflict_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    document: Mapped[Document] = relationship(back_populates="revisions")


class ProcessingJob(Base):
    __tablename__ = "documents_processing_jobs"
    __table_args__ = (UniqueConstraint("revision_id", "pipeline_version"),)   # idempotent (DOC-06/07)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    revision_id: Mapped[str] = mapped_column(ForeignKey("documents_revisions.id"))
    pipeline_version: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30), default="PENDING")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(200), nullable=True)
    stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Passage(Base):
    __tablename__ = "documents_passages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    revision_id: Mapped[str] = mapped_column(ForeignKey("documents_revisions.id"), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("documents_processing_jobs.id"))
    seq: Mapped[int] = mapped_column(Integer)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    section_reference: Mapped[str | None] = mapped_column(String(60), nullable=True)
    cell_address: Mapped[str | None] = mapped_column(String(60), nullable=True)
    content_type: Mapped[str] = mapped_column(String(20), default="TEXT")
    text: Mapped[str] = mapped_column(Text)
    flags: Mapped[list] = mapped_column(JSON, default=list)   # e.g. EMBEDDED_INSTRUCTION


class ExtractionAssertion(Base):
    __tablename__ = "documents_extraction_assertions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    passage_id: Mapped[str] = mapped_column(ForeignKey("documents_passages.id"))
    job_id: Mapped[str] = mapped_column(ForeignKey("documents_processing_jobs.id"))
    field_name: Mapped[str] = mapped_column(String(60))
    raw_value: Mapped[str] = mapped_column(Text)
    normalised_value: Mapped[str] = mapped_column(String(200))
    method: Mapped[str] = mapped_column(String(60))
    confidence: Mapped[float] = mapped_column(Numeric(4, 3))
    validation_status: Mapped[str] = mapped_column(String(20), default="PROPOSED")
    reviewer_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


# ---------------------------------------------------------------- obligations
class Obligation(Base):
    __tablename__ = "obligations_obligations"
    __table_args__ = (UniqueConstraint("subcontract_id", "source_mapping_id"),)  # no duplicates on retry
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    obligation_number: Mapped[str] = mapped_column(String(30))
    subcontract_id: Mapped[str] = mapped_column(ForeignKey("contracts_subcontracts.id"))
    source_mapping_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    internal_control_origin: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin_kind: Mapped[str] = mapped_column(String(30), default="CONTRACTUAL")  # or INTERNAL_CONTROL
    obligation_type: Mapped[str] = mapped_column(String(40))
    statement: Mapped[str] = mapped_column(Text)
    source_wording: Mapped[str] = mapped_column(Text)       # exact source text, kept apart from summary
    responsible_party: Mapped[str] = mapped_column(String(60), default="SUBCONTRACTOR")
    owner_role: Mapped[str] = mapped_column(String(60), default="contracts_manager")
    trigger: Mapped[str] = mapped_column(String(200), default="")
    notice_period_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    day_basis: Mapped[str] = mapped_column(String(20), default="CALENDAR")
    timezone: Mapped[str] = mapped_column(String(60), default="Asia/Kolkata")
    due_at: Mapped[str | None] = mapped_column(String(10), nullable=True)
    evidence_required: Mapped[str] = mapped_column(Text, default="")
    risk_level: Mapped[str] = mapped_column(String(10), default="MEDIUM")
    status: Mapped[str] = mapped_column(String(30), default="OPEN")


class EvidenceLink(Base):
    __tablename__ = "documents_evidence_links"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    obligation_id: Mapped[str] = mapped_column(ForeignKey("obligations_obligations.id"))
    document_revision_id: Mapped[str] = mapped_column(ForeignKey("documents_revisions.id"))
    submitted_by: Mapped[str] = mapped_column(String(36))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    note: Mapped[str] = mapped_column(Text, default="")
    review_status: Mapped[str] = mapped_column(String(20), default="SUBMITTED")  # ACCEPTED/REJECTED
    reviewed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------- governance
class AuditEvent(Base):
    """Append-only business-event ledger."""
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    actor_id: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(80))
    object_type: Mapped[str] = mapped_column(String(60))
    object_id: Mapped[str] = mapped_column(String(36))
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class IdempotencyRecord(Base):
    __tablename__ = "audit_idempotency_keys"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(36))
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
