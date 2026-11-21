"""ORM-модели.

Схема намеренно «узкая»: никаких сырых HTML и никаких полей, где могут
лежать ПДн (152-ФЗ, минимизация). Тексты уже прошли маскирование.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class SourceRow(Base):
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    feed_url: Mapped[str] = mapped_column(String(1024))
    site_url: Mapped[str] = mapped_column(String(1024))
    license: Mapped[str] = mapped_column(String(512))
    tos_allow_feed: Mapped[bool] = mapped_column(Boolean, default=False)
    audited: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    documents: Mapped[list[DocumentRow]] = relationship(back_populates="source")


class DocumentRow(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("source_id", "content_hash", name="uq_doc_source_hash"),
    )

    doc_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    url: Mapped[str] = mapped_column(String(1024))
    canonical_url: Mapped[str] = mapped_column(String(1024), index=True)
    title: Mapped[str] = mapped_column(String(1024))
    text: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str] = mapped_column(String(16), default="unknown")
    published_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    snapshot_at: Mapped[str] = mapped_column(String(40))
    license: Mapped[str] = mapped_column(String(512), default="")
    license_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    pii_masked: Mapped[str] = mapped_column(String(255), default="")
    cleaning_flags: Mapped[str] = mapped_column(String(255), default="")
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    source: Mapped[SourceRow] = relationship(back_populates="documents")
    anomaly: Mapped[AnomalyRow | None] = relationship(back_populates="document", uselist=False)


class AnomalyRow(Base):
    __tablename__ = "anomalies"

    doc_id: Mapped[str] = mapped_column(ForeignKey("documents.doc_id"), primary_key=True)
    is_anomaly: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    rule_hits: Mapped[str] = mapped_column(Text, default="")
    explanation: Mapped[str] = mapped_column(Text, default="")
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")

    document: Mapped[DocumentRow] = relationship(back_populates="anomaly")


class DatasetRowModel(Base):
    __tablename__ = "dataset_rows"

    doc_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    url: Mapped[str] = mapped_column(String(1024))
    title: Mapped[str] = mapped_column(String(1024))
    text: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(16))
    published_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    topic_label: Mapped[str] = mapped_column(String(64), index=True)
    label: Mapped[str] = mapped_column(String(64), index=True)
    label_confidence: Mapped[float] = mapped_column(Float, default=1.0)
    anomaly_score: Mapped[float] = mapped_column(Float, default=0.0)
    anomaly_reasons: Mapped[str] = mapped_column(Text, default="")
    split: Mapped[str] = mapped_column(String(8), index=True)
    license: Mapped[str] = mapped_column(String(512), default="")
    license_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    snapshot_at: Mapped[str] = mapped_column(String(40))


class PipelineRunRow(Base):
    __tablename__ = "pipeline_runs"

    run_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    stage: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stats_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
