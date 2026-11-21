"""Сборка датасета: разметка + сплиты + экспорт JSONL/CSV + Data Card."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from config.settings import Settings, get_settings
from legitfeed.dataset.labeling import assign_splits, label_document, split_ratios
from legitfeed.dataset.validation import (
    ValidationReport,
    dataframe_to_records,
    validate_dataframe,
)
from legitfeed.models import AnomalyRecord, CleanDoc, DatasetRow


@dataclass
class DatasetBundle:
    rows: list[DatasetRow]
    df: pd.DataFrame
    report: ValidationReport
    paths: dict[str, Path]

    def summary(self) -> dict:
        return {
            "rows": len(self.rows),
            "validation_passed": self.report.passed,
            "files": {k: str(v) for k, v in self.paths.items()},
            "stats": self.report.stats,
        }


def build_rows(
    docs: list[CleanDoc],
    anomalies: list[AnomalyRecord],
    settings: Settings | None = None,
) -> list[DatasetRow]:
    """Применить разметку и сплиты, вернуть строки датасета."""
    s = settings or get_settings()
    anomaly_map = {a.doc_id: a for a in anomalies}

    keep: list[CleanDoc] = []
    labels: dict[str, tuple[str, str, float, str]] = {}
    for doc in docs:
        anomaly = anomaly_map.get(doc.doc_id)
        if anomaly is not None and "duplicate" in anomaly.rule_hits:
            # точные/почти-дубликаты не нужны в датасете вообще
            continue
        label = label_document(doc, anomaly)
        keep.append(doc)
        labels[doc.doc_id] = (
            label.topic_label,
            label.label,
            label.confidence,
            "; ".join(label.reasons),
        )

    train_r, val_r, test_r = split_ratios(s)
    splits = assign_splits(
        keep, train_ratio=train_r, val_ratio=val_r, test_ratio=test_r, seed=s.random_seed
    )

    rows: list[DatasetRow] = []
    for doc in keep:
        anomaly = anomaly_map.get(doc.doc_id)
        topic, label, confidence, reasons = labels[doc.doc_id]
        rows.append(
            DatasetRow(
                doc_id=doc.doc_id,
                source_id=doc.source_id,
                url=doc.url,
                title=doc.title,
                text=doc.text,
                language=doc.language,
                published_at=doc.published_at,
                topic_label=topic,
                label=label,
                label_confidence=confidence,
                anomaly_score=anomaly.score if anomaly else 0.0,
                anomaly_reasons=reasons,
                split=splits.get(doc.doc_id, "train"),
                license=doc.license,
                license_url=doc.license_url,
                snapshot_at=doc.snapshot_at,
            )
        )
    return rows


def rows_to_dataframe(rows: list[DatasetRow]) -> pd.DataFrame:
    records = [r.model_dump() for r in rows]
    if not records:
        return pd.DataFrame(columns=list(DatasetRow.model_fields))
    return pd.DataFrame(records)


def export(df: pd.DataFrame, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    date_tag = datetime.now(UTC).strftime("%Y.%m.%d")

    jsonl = out_dir / "dataset.jsonl"
    with jsonl.open("w", encoding="utf-8") as f:
        for rec in dataframe_to_records(df):
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    paths["jsonl"] = jsonl

    csv = out_dir / "dataset.csv"
    df.to_csv(csv, index=False, encoding="utf-8-sig")
    paths["csv"] = csv

    # Per-split файлы — удобно для тренеров
    for split in ("train", "val", "test"):
        subset = df[df["split"] == split]
        if subset.empty:
            continue
        split_path = out_dir / f"{split}.jsonl"
        with split_path.open("w", encoding="utf-8") as f:
            for rec in dataframe_to_records(subset):
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        paths[split] = split_path

    metadata = {
        "name": "legitfeed-open-feed-corpus",
        "version": date_tag,
        "rows": len(df),
        "columns": list(df.columns),
        "license_note": "Каждая строка содержит license/license_url источника",
        "splits": {s: int((df["split"] == s).sum()) for s in ("train", "val", "test")},
        "labels": {k: int(v) for k, v in df["label"].value_counts().items()},
        "topics": {k: int(v) for k, v in df["topic_label"].value_counts().items()},
    }
    meta_path = out_dir / "dataset_meta.json"
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    paths["meta"] = meta_path
    return paths


def dataset_card(df: pd.DataFrame, report: ValidationReport) -> str:
    """Data Card — стандартное описание датасета для ML-команды."""
    stats = report.stats
    lines = [
        "# Dataset Card — LegitFeed Open Feed Corpus",
        "",
        "## Назначение",
        "Корпус публикаций из открытых RSS/Atom-фидов (python, AI/ML, data "
        "engineering). Предназначен для:",
        "",
        "* обучения и оценки классификатора тематики текста;",
        "* обучения детектора аномалий/качества данных;",
        "* бенчмарка шагов очистки данных.",
        "",
        "## Происхождение",
        "Источники перечислены в `config/sources.yaml`. Каждая строка содержит "
        "`url`, `license`, `license_url` и `snapshot_at`. Тексты прошли очистку и "
        "маскирование ПДн (152-ФЗ); персональные данные не хранятся.",
        "",
        "## Правовой статус",
        "* Собирались только публичные фиды с подтверждённым разрешением в ToS.",
        "* robots.txt проверялся по RFC 9309 (fail-closed при 5xx).",
        "* Датасет — производная выборка (excerpt limit), не полная перепубликация.",
        "",
        "## Схема",
        "| Колонка | Тип | Описание |",
        "|---|---|---|",
    ]
    descriptions = {
        "doc_id": "стабильный идентификатор документа",
        "source_id": "источник (см. sources.yaml)",
        "url": "канонический URL оригинала",
        "title": "заголовок (нормализованный, ПДн замаскированы)",
        "text": "очищенный текст (ПДн замаскированы)",
        "language": "ru/en/mixed/unknown",
        "published_at": "дата публикации (ISO 8601), может быть null",
        "topic_label": "тема (weak supervision)",
        "label": "метка: topic:<x> | needs_review | general",
        "label_confidence": "уверенность метки [0..1]",
        "anomaly_score": "суммарный вес сработавших правил",
        "anomaly_reasons": "объяснение разметки/аномалии на естественном языке",
        "split": "train/val/test",
        "license": "лицензия источника",
        "license_url": "ссылка на лицензию",
        "snapshot_at": "время снимка (provenance)",
    }
    for col in df.columns:
        lines.append(f"| `{col}` | {df[col].dtype} | {descriptions.get(col, '')} |")
    lines += [
        "",
        "## Распределение",
        f"* Строк: **{stats.get('rows', 0)}**",
        f"* Сплиты: {stats.get('by_split')}",
        f"* Метки: {stats.get('by_label')}",
        f"* Темы: {stats.get('by_topic')}",
        f"* Языки: {stats.get('by_language')}",
        f"* Записей с аномалиями: {stats.get('anomalies')}",
        "",
        "## Ограничения и этические замечания",
        "* Разметка слабая (правила), не заменяет ручную проверку.",
        "* Выборка смещена в сторону тех-контента на русском/английском.",
        "* Рекомендуется ручная валидация `needs_review`-подмножества.",
        "* Датасет не содержит ПДн; при переиспользовании проверьте лицензии источников.",
        "",
        "## Качество",
        f"Валидация: **{'PASSED' if report.passed else 'FAILED'}**, "
        f"ошибок: {len(report.errors)}, предупреждений: {len(report.warnings)}.",
        "Полный отчёт: `data/datasets/quality_report.json`.",
        "",
    ]
    return "\n".join(lines)


def build_dataset(
    docs: list[CleanDoc],
    anomalies: list[AnomalyRecord],
    settings: Settings | None = None,
    out_dir: Path | None = None,
) -> DatasetBundle:
    s = settings or get_settings()
    target = out_dir or s.path("datasets")
    rows = build_rows(docs, anomalies, s)
    df = rows_to_dataframe(rows)
    report = validate_dataframe(df)
    paths = export(df, target)
    report.save(target / "quality_report.json")
    card_path = target / "dataset_card.md"
    card_path.write_text(dataset_card(df, report), encoding="utf-8")
    paths["quality_report"] = target / "quality_report.json"
    paths["dataset_card"] = card_path
    return DatasetBundle(rows=rows, df=df, report=report, paths=paths)
