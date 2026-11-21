"""Репозитории: единый фасад над SQLAlchemy-сессией (SQLite <-> PostgreSQL)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Sequence

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from config.settings import get_settings
from legitfeed.legal.registry import Source
from legitfeed.models import AnomalyRecord, CleanDoc, DatasetRow
from legitfeed.storage.models import (
    AnomalyRow,
    Base,
    DatasetRowModel,
    DocumentRow,
    PipelineRunRow,
    SourceRow,
)


class Repository:
    """Все операции записи/чтения проходят через этот класс."""

    def __init__(self, database_url: str | None = None, *, echo: bool = False):
        settings = get_settings()
        self.database_url = database_url or settings.database_url
        connect_args = {}
        if self.database_url.startswith("sqlite"):
            connect_args = {"check_same_thread": False}
        self.engine = create_engine(
            self.database_url, future=True, echo=echo, connect_args=connect_args
        )
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        Base.metadata.create_all(self.engine)

    def session(self) -> Session:
        return self.session_factory()

    # ---------- sources ----------

    def upsert_source(self, source: Source) -> None:
        with self.session() as s:
            row = s.get(SourceRow, source.id)
            if row is None:
                row = SourceRow(id=source.id)
                s.add(row)
            row.name = source.name
            row.feed_url = str(source.feed_url)
            row.site_url = str(source.site_url)
            row.license = source.license
            row.tos_allow_feed = source.tos_allow_feed
            row.audited = source.is_audited
            s.commit()

    def list_sources(self) -> list[dict]:
        with self.session() as s:
            rows = s.scalars(select(SourceRow).order_by(SourceRow.id)).all()
            return [
                {
                    "id": r.id,
                    "name": r.name,
                    "feed_url": r.feed_url,
                    "license": r.license,
                    "tos_allow_feed": r.tos_allow_feed,
                    "audited": r.audited,
                }
                for r in rows
            ]

    # ---------- documents ----------

    def upsert_documents(self, docs: list[CleanDoc]) -> int:
        if not docs:
            return 0
        inserted = 0
        with self.session() as s:
            doc_ids = [d.doc_id for d in docs]
            hashes = [d.content_hash for d in docs]
            existing_ids = set(
                s.scalars(select(DocumentRow.doc_id).where(DocumentRow.doc_id.in_(doc_ids))).all()
            )
            existing_hashes = set(
                s.scalars(
                    select(DocumentRow.content_hash).where(DocumentRow.content_hash.in_(hashes))
                ).all()
            )
            for doc in docs:
                if doc.doc_id in existing_ids or doc.content_hash in existing_hashes:
                    continue
                s.add(
                    DocumentRow(
                        doc_id=doc.doc_id,
                        source_id=doc.source_id,
                        url=doc.url,
                        canonical_url=doc.canonical_url,
                        title=doc.title,
                        text=doc.text,
                        summary=doc.summary,
                        language=doc.language,
                        published_at=doc.published_at,
                        snapshot_at=doc.snapshot_at,
                        license=doc.license,
                        license_url=doc.license_url,
                        word_count=doc.word_count,
                        char_count=doc.char_count,
                        pii_masked=",".join(doc.pii_masked),
                        cleaning_flags=",".join(doc.cleaning_flags),
                        content_hash=doc.content_hash,
                    )
                )
                inserted += 1
            s.commit()
        return inserted

    def load_documents(self, source_id: str | None = None) -> list[CleanDoc]:
        with self.session() as s:
            stmt = select(DocumentRow).order_by(DocumentRow.created_at)
            if source_id:
                stmt = stmt.where(DocumentRow.source_id == source_id)
            rows = s.scalars(stmt).all()
            return [
                CleanDoc(
                    doc_id=r.doc_id,
                    source_id=r.source_id,
                    url=r.url,
                    canonical_url=r.canonical_url,
                    title=r.title,
                    text=r.text,
                    summary=r.summary,
                    language=r.language,
                    published_at=r.published_at,
                    snapshot_at=r.snapshot_at,
                    license=r.license,
                    license_url=r.license_url,
                    word_count=r.word_count,
                    char_count=r.char_count,
                    pii_masked=[x for x in r.pii_masked.split(",") if x],
                    cleaning_flags=[x for x in r.cleaning_flags.split(",") if x],
                    content_hash=r.content_hash,
                )
                for r in rows
            ]

    def count_documents(self) -> int:
        with self.session() as s:
            return int(s.scalar(select(func.count()).select_from(DocumentRow)) or 0)

    # ---------- anomalies ----------

    def upsert_anomalies(self, records: list[AnomalyRecord]) -> int:
        if not records:
            return 0
        with self.session() as s:
            for rec in records:
                row = s.get(AnomalyRow, rec.doc_id)
                if row is None:
                    row = AnomalyRow(doc_id=rec.doc_id)
                    s.add(row)
                row.is_anomaly = rec.is_anomaly
                row.score = rec.score
                row.rule_hits = ",".join(rec.rule_hits)
                row.explanation = rec.explanation
                row.metrics_json = json.dumps(rec.metrics, ensure_ascii=False, default=str)
            s.commit()
        return len(records)

    def load_anomalies(self, only_anomalies: bool = False) -> list[dict]:
        with self.session() as s:
            stmt = select(AnomalyRow).order_by(AnomalyRow.score.desc())
            if only_anomalies:
                stmt = stmt.where(AnomalyRow.is_anomaly.is_(True))
            rows = s.scalars(stmt).all()
            return [
                {
                    "doc_id": r.doc_id,
                    "is_anomaly": r.is_anomaly,
                    "score": r.score,
                    "rule_hits": [x for x in r.rule_hits.split(",") if x],
                    "explanation": r.explanation,
                    "metrics": json.loads(r.metrics_json or "{}"),
                }
                for r in rows
            ]

    # ---------- dataset ----------

    def replace_dataset(self, rows: list[DatasetRow]) -> int:
        with self.session() as s:
            for existing in s.scalars(select(DatasetRowModel)).all():
                s.delete(existing)
            s.flush()
            for row in rows:
                s.add(
                    DatasetRowModel(
                        doc_id=row.doc_id,
                        source_id=row.source_id,
                        url=row.url,
                        title=row.title,
                        text=row.text,
                        language=row.language,
                        published_at=row.published_at,
                        topic_label=row.topic_label,
                        label=row.label,
                        label_confidence=row.label_confidence,
                        anomaly_score=row.anomaly_score,
                        anomaly_reasons=row.anomaly_reasons,
                        split=row.split,
                        license=row.license,
                        license_url=row.license_url,
                        snapshot_at=row.snapshot_at,
                    )
                )
            s.commit()
        return len(rows)

    def load_dataset(self) -> list[dict]:
        with self.session() as s:
            rows: Sequence[DatasetRowModel] = s.scalars(
                select(DatasetRowModel).order_by(DatasetRowModel.doc_id)
            ).all()
            return [
                {
                    "doc_id": r.doc_id,
                    "source_id": r.source_id,
                    "url": r.url,
                    "title": r.title,
                    "text": r.text,
                    "language": r.language,
                    "published_at": r.published_at,
                    "topic_label": r.topic_label,
                    "label": r.label,
                    "label_confidence": r.label_confidence,
                    "anomaly_score": r.anomaly_score,
                    "anomaly_reasons": r.anomaly_reasons,
                    "split": r.split,
                    "license": r.license,
                    "license_url": r.license_url,
                    "snapshot_at": r.snapshot_at,
                }
                for r in rows
            ]

    # ---------- pipeline runs ----------

    def start_run(self, run_id: str, stage: str) -> None:
        with self.session() as s:
            s.add(PipelineRunRow(run_id=run_id, stage=stage, status="running"))
            s.commit()

    def finish_run(self, run_id: str, status: str, stats: dict, error: str | None = None) -> None:
        with self.session() as s:
            row = s.get(PipelineRunRow, run_id)
            if row is None:
                row = PipelineRunRow(run_id=run_id, stage="-", status=status)
                s.add(row)
            row.status = status
            row.finished_at = datetime.now(UTC)
            row.stats_json = json.dumps(stats, ensure_ascii=False, default=str)
            row.error = error
            s.commit()

    def last_runs(self, limit: int = 20) -> list[dict]:
        with self.session() as s:
            rows = s.scalars(
                select(PipelineRunRow).order_by(PipelineRunRow.started_at.desc()).limit(limit)
            ).all()
            return [
                {
                    "run_id": r.run_id,
                    "stage": r.stage,
                    "status": r.status,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                    "stats": json.loads(r.stats_json or "{}"),
                    "error": r.error,
                }
                for r in rows
            ]

    # ---------- summary ----------

    def summary(self) -> dict:
        with self.session() as s:
            docs = int(s.scalar(select(func.count()).select_from(DocumentRow)) or 0)
            anomalies = int(
                s.scalar(
                    select(func.count())
                    .select_from(AnomalyRow)
                    .where(AnomalyRow.is_anomaly.is_(True))
                )
                or 0
            )
            dataset = int(s.scalar(select(func.count()).select_from(DatasetRowModel)) or 0)
        return {"documents": docs, "anomalies": anomalies, "dataset_rows": dataset}

    def dispose(self) -> None:
        self.engine.dispose()
