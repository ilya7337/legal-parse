"""Валидация датасета: схема, ПДн, дубликаты, баланс, leakage.

Возвращает ``ValidationReport`` с машиночитаемым списком проверок — его
результат показывается в API, сохраняется в ``quality_report.json`` и
используется в тестах.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

from legitfeed.etl.pii import mask_pii

REQUIRED_COLUMNS = [
    "doc_id",
    "source_id",
    "url",
    "title",
    "text",
    "language",
    "published_at",
    "topic_label",
    "label",
    "label_confidence",
    "anomaly_score",
    "anomaly_reasons",
    "split",
    "license",
    "license_url",
    "snapshot_at",
]

SPLITS = ["train", "val", "test"]


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    severity: str = "error"  # error | warning

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ValidationReport:
    checks: list[Check] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(c.passed or c.severity != "error" for c in self.checks)

    @property
    def errors(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and c.severity == "error"]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and c.severity == "warning"]

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "stats": self.stats,
            "checks": [c.as_dict() for c in self.checks],
            "errors": len(self.errors),
            "warnings": len(self.warnings),
        }

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def to_markdown(self) -> str:
        lines = ["# Отчёт качества датасета", ""]
        lines.append(f"Итог: {'PASSED' if self.passed else 'FAILED'}")
        lines.append("")
        lines.append("| Проверка | Результат | Детали |")
        lines.append("|---|---|---|")
        for c in self.checks:
            mark = "ok" if c.passed else ("WARN" if c.severity == "warning" else "FAIL")
            lines.append(f"| {c.name} | {mark} | {c.detail} |")
        lines.append("")
        lines.append("## Статистика")
        lines.append("")
        for k, v in self.stats.items():
            lines.append(f"* `{k}`: {v}")
        lines.append("")
        return "\n".join(lines)


def dataframe_to_records(df: pd.DataFrame) -> list[dict]:
    return json.loads(df.to_json(orient="records", force_ascii=False))


def validate_dataframe(df: pd.DataFrame) -> ValidationReport:
    report = ValidationReport()

    # 1. Схема
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    report.checks.append(
        Check(
            name="schema",
            passed=not missing,
            detail="все обязательные колонки присутствуют"
            if not missing
            else f"отсутствуют колонки: {missing}",
        )
    )
    if missing:
        return report

    # 2. Непустой датасет
    report.checks.append(
        Check(name="non_empty", passed=len(df) > 0, detail=f"строк: {len(df)}")
    )

    # 3. Нет пустых текстов/тайтлов
    empty_text = int((df["text"].astype(str).str.strip().str.len() == 0).sum())
    empty_title = int((df["title"].astype(str).str.strip().str.len() == 0).sum())
    report.checks.append(
        Check(
            name="no_empty_fields",
            passed=empty_text == 0 and empty_title == 0,
            detail=f"пустых text: {empty_text}, пустых title: {empty_title}",
        )
    )

    # 4. Уникальность doc_id
    dup_ids = int(df["doc_id"].duplicated().sum())
    report.checks.append(
        Check(name="unique_doc_id", passed=dup_ids == 0, detail=f"дублей doc_id: {dup_ids}")
    )

    # 5. Нет ПДн в тексте (критично для 152-ФЗ)
    pii_rows = 0
    pii_types: set[str] = set()
    for text in df["text"].astype(str):
        found = mask_pii(text).found
        if found:
            pii_rows += 1
            pii_types.update(found)
    report.checks.append(
        Check(
            name="no_personal_data",
            passed=pii_rows == 0,
            detail=f"строк с ПДн: {pii_rows}"
            + (f" (типы: {sorted(pii_types)})" if pii_types else ""),
        )
    )

    # 6. Сплиты корректны
    bad_splits = sorted(set(df["split"].unique()) - set(SPLITS))
    split_counts = df["split"].value_counts().to_dict()
    report.checks.append(
        Check(
            name="valid_splits",
            passed=not bad_splits,
            detail=f"распределение: {split_counts}"
            + (f"; неизвестные значения: {bad_splits}" if bad_splits else ""),
        )
    )

    # 7. Leakage: каждый источник должен присутствовать в train
    leakage_sources = [
        src for src, group in df.groupby("source_id") if "train" not in set(group["split"])
    ]
    report.checks.append(
        Check(
            name="split_coverage",
            passed=not leakage_sources,
            detail="каждый источник присутствует в train"
            if not leakage_sources
            else f"источники без train: {leakage_sources}",
            severity="warning",
        )
    )

    # 8. Provenance: url/license/snapshot обязательны (лицензионная чистота)
    missing_prov = int(
        (
            (df["url"].astype(str).str.strip() == "")
            | (df["license"].astype(str).str.strip() == "")
            | (df["snapshot_at"].astype(str).str.strip() == "")
        ).sum()
    )
    report.checks.append(
        Check(
            name="provenance",
            passed=missing_prov == 0,
            detail=f"строк без url/license/snapshot: {missing_prov}",
        )
    )

    # 9. Баланс меток (warning)
    label_counts = df["label"].value_counts()
    imbalance = float(label_counts.max() / label_counts.min()) if len(label_counts) > 1 else 1.0
    report.checks.append(
        Check(
            name="label_balance",
            passed=imbalance <= 10,
            detail=f"максимальный дисбаланс классов: {imbalance:.2f}; "
            f"классы: {label_counts.to_dict()}",
            severity="warning",
        )
    )

    # 10. Нет дублей текста
    text = df["text"].astype(str).str.lower().str.strip()
    dup_text = int(text.duplicated().sum())
    report.checks.append(
        Check(
            name="no_duplicate_text",
            passed=dup_text == 0,
            detail=f"дублированных текстов: {dup_text}",
        )
    )

    # 11. Длины текстов
    lens = df["text"].astype(str).str.len()
    report.checks.append(
        Check(
            name="text_length",
            passed=bool(lens.min() >= 50),
            detail=f"min={int(lens.min())}, median={int(lens.median())}, max={int(lens.max())}",
            severity="warning",
        )
    )

    report.stats = {
        "rows": len(df),
        "by_split": split_counts,
        "by_label": df["label"].value_counts().to_dict(),
        "by_topic": df["topic_label"].value_counts().to_dict(),
        "by_language": df["language"].value_counts().to_dict(),
        "by_source": df["source_id"].value_counts().to_dict(),
        "anomalies": int((df["anomaly_score"] > 0).sum()),
        "needs_review": int((df["label"] == "needs_review").sum()),
        "mean_text_len": round(float(lens.mean()), 1),
        "generated_at": df["snapshot_at"].max() if len(df) else None,
    }
    return report
