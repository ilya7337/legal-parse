"""Weak-supervision разметка и разбиение на train/val/test.

Без ручной разметки на MVP используем правила. Важно: аномалии **не
выбрасываются** — они уходят в отдельный датасет-срез ``needs_review`` и
могут использоваться для обучения детектора аномалий.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

from config.settings import Settings, get_settings
from legitfeed.models import AnomalyRecord, CleanDoc

# Тематические правила: имя темы -> набор regex
TOPIC_RULES: dict[str, list[re.Pattern[str]]] = {
    "ai": [
        re.compile(r"\b(нейросет\w*|neural|ml|машинн\w+ обучен\w+|deep learning)\b", re.IGNORECASE),
        re.compile(r"\b(llm|gpt|трансформер|transformer|embedding|эмбеддинг)\b", re.IGNORECASE),
    ],
    "nlp": [
        re.compile(r"\b(nlp|обработк\w+ естественн\w+ язык\w+|токенизац\w+|лемматизац\w+)\b", re.IGNORECASE),
        re.compile(r"\b(bert|tokenizer|corpus|корпус текстов)\b", re.IGNORECASE),
    ],
    "data_engineering": [
        re.compile(r"\b(etl|airflow|пайплайн|pipeline|парсинг|scrap\w+|краулинг)\b", re.IGNORECASE),
        re.compile(r"\b(postgres\w*|sql|база данных|dataset|датасет)\b", re.IGNORECASE),
    ],
    "python": [
        re.compile(r"\b(python|питон|pandas|numpy|asyncio|django|fastapi|flask)\b", re.IGNORECASE),
        re.compile(r"\b(pypi|pep\s?\d+|virtualenv|python 3\.\d+)\b", re.IGNORECASE),
    ],
    "legal_data": [
        re.compile(r"\b(152-?фз|персональн\w+ данн\w+|robots\.txt|rfc 9309|to?S|лицензи\w+)\b", re.IGNORECASE),
        re.compile(r"\b(гдпр|gdpr|правов\w+ аспект\w+|юридическ\w+)\b", re.IGNORECASE),
    ],
}


@dataclass
class LabelResult:
    topic_label: str
    label: str
    confidence: float
    reasons: list[str]


def classify_topic(doc: CleanDoc) -> tuple[str, float, list[str]]:
    """Определить тему по ключевым словам. Возвращает (topic, confidence, reasons)."""
    haystack = f"{doc.title} {doc.text}"
    scores: dict[str, int] = {}
    for topic, patterns in TOPIC_RULES.items():
        hits = sum(len(p.findall(haystack)) for p in patterns)
        if hits:
            scores[topic] = hits
    # теги источника тоже усиливают тему
    tag_text = " ".join(doc.tags).lower()
    for topic, patterns in TOPIC_RULES.items():
        if any(p.search(tag_text) for p in patterns):
            scores[topic] = scores.get(topic, 0) + 2
    if not scores:
        return "general", 0.3, ["Ключевых слов тематик не найдено — присвоена общая метка"]
    topic, top = max(scores.items(), key=lambda kv: kv[1])
    total = sum(scores.values()) or 1
    confidence = round(min(0.95, 0.4 + 0.6 * (top / total)), 2)
    return topic, confidence, [f"Тема '{topic}': {top} совпадений из {total}"]


def label_document(doc: CleanDoc, anomaly: AnomalyRecord | None) -> LabelResult:
    """Приоритет: аномалия -> needs_review; иначе тема; иначе general."""
    if anomaly is not None and anomaly.is_anomaly:
        return LabelResult(
            topic_label="general",
            label="needs_review",
            confidence=0.5,
            reasons=[f"Найдена аномалия (score={anomaly.score}): {anomaly.explanation}"],
        )
    topic, confidence, reasons = classify_topic(doc)
    return LabelResult(
        topic_label=topic,
        label=f"topic:{topic}" if topic != "general" else "general",
        confidence=confidence,
        reasons=reasons,
    )


def assign_splits(
    docs: list[CleanDoc],
    *,
    train_ratio: float,
    val_ratio: float,
    test_ratio: float,
    seed: int,
) -> dict[str, str]:
    """Стратифицированное разбиение по (source_id, topic он же label-источник).

    Пространство ключей — source_id, чтобы разные источники не «утекали»
    между сплитами (защита от leakage по стилю источника).
    """
    rng = random.Random(seed)
    groups: dict[str, list[str]] = {}
    for doc in docs:
        groups.setdefault(doc.source_id, []).append(doc.doc_id)

    splits = {"train": [], "val": [], "test": []}
    for _, doc_ids in groups.items():
        shuffled = doc_ids[:]
        rng.shuffle(shuffled)
        n = len(shuffled)
        # При малом n важно, чтобы val/test получали хотя бы по одному объекту,
        # но train всегда оставался непустым.
        if n >= 3:
            n_val = max(1, round(n * val_ratio))
            n_test = max(1, round(n * test_ratio))
            n_train = n - n_val - n_test
            if n_train < 1:
                n_train = 1
                overflow = n_val + n_test - (n - 1)
                while overflow > 0:
                    if n_val >= n_test and n_val > 1:
                        n_val -= 1
                    elif n_test > 1:
                        n_test -= 1
                    else:
                        break
                    overflow -= 1
        elif n == 2:
            n_train, n_val, n_test = 1, 1, 0
        else:
            n_train, n_val, n_test = n, 0, 0
        train = shuffled[:n_train]
        val = shuffled[n_train : n_train + n_val]
        test = shuffled[n_train + n_val : n_train + n_val + n_test]
        splits["train"].extend(train)
        splits["val"].extend(val)
        splits["test"].extend(test)

    mapping: dict[str, str] = {}
    for name, ids in splits.items():
        for doc_id in ids:
            mapping[doc_id] = name
    return mapping


def split_ratios(settings: Settings | None = None) -> tuple[float, float, float]:
    s = settings or get_settings()
    total = s.train_ratio + s.val_ratio + s.test_ratio
    return s.train_ratio / total, s.val_ratio / total, s.test_ratio / total
