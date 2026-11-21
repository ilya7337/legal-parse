"""Тесты очистки: нормализация, HTML→текст, язык, маскирование ПДн, дедуп."""

from __future__ import annotations

from legitfeed.etl.cleaner import (
    Cleaner,
    detect_language,
    html_to_text,
    normalize_text,
    simhash,
)
from legitfeed.etl.pii import contains_pii, mask_pii
from legitfeed.models import RawItem


def _item(source_id="src", title="Заголовок", link="https://example.org/p/1",
          html="", summary_html=None) -> RawItem:
    return RawItem(
        source_id=source_id,
        title=title,
        link=link,
        summary_html=summary_html if summary_html is not None else html,
    )


LONG_TEXT = (
    "В статье мы подробно разбираем построение ETL-пайплайна на Python и Airflow. "
    "Показываем настройку DAG, очистку и валидацию данных на этапе загрузки, а также "
    "метрики потребления памяти и времени выполнения каждой задачи в продакшене."
)


# ---------- ПДн ----------

def test_mask_email():
    res = mask_pii("Пишите на ivan.petrov@example.com для связи")
    assert "ivan.petrov@example.com" not in res.text
    assert "[EMAIL]" in res.text
    assert "email" in res.found


def test_mask_russian_phone():
    res = mask_pii("Телефон +7 (999) 123-45-67 доступен в будни")
    assert "+7" not in res.text
    assert "[PHONE]" in res.text
    assert "phone" in res.found


def test_mask_url_token():
    res = mask_pii("Ссылка http://api.example.com/data?token=secret123 далее")
    assert "secret123" not in res.text
    assert "REDACTED" in res.text


def test_contains_pii():
    assert contains_pii("контакт test@example.com") is True
    assert contains_pii("обычный текст без данных") is False


# ---------- нормализация / HTML ----------

def test_normalize_collapses_whitespace():
    assert normalize_text("  привет\t\tмир\n\nснова   тут ") == "привет мир снова тут"


def test_normalize_strips_boilerplate():
    out = normalize_text("Читать далее\nПолезный текст статьи")
    assert "Читать далее" not in out


def test_html_to_text_removes_scripts():
    html = "<p>Полезный текст</p><script>alert('xss')</script><p>Ещё текст</p>"
    out = html_to_text(html)
    assert "Полезный текст" in out
    assert "alert" not in out


# ---------- язык ----------

def test_detect_language_ru_en():
    assert detect_language("Привет как дела сегодня") == "ru"
    assert detect_language("Hello how are you today") == "en"


def test_detect_language_mixed_and_unknown():
    assert detect_language("Привет world") == "mixed"
    assert detect_language("") == "unknown"


# ---------- SimHash ----------

def test_simhash_similar_texts_close():
    a = simhash(LONG_TEXT)
    b = simhash(LONG_TEXT.replace("воздух", "памяти", 1))
    from legitfeed.etl.cleaner import _hamming

    assert _hamming(a, b) <= 8


# ---------- Cleaner ----------

def test_cleaner_drops_short_text(tmp_settings, policy):
    cleaner = Cleaner(tmp_settings, policy)
    item = _item(summary_html="слишком коротко")
    assert cleaner.clean(item) is None


def test_cleaner_masks_pii_and_flags(tmp_settings, policy):
    cleaner = Cleaner(tmp_settings, policy)
    item = _item(summary_html=f"{LONG_TEXT} Контакты: test@example.com, +7 (999) 123-45-67")
    doc = cleaner.clean(item)
    assert doc is not None
    assert "test@example.com" not in doc.text
    assert "pii_masked" in doc.cleaning_flags
    assert {"email", "phone"}.issubset(set(doc.pii_masked))


def test_cleaner_dedupes_identical(tmp_settings, policy):
    cleaner = Cleaner(tmp_settings, policy)
    first = cleaner.clean(_item(link="https://example.org/a", summary_html=LONG_TEXT))
    dup = cleaner.clean(_item(link="https://example.org/a", summary_html=LONG_TEXT))
    assert first is not None
    assert dup is None
    assert cleaner._last_drop == "duplicate"


def test_cleaner_detects_near_duplicate(tmp_settings, policy):
    cleaner = Cleaner(tmp_settings, policy)
    first = cleaner.clean(_item(link="https://example.org/a", summary_html=LONG_TEXT))
    # почти идентичный текст: добавлены два слова в конец -> сходство > 0.92
    near = cleaner.clean(
        _item(link="https://example.org/b", summary_html=LONG_TEXT + " и ещё немного")
    )
    assert first is not None
    assert near is None
    assert cleaner._last_drop == "near_duplicate"


def test_clean_many_counts(tmp_settings, policy):
    cleaner = Cleaner(tmp_settings, policy)
    items = [
        _item(link="https://example.org/1", summary_html=LONG_TEXT),
        _item(link="https://example.org/2", summary_html="короткий"),
        _item(link="https://example.org/1", summary_html=LONG_TEXT),  # дубль
    ]
    docs, stats = cleaner.clean_many(items)
    assert stats.total == 3
    assert stats.cleaned == 1
    assert stats.dropped_short == 1
    assert stats.duplicates == 1
