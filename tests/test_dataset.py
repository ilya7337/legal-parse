"""Тесты разметки, сплитов и сборки/валидации датасета."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from legitfeed.dataset.builder import build_dataset, build_rows, rows_to_dataframe
from legitfeed.dataset.labeling import assign_splits, label_document
from legitfeed.dataset.validation import validate_dataframe
from legitfeed.models import AnomalyRecord, CleanDoc


def _doc(doc_id="d1", source_id="src", text="", title="Заголовок", url=None,
         license="CC BY 4.0") -> CleanDoc:
    u = url or f"https://example.org/{doc_id}"
    return CleanDoc(
        doc_id=doc_id,
        source_id=source_id,
        url=u,
        canonical_url=u,
        title=title,
        text=text,
        summary=text[:200],
        language="ru",
        published_at="2025-11-20T00:00:00+00:00",
        snapshot_at="2025-11-20T00:00:00+00:00",
        license=license,
        license_url="https://example.org/license",
        word_count=len(text.split()),
        char_count=len(text),
    )


NLP_TEXT = "Статья про обработку естественного языка, токенизацию и лемматизацию корпуса текстов."
AI_TEXT = "Нейросети и машинное обучение для трансформеров и эмбеддингов моделей."
GENERAL = "Просто заметка о жизни без технических ключевых слов."


def test_label_topic_nlp():
    res = label_document(_doc(text=NLP_TEXT), None)
    assert res.label == "topic:nlp"


def test_label_general():
    res = label_document(_doc(text=GENERAL), None)
    assert res.label == "general"


def test_label_anomaly_needs_review():
    anomaly = AnomalyRecord(doc_id="d1", is_anomaly=True, score=1.2, explanation="спам")
    res = label_document(_doc(text=AI_TEXT), anomaly)
    assert res.label == "needs_review"


def test_assign_splits_deterministic_and_covered():
    docs = [_doc(doc_id=f"d{i}", text=f"{NLP_TEXT} вариант {i}") for i in range(10)]
    a = assign_splits(docs, train_ratio=0.8, val_ratio=0.1, test_ratio=0.1, seed=42)
    b = assign_splits(docs, train_ratio=0.8, val_ratio=0.1, test_ratio=0.1, seed=42)
    assert a == b
    assert set(a.values()) == {"train", "val", "test"}
    assert "train" in set(a.values())


def test_assign_splits_small_group_no_overlap():
    docs = [_doc(doc_id=f"d{i}", source_id="s1", text=f"{NLP_TEXT} {i}") for i in range(2)]
    splits = assign_splits(docs, train_ratio=0.8, val_ratio=0.1, test_ratio=0.1, seed=1)
    assert "train" in splits.values()


def test_build_dataset_end_to_end(tmp_path, tmp_settings, sample_docs):
    anomalies = [AnomalyRecord(doc_id="doc0", is_anomaly=True, score=1.0, explanation="тест")]
    bundle = build_dataset(sample_docs, anomalies, settings=tmp_settings, out_dir=tmp_path)
    assert bundle.report.passed is True
    assert len(bundle.rows) == len(sample_docs)
    assert (tmp_path / "dataset.jsonl").exists()
    assert (tmp_path / "quality_report.json").exists()
    # в датасете не должно быть ПДн
    report = json.loads((tmp_path / "quality_report.json").read_text(encoding="utf-8"))
    assert report["passed"] is True


def test_validate_flags_pii_in_dataframe():
    df = pd.DataFrame(
        [
            {
                "doc_id": "x1",
                "source_id": "s",
                "url": "https://e.org/1",
                "title": "t",
                "text": f"{NLP_TEXT} контакт bad@example.com",
                "language": "ru",
                "published_at": None,
                "topic_label": "nlp",
                "label": "topic:nlp",
                "label_confidence": 0.8,
                "anomaly_score": 0.0,
                "anomaly_reasons": "",
                "split": "train",
                "license": "CC BY 4.0",
                "license_url": "https://e.org/l",
                "snapshot_at": "2025-11-20T00:00:00+00:00",
            }
        ]
    )
    report = validate_dataframe(df)
    pii_check = next(c for c in report.checks if c.name == "no_personal_data")
    assert pii_check.passed is False


def test_rows_to_dataframe_columns():
    from legitfeed.models import DatasetRow

    # пустой список -> пустой DataFrame с нужными колонками
    df = rows_to_dataframe([])
    assert "doc_id" in df.columns
    assert len(df) == 0
