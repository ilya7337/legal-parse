"""Непрерывный мониторинг фидов по расписанию (APScheduler).

Whytrend-подобное поведение: периодический прогон пайплайна, устойчивый к
падению сети, с журналированием каждого запуска в БД.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime

from apscheduler.schedulers.blocking import BlockingScheduler

from config.settings import get_settings, reset_settings_cache


def run_once(offline: bool | None = None, strict_audit: bool = True) -> dict:
    """Один прогон пайплайна целиком; возвращает сводку."""
    if offline is not None:
        os.environ["LEGITFEED_OFFLINE"] = "true" if offline else "false"
        reset_settings_cache()

    from legitfeed.pipeline.runner import Pipeline

    result = Pipeline().run(strict_audit=strict_audit)
    summary = result.summary()
    line = {
        "raw": summary["raw_items"],
        "docs": summary["clean_docs"],
        "anomalies": summary["anomalies"],
        "dataset": summary["dataset_rows"],
    }
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    print(f"[{stamp}] прогон завершён: {json.dumps(line, ensure_ascii=False)}")
    return summary


def run_scheduler(
    interval_minutes: int = 60,
    once: bool = False,
    offline: bool = False,
    strict_audit: bool = True,
) -> int:
    """Запустить планировщик. ``once=True`` — один прогон и выход."""
    if once:
        run_once(offline=offline, strict_audit=strict_audit)
        return 0

    settings = get_settings()
    scheduler = BlockingScheduler(timezone="UTC")

    @scheduler.scheduled_job("interval", minutes=interval_minutes, max_instances=1, coalesce=True)
    def _job() -> None:
        try:
            run_once(offline=offline, strict_audit=strict_audit)
        except Exception as exc:  # noqa: BLE001 - не роняем планировщик
            print(f"[scheduler] ошибка прогона: {exc}", file=sys.stderr)

    print(
        f"Планировщик запущен: интервал {interval_minutes} мин, "
        f"offline={offline}, БД={settings.database_url}. Ctrl+C для остановки."
    )
    # немедленный первый прогон, затем по расписанию
    try:
        _job()
    except Exception as exc:  # noqa: BLE001
        print(f"[scheduler] первый прогон не удался: {exc}", file=sys.stderr)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("\nПланировщик остановлен")
    return 0
