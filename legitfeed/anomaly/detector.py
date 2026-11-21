"""Детектор аномалий: правила + статистика + NLP (TF-IDF), с объяснениями.

Три слоя:
  1. Правила (``rules.py``) — детерминированные проверки качества/спама/ПДн.
  2. Статистика — z-score по длине текста относительно истории источника
     и относительно всего корпуса.
  3. NLP — косинусное расстояние до центроида TF-IDF: «запись не о том,
     о чём обычно пишет источник».

Каждая проверка возвращает **объяснение на естественном языке** — это и есть
explainable anomaly detection: пользователь видит не только score, но и почему.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from statistics import median

from legitfeed.anomaly.rules import ALL_RULES, RuleHit, rule_health
from legitfeed.models import AnomalyRecord, CleanDoc, collapse_ws

_STOPWORDS = {
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то", "все",
    "она", "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за", "бы", "по",
    "только", "ее", "мне", "было", "вот", "от", "меня", "еще", "нет", "о", "из",
    "ему", "теперь", "когда", "даже", "ну", "вдруг", "ли", "если", "уже", "или",
    "ни", "быть", "был", "него", "до", "вас", "нибудь", "опять", "уж", "вам",
    "ведь", "там", "потом", "себя", "ничего", "ей", "может", "они", "тут", "где",
    "есть", "надо", "ней", "для", "мы", "тебя", "их", "чем", "была", "сам", "чтоб",
    "без", "будто", "чего", "раз", "тоже", "себе", "под", "будет", "ж", "тогда",
    "who", "the", "a", "an", "and", "or", "of", "to", "in", "is", "are", "for",
    "on", "with", "as", "at", "by", "this", "that", "it", "be", "from", "we",
}


def tokenize(text: str) -> list[str]:
    """Простая токенизация: lower-case, слова, без стоп-слов, длина не менее 2."""
    tokens: list[str] = []
    for word in collapse_ws(text).split():
        token = word.strip("_").lower().split("'")[0]
        if len(token) >= 2 and token not in _STOPWORDS and token.replace("-", "").isalnum():
            tokens.append(token)
    return tokens


class TfidfCentroid:
    """Мини TF-IDF + центроид корпуса без sklearn (контроль зависимостей)."""

    def __init__(self, docs: list[str]):
        self.doc_tokens = [tokenize(d) for d in docs]
        n = max(1, len(self.doc_tokens))
        df: Counter[str] = Counter()
        for toks in self.doc_tokens:
            df.update(set(toks))
        self.idf = {term: math.log((1 + n) / (1 + c)) + 1.0 for term, c in df.items()}
        self.size = n
        self.tf = [self._tf(toks) for toks in self.doc_tokens]
        self.centroid = self._centroid(self.tf)

    def _tf(self, tokens: list[str]) -> dict[str, float]:
        counts = Counter(tokens)
        total = sum(counts.values()) or 1
        return {t: c / total * self.idf.get(t, 1.0) for t, c in counts.items()}

    @staticmethod
    def _norm(vec: dict[str, float]) -> float:
        return math.sqrt(sum(v * v for v in vec.values())) or 1.0

    def _centroid(self, tfs: list[dict[str, float]]) -> dict[str, float]:
        acc: dict[str, float] = defaultdict(float)
        for vec in tfs:
            norm = self._norm(vec)
            for k, v in vec.items():
                acc[k] += v / norm
        if not tfs:
            return {}
        for k in acc:
            acc[k] /= len(tfs)
        return dict(acc)

    def cosine_to_centroid(self, text: str) -> float:
        if not self.centroid:
            return 1.0
        vec = self._tf(tokenize(text))
        keys = set(vec) & set(self.centroid)
        if not keys:
            return 0.0
        dot = sum(vec[k] * self.centroid[k] for k in keys)
        return dot / (self._norm(vec) * self._norm(self.centroid))

    def top_terms(self, k: int = 8) -> list[str]:
        return [t for t, _ in sorted(self.centroid.items(), key=lambda kv: -kv[1])[:k]]


@dataclass
class CorpusProfile:
    """Профиль корпуса: z-score и TF-IDF-центроиды."""

    char_counts: list[int] = field(default_factory=list)
    word_counts: list[int] = field(default_factory=list)
    global_centroid: TfidfCentroid | None = None
    per_source_centroid: dict[str, TfidfCentroid] = field(default_factory=dict)
    median_chars: float = 0.0
    stdev_chars: float = 0.0
    median_words: float = 0.0
    stdev_words: float = 0.0


def _stats(values: list[int]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    med = float(median(values))
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return med, math.sqrt(var)


def _z(value: float, med: float, sd: float) -> float:
    if sd <= 0:
        return 0.0
    return (value - med) / sd


def build_profile(docs: list[CleanDoc]) -> CorpusProfile:
    prof = CorpusProfile()
    prof.char_counts = [d.char_count for d in docs]
    prof.word_counts = [d.word_count for d in docs]
    prof.median_chars, prof.stdev_chars = _stats(prof.char_counts)
    prof.median_words, prof.stdev_words = _stats(prof.word_counts)
    texts = [d.text for d in docs]
    prof.global_centroid = TfidfCentroid(texts) if texts else None
    by_source: dict[str, list[str]] = defaultdict(list)
    for d in docs:
        by_source[d.source_id].append(d.text)
    for src, texts_src in by_source.items():
        if len(texts_src) >= 3:
            prof.per_source_centroid[src] = TfidfCentroid(texts_src)
    return prof


class AnomalyDetector:
    def __init__(
        self,
        *,
        z_threshold: float = 3.0,
        min_history: int = 5,
        min_chars: int = 120,
        on_threshold: float = 1.0,
        centroid_threshold: float = 0.05,
    ):
        self.z_threshold = z_threshold
        self.min_history = min_history
        self.min_chars = min_chars
        self.on_threshold = on_threshold
        self.centroid_threshold = centroid_threshold

    def detect(
        self,
        doc: CleanDoc,
        profile: CorpusProfile | None = None,
        history: list[CleanDoc] | None = None,
    ) -> AnomalyRecord:
        hits: list[RuleHit] = []
        metrics: dict = {}

        # 1. Правила
        for rule in ALL_RULES:
            if rule is rule_health:
                hit = rule(doc, min_chars=self.min_chars)
            else:
                hit = rule(doc)
            if hit:
                hits.append(hit)
                metrics.update(hit.metrics)

        # 2. Статистика по истории источника
        hist = history or []
        if len(hist) >= self.min_history:
            med_w, sd_w = _stats([h.word_count for h in hist])
            z_w = _z(doc.word_count, med_w, sd_w)
            metrics["z_words"] = round(z_w, 2)
            if abs(z_w) >= self.z_threshold:
                direction = "длиннее" if z_w > 0 else "короче"
                hits.append(
                    RuleHit(
                        rule="length_outlier",
                        explanation=(
                            f"Аномальная длина для этого источника: {doc.word_count} слов, "
                            f"что {direction} обычного (медиана {med_w:.0f}, z={z_w:+.1f})"
                        ),
                        weight=min(2.0, abs(z_w) / self.z_threshold),
                        metrics={"z_words": round(z_w, 2), "median_words": med_w},
                    )
                )

        # 3. NLP: близость к центроиду корпуса/источника
        if profile is not None:
            centroid = profile.per_source_centroid.get(doc.source_id) or profile.global_centroid
            if centroid is not None and centroid.size >= 3:
                sim = centroid.cosine_to_centroid(doc.text)
                metrics["centroid_similarity"] = round(sim, 4)
                if sim < self.centroid_threshold:
                    expected = ", ".join(centroid.top_terms(6))
                    hits.append(
                        RuleHit(
                            rule="off_topic",
                            explanation=(
                                "Содержание не похоже на типичные публикации источника "
                                f"(сходство с центроидом {sim:.2f}); ожидаемые темы: {expected}"
                            ),
                            weight=1.2,
                            metrics={"centroid_similarity": round(sim, 4)},
                        )
                    )
            if profile.median_chars and profile.stdev_chars:
                z_c = _z(doc.char_count, profile.median_chars, profile.stdev_chars)
                metrics["z_chars_corpus"] = round(z_c, 2)
                if abs(z_c) >= self.z_threshold:
                    hits.append(
                        RuleHit(
                            rule="corpus_length_outlier",
                            explanation=(
                                f"Длина текста нетипична для корпуса: {doc.char_count} симв. "
                                f"против медианы {profile.median_chars:.0f} (z={z_c:+.1f})"
                            ),
                            weight=1.0,
                            metrics={"z_chars_corpus": round(z_c, 2)},
                        )
                    )

        score = round(sum(h.weight for h in hits), 3)
        explanations = [h.explanation for h in hits]
        return AnomalyRecord(
            doc_id=doc.doc_id,
            is_anomaly=score >= self.on_threshold,
            score=score,
            rule_hits=[h.rule for h in hits],
            explanations=explanations,
            explanation=" ".join(explanations) if explanations else "Аномалий не обнаружено",
            metrics=metrics,
        )

    def detect_many(self, docs: list[CleanDoc]) -> tuple[list[AnomalyRecord], CorpusProfile]:
        profile = build_profile(docs)
        hist_by_source: dict[str, list[CleanDoc]] = defaultdict(list)
        records: list[AnomalyRecord] = []
        for doc in docs:
            rec = self.detect(doc, profile=profile, history=hist_by_source[doc.source_id])
            hist_by_source[doc.source_id].append(doc)
            records.append(rec)
        return records, profile
