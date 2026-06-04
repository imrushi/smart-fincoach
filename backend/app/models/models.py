"""SQLAlchemy models — canonical ledger schema."""
import uuid
from datetime import datetime, date
from decimal import Decimal
from enum import Enum as PyEnum

from sqlalchemy import (
    String, Text, Numeric, DateTime, Date, Boolean, Integer,
    ForeignKey, Enum, Index, UniqueConstraint, CheckConstraint
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID, ARRAY, JSONB
from pgvector.sqlalchemy import Vector

from app.core.database import Base


# ── Enums ────────────────────────────────────────────────────────────────────

class TransactionType(str, PyEnum):
    EXPENSE = "EXPENSE"
    INCOME = "INCOME"
    TRANSFER = "TRANSFER"
    REFUND = "REFUND"
    INVESTMENT = "INVESTMENT"


class SourceType(str, PyEnum):
    ICICI_BANK = "ICICI_BANK"
    HDFC_BANK = "HDFC_BANK"
    SBI_BANK = "SBI_BANK"
    BOB_BANK = "BOB_BANK"
    PHONEPE = "PHONEPE"
    GOOGLEPAY = "GOOGLEPAY"
    CRED = "CRED"
    IMOBILE = "IMOBILE"
    MANUAL = "MANUAL"


class ReconciliationMethod(str, PyEnum):
    UTR_EXACT = "UTR_EXACT"
    AMOUNT_DATE_FUZZY = "AMOUNT_DATE_FUZZY"
    MANUAL = "MANUAL"
    UNMATCHED = "UNMATCHED"


# ── Upload tracking ──────────────────────────────────────────────────────────

class Upload(Base):
    __tablename__ = "uploads"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    filename: Mapped[str] = mapped_column(String(500))
    source_type: Mapped[SourceType] = mapped_column(Enum(SourceType))
    encrypted_path: Mapped[str] = mapped_column(String(1000), nullable=True)
    file_hash: Mapped[str] = mapped_column(String(64), index=True, comment="SHA-256 to prevent duplicate uploads")
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    parsed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    parse_errors: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    transactions = relationship("Transaction", back_populates="upload")


# ── Canonical transaction (bank = source of truth) ───────────────────────────

class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    upload_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("uploads.id"))

    # Core fields
    txn_date: Mapped[date] = mapped_column(Date, index=True)
    txn_datetime: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    is_debit: Mapped[bool] = mapped_column(Boolean)
    balance_after: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)

    # Source info
    source_type: Mapped[SourceType] = mapped_column(Enum(SourceType))
    raw_narration: Mapped[str] = mapped_column(Text, default="")
    bank_ref: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="Bank reference number")
    utr: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True, comment="UTR/RRN — primary reconciliation key")
    cheque_no: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Enrichment (from UPI app matching)
    counterparty_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    counterparty_upi_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    payment_app_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Classification
    transaction_type: Mapped[TransactionType] = mapped_column(Enum(TransactionType), default=TransactionType.EXPENSE)
    category_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("categories.id"), nullable=True)
    categorization_source: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="RULE|EMBEDDING|LLM|MANUAL")
    categorization_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)

    # Reconciliation
    reconciled_with_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("transactions.id"), nullable=True)
    reconciliation_method: Mapped[ReconciliationMethod] = mapped_column(Enum(ReconciliationMethod), default=ReconciliationMethod.UNMATCHED)

    # Self-transfer detection
    is_self_transfer: Mapped[bool] = mapped_column(Boolean, default=False)

    # Metadata
    is_recurring: Mapped[bool] = mapped_column(Boolean, default=False)
    tags: Mapped[list | None] = mapped_column(ARRAY(String), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    upload = relationship("Upload", back_populates="transactions")
    category = relationship("Category", back_populates="transactions")

    __table_args__ = (
        Index("ix_txn_date_amount", "txn_date", "amount"),
        Index("ix_txn_utr", "utr", postgresql_where="utr IS NOT NULL"),
    )


# ── Categories ───────────────────────────────────────────────────────────────

class Category(Base):
    __tablename__ = "categories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    icon: Mapped[str] = mapped_column(String(10), default="💰")
    color: Mapped[str] = mapped_column(String(7), default="#6366f1")
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, comment="System categories can't be deleted")

    transactions = relationship("Transaction", back_populates="category")
    budgets = relationship("Budget", back_populates="category")


# ── Merchant → Category cache (key cost-control mechanism) ───────────────────

class MerchantCategoryCache(Base):
    __tablename__ = "merchant_category_cache"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_name: Mapped[str] = mapped_column(String(500), unique=True, index=True)
    merchant_normalized: Mapped[str] = mapped_column(String(500), index=True)
    category_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("categories.id"))
    source: Mapped[str] = mapped_column(String(50), comment="RULE|EMBEDDING|LLM|MANUAL")
    embedding: Mapped[list | None] = mapped_column(Vector(1536), nullable=True)
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), default=1.0)
    override_count: Mapped[int] = mapped_column(Integer, default=0, comment="Times user confirmed/overrode")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    category = relationship("Category")


# ── Budgets ──────────────────────────────────────────────────────────────────

class Budget(Base):
    __tablename__ = "budgets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    category_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("categories.id"))
    month: Mapped[date] = mapped_column(Date, comment="First day of month, e.g. 2026-05-01")
    limit_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    is_adaptive: Mapped[bool] = mapped_column(Boolean, default=True, comment="Auto-set from median of last N months")
    alert_threshold_pct: Mapped[int] = mapped_column(Integer, default=80)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    category = relationship("Category", back_populates="budgets")

    __table_args__ = (
        UniqueConstraint("category_id", "month", name="uq_budget_category_month"),
    )


# ── Alerts ───────────────────────────────────────────────────────────────────

class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    category_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("categories.id"), nullable=True)
    alert_type: Mapped[str] = mapped_column(String(50), comment="THRESHOLD|RUNRATE|ANOMALY|INSIGHT")
    title: Mapped[str] = mapped_column(String(500))
    message: Mapped[str] = mapped_column(Text)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    category = relationship("Category")


# ── Category rules (YAML-driven, highest priority) ──────────────────────────

class CategoryRule(Base):
    __tablename__ = "category_rules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pattern: Mapped[str] = mapped_column(String(500), comment="Regex or substring to match narration")
    category_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("categories.id"))
    priority: Mapped[int] = mapped_column(Integer, default=0, comment="Higher = checked first")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(50), default="MANUAL", comment="SEED|MANUAL|AUTO_PROMOTED")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    category = relationship("Category")

