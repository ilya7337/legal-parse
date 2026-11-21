"""Очистка: HTML -> текст, нормализация, детекция языка, дедупликация."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from bs4 import BeautifulSoup

from config.settings import Settings, get_settings
from legitfeed.etl.pii import mask_pii
from legitfeed.legal.policy import LegalPolicy, load_policy
from legitfeed.legal.robots import canonical_url
from legitfeed.models import CleanDoc, RawItem, collapse_ws, text_hash

# Теги, содержимое которых не является полезным текстом
_DROP_TAGS = ["script", "style", "noscript", "iframe", "svg", "form", "template"]
_BOILERPLATE = [
    re.compile(
        r"^\s*(читать далее|read more|подписывайтесь|subscribe|источник:|source:).*$", re.IGNORECASE
    ),
    re.compile(r"^\s*(поделиться|share this|комментари[ий]\w*)\s*$", re.IGNORECASE),
]
_CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)
_LATIN = re.compile(r"[a-z]", re.IGNORECASE)


@dataclass
class CleanStats:
    total: int = 0
    cleaned: int = 0
    dropped_short: int = 0
    duplicates: int = 0
    near_duplicates: int = 0
    pii_masked: int = 0

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "cleaned": self.cleaned,
            "dropped_short": self.dropped_short,
            "duplicates": self.duplicates,
            "near_duplicates": self.near_duplicates,
            "pii_masked": self.pii_masked,
        }


def html_to_text(html: str) -> str:
    """HTML -> читаемый текст без скриптов, навигации и лишних пробелов."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(_DROP_TAGS):
        tag.decompose()
    # блочные элементы -> перевод строки, чтобы сохранить абзацы
    for br in soup.find_all(["br", "p", "div", "li", "h1", "h2", "h3", "h4", "tr"]):
        br.insert_after("\n")
    return normalize_text(soup.get_text(separator=" "))


def normalize_text(text: str) -> str:
    """Unicode-нормализация, снятие мусора, единые пробелы, отсев boilerplate."""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u200b", "").replace("\xa0", " ")
    text = text.replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = collapse_ws(raw_line)
        if not line:
            continue
        if any(p.match(line) for p in _BOILERPLATE):
            continue
        lines.append(line)
    out = " ".join(lines)
    # убираем лишние пробелы перед знаками и повторяющуюся пунктуацию
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)
    out = re.sub(r"([!?.,])\1{2,}", r"\1\1", out)
    return collapse_ws(out)


def detect_language(text: str) -> str:
    """Простая и достаточная для MVP эвристика ru/en/mixed."""
    if not text:
        return "unknown"
    sample = text[:2000]
    cyr = len(_CYRILLIC.findall(sample))
    lat = len(_LATIN.findall(sample))
    if cyr == 0 and lat == 0:
        return "unknown"
    if cyr > lat * 1.5:
        return "ru"
    if lat > cyr * 1.5:
        return "en"
    return "mixed"


def _simhash(text: str, bits: int = 64) -> int:
    """SimHash для near-duplicate detection на шинглах по 3 слова."""
    words = text.lower().split()
    if not words:
        return 0
    shingles = [" ".join(words[i : i + 3]) for i in range(max(1, len(words) - 2))]
    vector = [0] * bits
    for shingle in shingles:
        h = int(text_hash(shingle)[:16], 16)
        for i in range(bits):
            vector[i] += 1 if h & (1 << i) else -1
    result = 0
    for i in range(bits):
        if vector[i] > 0:
            result |= 1 << i
    return result


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def simhash(text: str) -> int:
    """Публичная обёртка (используется детектором аномалий и тестами)."""
    return _simhash(text)


class Cleaner:
    """Очистка RawItem -> CleanDoc с защитой от ПДн и дублей."""

    def __init__(self, settings: Settings | None = None, policy: LegalPolicy | None = None):
        self.settings = settings or get_settings()
        self.policy = policy or load_policy()
        self.seen_hashes: set[str] = set()
        self.seen_urls: set[str] = set()
        self._simhashes: list[int] = []
        self._last_drop: str | None = None

    def clean(self, item: RawItem) -> CleanDoc | None:
        """Очистить одну запись. None — если она отброшена (короткая/дубль)."""
        item.ensure_hash()
        html = item.summary_html or ""
        text = html_to_text(html) if ("<" in html and ">" in html) else normalize_text(html)

        flags: list[str] = []
        if not text:
            flags.append("empty_text")

        masked = self.policy.personal_data.mask_pii and self.settings.mask_pii
        pii_found: list[str] = []
        if masked:
            res = mask_pii(text, self.policy.personal_data.mask_types)
            text, pii_found = res.text, res.found
            if pii_found:
                flags.append("pii_masked")

        title = normalize_text(item.title)
        if masked:
            title = mask_pii(title, self.policy.personal_data.mask_types).text

        if len(text) < self.settings.min_text_chars:
            self._last_drop = "short"
            return None

        canonical = canonical_url(item.link)
        if item.content_hash in self.seen_hashes or canonical in self.seen_urls:
            self._last_drop = "duplicate"
            return None

        sh = _simhash(text)
        for prev in self._simhashes:
            similarity = 1 - _hamming(sh, prev) / 64
            if similarity >= self.settings.duplicate_similarity_threshold:
                self._last_drop = "near_duplicate"
                return None
        self._simhashes.append(sh)

        self.seen_hashes.add(item.content_hash)
        self.seen_urls.add(canonical)
        self._last_drop = None

        # excerpt для датасета ограничен политикой (короткая цитата, fair use)
        max_excerpt = self.policy.attribution.max_excerpt_chars

        return CleanDoc(
            doc_id=CleanDoc.make_id(item.source_id, canonical, title),
            source_id=item.source_id,
            url=item.link,
            canonical_url=canonical,
            title=title,
            text=text,
            summary=text[:max_excerpt],
            language=detect_language(text),
            published_at=item.published_at,
            snapshot_at=item.snapshot_at,
            license=item.license,
            license_url=item.license_url,
            tags=item.tags,
            word_count=len(text.split()),
            char_count=len(text),
            pii_masked=pii_found,
            cleaning_flags=flags,
            content_hash=item.content_hash,
        )

    def clean_many(self, items: list[RawItem]) -> tuple[list[CleanDoc], CleanStats]:
        stats = CleanStats(total=len(items))
        docs: list[CleanDoc] = []
        for item in items:
            doc = self.clean(item)
            if doc is None:
                if self._last_drop == "near_duplicate":
                    stats.near_duplicates += 1
                elif self._last_drop == "duplicate":
                    stats.duplicates += 1
                else:
                    stats.dropped_short += 1
                continue
            if doc.pii_masked:
                stats.pii_masked += 1
            docs.append(doc)
        stats.cleaned = len(docs)
        return docs, stats
