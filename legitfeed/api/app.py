"""FastAPI-приложение: тонкий слой над репозиторием и пайплайном.

Эндпоинты:
    GET  /health                       — живость
    GET  /api/sources                  — реестр источников из БД
    GET  /api/stats                    — сводка (документы/аномалии/датасет)
    GET  /api/runs                     — журнал прогонов пайплайна
    GET  /api/documents                — очищенные документы (пагинация)
    GET  /api/anomalies                — аномалии с объяснениями
    GET  /api/dataset/preview          — первые N строк датасета
    GET  /api/dataset/quality          — отчёт качества датасета
    GET  /api/dataset/download/{name}  — скачать артефакт датасета
    POST /api/pipeline/run             — запустить пайплайн (offline по умолчанию)
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from config.settings import get_settings
from legitfeed import __version__
from legitfeed.storage.repository import Repository

app = FastAPI(
    title="LegitFeed API",
    version=__version__,
    description=(
        "MVP: легитимный сбор открытых фидов, очистка, объяснимые аномалии "
        "и подготовка датасета для ML. Все источники проходят правовой аудит."
    ),
)
router = APIRouter(prefix="/api")
_repo = Repository()


def get_repo() -> Repository:
    return _repo


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__, "database": get_settings().database_url}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    s = _repo.summary()
    return f"""
    <html><head><meta charset="utf-8"><title>LegitFeed</title>
    <style>body{{font-family:system-ui;margin:2rem;max-width:900px}}
    code{{background:#f3f4f6;padding:.1rem .3rem;border-radius:4px}}</style></head>
    <body>
      <h1>LegitFeed v{__version__}</h1>
      <p>Легитимный сбор данных из открытых фидов и подготовка датасетов для ИИ.</p>
      <h2>Состояние</h2>
      <ul>
        <li>Документы: <b>{s['documents']}</b></li>
        <li>Аномалии: <b>{s['anomalies']}</b></li>
        <li>Строки датасета: <b>{s['dataset_rows']}</b></li>
      </ul>
      <h2>Эндпоинты</h2>
      <ul>
        <li><code>GET /docs</code> — OpenAPI UI</li>
        <li><code>GET /api/sources</code></li>
        <li><code>GET /api/documents?limit=10</code></li>
        <li><code>GET /api/anomalies?only=true</code></li>
        <li><code>GET /api/dataset/preview</code></li>
        <li><code>GET /api/dataset/quality</code></li>
        <li><code>POST /api/pipeline/run</code></li>
      </ul>
    </body></html>
    """


@router.get("/sources")
def list_sources() -> list[dict]:
    return get_repo().list_sources()


@router.get("/stats")
def stats() -> dict:
    return get_repo().summary()


@router.get("/runs")
def runs(limit: int = Query(20, ge=1, le=200)) -> list[dict]:
    return get_repo().last_runs(limit)


@router.get("/documents")
def documents(
    source_id: str | None = None,
    limit: int = Query(20, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    docs = get_repo().load_documents(source_id)
    page = docs[offset : offset + limit]
    return {
        "total": len(docs),
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "doc_id": d.doc_id,
                "source_id": d.source_id,
                "title": d.title,
                "url": d.url,
                "language": d.language,
                "published_at": d.published_at,
                "word_count": d.word_count,
                "pii_masked": d.pii_masked,
                "excerpt": d.summary[:300],
            }
            for d in page
        ],
    }


@router.get("/anomalies")
def anomalies(only: bool = False, limit: int = Query(50, ge=1, le=500)) -> list[dict]:
    return get_repo().load_anomalies(only_anomalies=only)[:limit]


@router.get("/dataset/preview")
def dataset_preview(limit: int = Query(10, ge=1, le=200)) -> dict:
    rows = get_repo().load_dataset()
    return {"total": len(rows), "items": rows[:limit]}


@router.get("/dataset/quality")
def dataset_quality() -> JSONResponse:
    path = get_settings().path("datasets") / "quality_report.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="quality_report.json не найден — запустите пайплайн")
    return JSONResponse(json.loads(path.read_text(encoding="utf-8")))


@router.get("/dataset/download/{name}")
def dataset_download(name: str) -> FileResponse:
    allowed = {"dataset.jsonl", "dataset.csv", "train.jsonl", "val.jsonl", "test.jsonl",
               "dataset_meta.json", "dataset_card.md", "quality_report.json"}
    if name not in allowed:
        raise HTTPException(status_code=404, detail="неизвестный артефакт")
    path: Path = get_settings().path("datasets") / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="файл не найден")
    return FileResponse(path, filename=name)


@router.post("/pipeline/run")
def pipeline_run(
    offline: bool = True,
    strict_audit: bool = True,
    stages: str | None = None,
) -> dict:
    import os

    from config.settings import reset_settings_cache

    os.environ["LEGITFEED_OFFLINE"] = "true" if offline else "false"
    reset_settings_cache()

    from legitfeed.pipeline.runner import Pipeline

    stage_tuple = tuple(s.strip() for s in stages.split(",")) if stages else (
        "audit", "collect", "clean", "detect", "dataset"
    )
    result = Pipeline().run(stages=stage_tuple, strict_audit=strict_audit)
    return result.summary()


app.include_router(router)
