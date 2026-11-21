"""Тесты детектора аномалий: правила + статистика + TF-IDF + объяснения."""

from __future__ import annotations

from legitfeed.anomaly.detector import AnomalyDetector, TfidfCentroid, build_profile
from legitfeed.anomaly.rules import rule_pii_present, rule_spam
from legitfeed.models import CleanDoc


def _doc(doc_id="d1", source_id="src", text="", pii_masked=None, word_count=None, char_count=None,
         url="https://example.org/1", cleaning_flags=None) -> CleanDoc:
    return CleanDoc(
        doc_id=doc_id,
        source_id=source_id,
        url=url,
        canonical_url=url,
        title="Заголовок про данные и машинное обучение",
        text=text,
        summary=text[:200],
        language="ru",
        snapshot_at="2025-11-20T00:00:00+00:00",
        license="CC BY 4.0",
        word_count=word_count if word_count is not None else len(text.split()),
        char_count=char_count if char_count is not None else len(text),
        pii_masked=pii_masked or [],
        cleaning_flags=cleaning_flags or [],
    )


CLEAN = (
    "В статье мы разбираем построение ETL-пайплайна на Python и Airflow. Показываем "
    "настройку DAG, очистку и валидацию данных, а также метрики выполнения задач."
)

SPAM = (
    "ЗАРАБОТОК БЕЗ ВЛОЖЕНИЙ!!! КАЗИНО СТАВКИ!!! https://spam1.example "
    "https://spam2.example https://spam3.example https://spam4.example https://spam5.example"
)


def test_rule_spam_triggers_on_many_links_and_caps():
    hit = rule_spam(_doc(text=SPAM))
    assert hit is not None
    assert hit.rule == "spam_like"


def test_rule_spam_silent_on_clean_text():
    assert rule_spam(_doc(text=CLEAN)) is None


def test_rule_pii_triggers_on_masked_pii():
    hit = rule_pii_present(_doc(text=CLEAN, pii_masked=["email"]))
    assert hit is not None
    assert "персональные данные" in hit.explanation


def test_detector_clean_doc_not_anomaly():
    detector = AnomalyDetector()
    rec = detector.detect(_doc(text=CLEAN))
    assert rec.is_anomaly is False


def test_detector_spam_doc_is_anomaly():
    detector = AnomalyDetector()
    rec = detector.detect(_doc(text=SPAM))
    assert rec.is_anomaly is True
    assert "spam_like" in rec.rule_hits


def test_detector_length_outlier_with_history():
    detector = AnomalyDetector(z_threshold=3.0, min_history=3)
    docs = [CLEAN] * 4  # одинаковая длина -> tiny stdev
    rec = detector.detect(_doc(text=CLEAN), history=[_doc(text=t) for t in docs])
    # при нулевой дисперсии z=0, выброса нет
    assert "length_outlier" not in rec.rule_hits


def test_tfidf_centroid_off_topic_low_similarity():
    on_topic = [
        "нейронные сети и машинное обучение для классификации текстов данных моделей",
        "обучение моделей классификации текстов на нейронных сетях с учителем",
        "машинное обучение нейросетей классификация текстовых данных модели",
    ]
    centroid = TfidfCentroid(on_topic)
    off_topic = "рецепт приготовления борща со свеклой и капустой на обед домой"
    assert centroid.cosine_to_centroid(off_topic) < 0.2


def test_tfidf_centroid_top_terms():
    centroid = TfidfCentroid(["данные данные модели обучение классификация"])
    assert "данные" in centroid.top_terms(3)


def test_build_profile_stats():
    docs = [_doc(doc_id=f"d{i}", text=f"{CLEAN} выпуск {i}") for i in range(5)]
    profile = build_profile(docs)
    assert profile.median_chars > 0
    assert profile.global_centroid is not None
