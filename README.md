# LegitFeed — легитимный сбор данных и подготовка датасетов для ИИ

MVP-система мониторинга классифицированных открытых источников (RSS/Atom-фидов),
которая собирает данные **юридически корректно**, очищает их, находит и
объясняет аномалии и формирует валидированный датасет для обучения моделей.

> Учебный проект. Он не является юридической консультацией.

---

## 1. Что делает система

Конвейер (`pipeline`) из шести этапов, каждый этап логируется в БД и
воспроизводим из CLI:

| # | Этап | Модуль | Что происходит |
|---|------|--------|----------------|
| 0 | Правовой аудит источника | `legitfeed.legal.source_audit` | robots.txt (RFC 9309), ToS-эвристики, классификация источника, персональные данные |
| 1 | Легитимный сбор (фиды + API + страницы + датасеты) | `legitfeed.collect.*` | вежливый HTTP (rate limit, backoff, кэш), RSS/Atom; JSON API; извлечение текста из HTML; импорт готовых датасетов |
| 2 | Очистка (ETL) | `legitfeed.etl.cleaner` | дедуп по canonical-URL/хешу, удаление HTML-шума, нормализация текста, **маскирование ПДн** |
| 3 | Детекция и объяснение аномалий (NLP) | `legitfeed.anomaly.detector` | правила + статистика (z-score/EWMA) + TF-IDF косинус + объяснение на естественном языке |
| 4 | Хранение | `legitfeed.storage` | SQLite по умолчанию, PostgreSQL (`LEGITFEED_DATABASE_URL`) в проде |
| 5 | Разметка и датасет | `legitfeed.dataset.builder` | weak-supervision разметка, валидация Pandas, экспорт JSONL/CSV + отчёт качества |

Плюс:
- `legitfeed.api` — FastAPI: статус пайплайна, документы, аномалии, скачивание датасета;
- `legitfeed.scheduler` — APScheduler: непрерывный мониторинг по расписанию;
- `legitfeed.legal.auditor_report` — генерация Markdown-отчёта правового аудита.

### Источники данных (4 вида)

Все виды проходят один правовой аудит и выдают одинаковый `RawItem`, поэтому
дальше идут через общий конвейер:

| Вид | Конфиг | Модуль | Что собирает |
|-----|--------|--------|--------------|
| RSS/Atom фиды | `config/feeds.yaml` | `collect.feed_collector` | публичные фиды |
| JSON API | `config/apis.yaml` | `collect.api_collector` | публичные API (без авторизации): `items_path`, маппинг полей, пагинация-лимит |
| HTML-страницы | `config/pages.yaml` | `collect.html_collector` | основной текст статьи (readability-подобно), title/meta-дата/canonical |
| Готовые датасеты | `config/datasets.yaml` | `collect.dataset_importer` | локальный CSV/JSONL/JSON, файл по URL, HuggingFace (`datasets`) |

Правовой профиль каждого источника — в `config/sources.yaml` (`kind: feed|api|page|dataset`).
Для датасетов robots.txt не применяется (это не краулинг), но лицензия обязательна.

---

## 2. Быстрый старт

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt
pip install -e .

# Полный прогон демо-пайплайна (offline, без сети — встроенный фикстурный фид)
python -m legitfeed.cli demo

# Легитимный сбор реальных фидов из config/feeds.yaml
python -m legitfeed.cli collect
python -m legitfeed.cli clean
python -m legitfeed.cli detect
python -m legitfeed.cli dataset

# Всё одной командой
python -m legitfeed.cli run-all

# API
uvicorn legitfeed.api.app:app --reload
# http://127.0.0.1:8000/docs
```

Каталоги по умолчанию (см. `config/settings.py`):

```
data/raw/           исходные ответы фидов (JSONL, для воспроизводимости)
data/processed/     очищенные документы
data/datasets/      итоговый датасет + отчёты
data/cache/         кэш HTTP-ответов
data/audit/         отчёты правового аудита (Markdown + JSON)
legitfeed.db        SQLite (если LEGITFEED_DATABASE_URL не задан)
```

---

## 3. Юридическая модель проекта

Подход: **collect only what is explicitly allowed, store less than you collect.**

* `robots.txt` соблюдается по RFC 9309: группа `User-agent`, `Allow`/`Disallow`,
  `Crawl-delay`, wildcard-правила, приоритет самого длинного совпадения.
  При недоступности robots.txt по 5xx — источник пропускается (fail-closed).
* Проверка `User-Agent` — система представляется честно, со ссылкой на репозиторий.
* `rate_limit_rps` и `min_delay_seconds` на каждый источник, обязательный `Crawl-delay`.
* Разрешены только источники с явной лицензией/публичным фидом: белый список в
  `config/sources.yaml`; `tos_allow_feed: false` → источник не парсится.
* Персональные данные (152-ФЗ): телефоны/E-mail/адреса маскируются при очистке,
  сырые тексты не попадают в датасет. Ст. 272 УК РФ — запрет неправомерного
  доступа: система не обходит авторизацию, не парсит закрытые разделы,
  не подбирает параметры, не использует прокси для обхода блокировок.
* `config/legal_policy.yaml` фиксирует правила в машинночитаемом виде,
  тесты проверяют, что политика применяется (`tests/test_legal.py`).

Подробнее: [docs/LEGAL.md](docs/LEGAL.md), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## 4. Разметка датасета

Разметка — **weak supervision** по правилам, с приоритетом:

1. `anomaly.is_anomaly` → `label = "needs_review"` (а не «выбросить» — такие
   записи полезны для обучения детектора аномалий, они в отдельном сплите);
2. эвристики тематики по ключевым словам → `label = "topic:<domain>"`;
3. иначе `label = "general"`.

Поля датасета: `doc_id, source_id, url, title, text, language, published_at,
topic_label, label, anomaly_score, anomaly_reasons, split`.

Валидация (`legitfeed.dataset.validation`) проверяет схему, отсутствие ПДн,
отсутствие пустых текстов, дубликатов, баланс тем, пересечения `train/val/test`
и пишет `data/datasets/quality_report.json`. Датасет выгружается в
`dataset.jsonl`, `dataset.csv` и `dataset_card.md` (Data Card).

---

## 5. Тесты

```bash
pytest -q
pytest --cov=legitfeed --cov-report=term-missing
```

Покрытие: robots.txt-парсер (включая RFC 9309-кейсы и wildcard), rate limiter,
детектор аномалий и объяснения, очистка и маскирование ПДн, разметка и
валидация датасета, API, политика (нет обхода robots/ToS).

---

## 6. Структура

```
config/                 feeds.yaml, apis.yaml, pages.yaml, datasets.yaml,
                        sources.yaml, legal_policy.yaml, settings.py
legitfeed/
  legal/                robots, tos-эвристики, аудит источника, отчёт
  collect/              HTTP-клиент с rate limit/backoff/кэшем, сборщик фидов
  etl/                  очистка, нормализация, маскирование ПДн, дедуп
  anomaly/              правила + статистика + TF-IDF, объяснения
  storage/              SQLAlchemy-модели и репозитории
  dataset/              разметка, сборка, валидация, карточка датасета
  pipeline/             оркестрация этапов
  api/                  FastAPI
  cli.py, scheduler.py
tests/
docs/
Dockerfile, docker-compose.yml
```
