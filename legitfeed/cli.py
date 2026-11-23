"""CLI: управление пайплайном, аудитом и датасетом.

Пример:
    python -m legitfeed.cli demo
    python -m legitfeed.cli audit
    python -m legitfeed.cli run-all --offline
    python -m legitfeed.cli serve
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from config.settings import get_settings, reset_settings_cache


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _configure(args):
    """Применяем CLI-флаги к переменным окружения до создания Settings."""
    if getattr(args, "offline", False):
        os.environ["LEGITFEED_OFFLINE"] = "true"
    if getattr(args, "db", None):
        os.environ["LEGITFEED_DATABASE_URL"] = args.db
    reset_settings_cache()
    return get_settings()


def cmd_init(args) -> int:
    settings = _configure(args)
    from legitfeed.legal.registry import load_registry
    from legitfeed.storage.repository import Repository

    registry = load_registry()
    repo = Repository()
    for source in registry.sources.values():
        repo.upsert_source(source)
    _print(
        {
            "database": settings.database_url,
            "data_dir": str(settings.data_dir),
            "sources": len(registry),
            "summary": repo.summary(),
        }
    )
    return 0


def cmd_audit(args) -> int:
    settings = _configure(args)
    from legitfeed.pipeline.runner import Pipeline

    pipeline = Pipeline(settings=settings)
    audits = pipeline.stage_audit(strict=not args.not_strict)
    for a in audits:
        status = "ALLOWED" if a.allowed else "DENIED"
        print(f"[{status}] {a.source_id}: " + ("; ".join(a.reasons) or "ok"))
    print(f"\nОтчёт: {pipeline._artifacts.get('legal_audit_md')}")
    return 0


def cmd_collect(args) -> int:
    settings = _configure(args)
    from legitfeed.pipeline.runner import Pipeline

    pipeline = Pipeline(settings=settings)
    items, _audits, stats = pipeline.stage_collect_all()
    _print(stats)
    print(f"\nСохранено сырых записей (фиды+API+страницы+датасеты): {len(items)} -> {pipeline._artifacts.get('raw_items')}")
    return 0


def cmd_clean(args) -> int:
    settings = _configure(args)
    from legitfeed.models import RawItem
    from legitfeed.pipeline.runner import Pipeline

    pipeline = Pipeline(settings=settings)
    raw = pipeline._load_jsonl(settings.path("raw") / "raw_items.jsonl", RawItem)
    if not raw:
        print("Нет сырых данных. Сначала выполните: collect", file=sys.stderr)
        return 1
    _docs, stats = pipeline.stage_clean(raw)
    _print(stats)
    return 0


def cmd_detect(args) -> int:
    settings = _configure(args)
    from legitfeed.pipeline.runner import Pipeline

    pipeline = Pipeline(settings=settings)
    docs = pipeline.repo.load_documents()
    if not docs:
        print("Нет очищенных документов. Сначала: clean", file=sys.stderr)
        return 1
    records, stats = pipeline.stage_detect(docs)
    _print(stats)
    print("\nТоп-5 аномалий с объяснением:")
    for rec in sorted(records, key=lambda r: -r.score)[:5]:
        print(f"  {rec.doc_id} score={rec.score}: {rec.explanation}")
    return 0


def cmd_crawl(args) -> int:
    """Многостраничный краулинг на Scrapy (опционально, robots.txt соблюдается)."""
    from legitfeed.crawl.spider import ScrapyUnavailableError, run_crawl

    try:
        items = run_crawl(
            args.start_urls,
            article_css=args.article_css,
            title_css=args.title_css,
            body_css=args.body_css,
            next_css=args.next_css,
            max_pages=args.max_pages,
            allowed_domains=args.allowed_domains or None,
        )
    except ScrapyUnavailableError as exc:
        print(exc, file=sys.stderr)
        return 1

    settings = _configure(args)
    out = settings.path("raw") / "crawl_items.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    _print({"collected": len(items), "saved_to": str(out), "sample": items[:3]})
    print(
        "Заметка: robots.txt соблюдался Scrapy автоматически. Для включения в датасет "
        "добавьте источник в config/sources.yaml и импортируйте через конвейер."
    )
    return 0


def cmd_dataset(args) -> int:
    settings = _configure(args)
    from legitfeed.models import AnomalyRecord
    from legitfeed.pipeline.runner import Pipeline

    pipeline = Pipeline(settings=settings)
    docs = pipeline.repo.load_documents()
    if not docs:
        print("Нет документов. Сначала: clean", file=sys.stderr)
        return 1
    anomalies = [
        AnomalyRecord(**{k: v for k, v in a.items() if k in AnomalyRecord.model_fields})
        for a in pipeline.repo.load_anomalies()
    ]
    bundle = pipeline.stage_dataset(docs, anomalies)
    _print(bundle.summary())
    return 0 if bundle.report.passed else 2


def cmd_run_all(args) -> int:
    settings = _configure(args)
    from legitfeed.pipeline.runner import Pipeline

    result = Pipeline(settings=settings).run(strict_audit=not args.not_strict)
    _print(result.summary())
    return 0 if (result.dataset and result.dataset.report.passed) else 2


def _seed_robots_cache(settings) -> None:
    """Демо: детерминированный robots.txt из фикстур (воспроизводимый аудит)."""
    from pathlib import Path

    from legitfeed.legal.auditor import RobotsFetcher

    fixtures = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "robots"
    cache_dir = settings.path("cache") / "robots"
    cache_dir.mkdir(parents=True, exist_ok=True)
    fetcher = RobotsFetcher(cache_dir=cache_dir)
    mapping = {
        "https://habr.com/robots.txt": "habr_robots.txt",
        "https://pythoninsider.blogspot.com/robots.txt": "arxiv_robots.txt",
        "https://arxiv.org/robots.txt": "arxiv_robots.txt",
        # API / HTML-источники (разрешено всё — публичные ресурсы)
        "https://api.github.com/robots.txt": "allow_all_robots.txt",
        "http://hn.algolia.com/robots.txt": "allow_all_robots.txt",
        "https://gitlab.com/robots.txt": "allow_all_robots.txt",
        "https://peps.python.org/robots.txt": "allow_all_robots.txt",
        "https://en.wikipedia.org/robots.txt": "allow_all_robots.txt",
    }
    for url, filename in mapping.items():
        text = (fixtures / filename).read_text(encoding="utf-8")
        fetcher._cache_path(url).write_text(
            json.dumps({"text": text, "status": 200}, ensure_ascii=False), encoding="utf-8"
        )


def cmd_demo(args) -> int:
    """Полная демонстрация на встроенных фикстурах, без сети."""
    os.environ["LEGITFEED_OFFLINE"] = "true"
    reset_settings_cache()
    settings = get_settings()
    _seed_robots_cache(settings)

    from legitfeed.pipeline.runner import Pipeline

    # strict_audit=False: демонстрация собирает и неодобренный источник, чтобы показать,
    # как работает отказ. В проде strict_audit=True (по умолчанию).
    result = Pipeline(settings=settings).run(strict_audit=False)

    print("=" * 78)
    print("  LegitFeed DEMO — полный пайплайн на встроенных фикстурах")
    print("=" * 78)
    for stage_name, stage_stats in result.stages.items():
        if stage_name.startswith("run_"):
            continue
        print(f"\n[{stage_name.upper()}]")
        print(json.dumps(stage_stats, ensure_ascii=False, indent=2, default=str))

    print("\n" + "=" * 78)
    print("  ИТОГ")
    print("=" * 78)
    _print(result.summary())

    if result.dataset:
        print("\nПримеры аномалий с объяснением:")
        for row in sorted(result.dataset.rows, key=lambda r: -r.anomaly_score)[:3]:
            print(f"  - {row.title[:60]!r}: score={row.anomaly_score}")
            print(f"    {row.anomaly_reasons[:400]}")

    denied = [a for a in result.audits if not a.allowed]
    if denied:
        print("\nИсточники, отклонённые правовым аудитом (не собирались):")
        for a in denied:
            print(f"  - {a.source_id}: {'; '.join(a.reasons)}")

    return 0 if (result.dataset and result.dataset.report.passed) else 2


def cmd_status(args) -> int:
    _configure(args)
    from legitfeed.storage.repository import Repository

    repo = Repository()
    _print({"summary": repo.summary(), "runs": repo.last_runs(10)})
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    uvicorn.run("legitfeed.api.app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_schedule(args) -> int:
    from legitfeed.scheduler import run_scheduler

    return run_scheduler(interval_minutes=args.interval, once=args.once, offline=args.offline)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="legitfeed",
        description="Легитимный сбор данных из открытых фидов и подготовка датасетов для ИИ",
    )
    parser.add_argument("--db", help="URL БД (по умолчанию SQLite в корне проекта)")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--offline", action="store_true", help="без выхода в сеть (фикстуры)")
        return p

    common(sub.add_parser("init", help="создать схему БД и зарегистрировать источники"))
    p_audit = common(sub.add_parser("audit", help="правовой аудит источников"))
    p_audit.add_argument("--not-strict", action="store_true", help="не требовать аудит источника")
    common(sub.add_parser("collect", help="легитимный сбор фидов"))
    common(sub.add_parser("clean", help="очистка сырых данных"))
    common(sub.add_parser("detect", help="детекция и объяснение аномалий"))
    common(sub.add_parser("dataset", help="собрать и провалидировать датасет"))
    p_crawl = sub.add_parser("crawl", help="многостраничный краулинг на Scrapy")
    p_crawl.add_argument("start_urls", nargs="+")
    p_crawl.add_argument("--article-css", default="article a::attr(href)")
    p_crawl.add_argument("--title-css", default="h1::text")
    p_crawl.add_argument("--body-css", default="article p::text")
    p_crawl.add_argument("--next-css", default="")
    p_crawl.add_argument("--max-pages", type=int, default=5)
    p_crawl.add_argument("--allowed-domains", nargs="*")
    p_run = common(sub.add_parser("run-all", help="весь пайплайн"))
    p_run.add_argument("--not-strict", action="store_true")

    sub.add_parser("demo", help="демонстрация на встроенных фикстурах (offline)")
    common(sub.add_parser("status", help="статус БД и последние прогоны"))

    p_serve = common(sub.add_parser("serve", help="запустить API (FastAPI)"))
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--reload", action="store_true")

    p_sched = common(sub.add_parser("schedule", help="непрерывный мониторинг по расписанию"))
    p_sched.add_argument("--interval", type=int, default=60, help="минуты между прогонами")
    p_sched.add_argument("--once", action="store_true", help="один прогон и выход")

    return parser


COMMANDS = {
    "init": cmd_init,
    "audit": cmd_audit,
    "collect": cmd_collect,
    "clean": cmd_clean,
    "detect": cmd_detect,
    "dataset": cmd_dataset,
    "crawl": cmd_crawl,
    "run-all": cmd_run_all,
    "demo": cmd_demo,
    "status": cmd_status,
    "serve": cmd_serve,
    "schedule": cmd_schedule,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except KeyboardInterrupt:
        print("\nПрервано пользователем", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
