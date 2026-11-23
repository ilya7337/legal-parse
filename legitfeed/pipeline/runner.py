"""Оркестратор пайплайна: связывает все этапы и сохраняет артефакты.

Каждый запуск пишет JSONL-артефакты в ``data/`` и строку в таблицу
``pipeline_runs`` — это даёт воспроизводимость и журнал для демонстрации.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from config.settings import Settings, get_settings
from legitfeed.anomaly.detector import AnomalyDetector
from legitfeed.collect.api_collector import APICollector
from legitfeed.collect.dataset_importer import DatasetImporter
from legitfeed.collect.feed_collector import FeedCollector, dedupe_raw
from legitfeed.collect.html_collector import HTMLCollector
from legitfeed.collect.http_client import PoliteHttpClient
from legitfeed.collect.specs import load_api_specs, load_dataset_specs, load_page_specs
from legitfeed.dataset.builder import DatasetBundle, build_dataset
from legitfeed.etl.cleaner import Cleaner
from legitfeed.legal.auditor import AuditResult, RobotsFetcher, audit_source
from legitfeed.legal.auditor_report import build_report
from legitfeed.legal.policy import LegalPolicy, load_policy
from legitfeed.legal.registry import SourceRegistry, load_registry
from legitfeed.models import AnomalyRecord, CleanDoc, RawItem
from legitfeed.storage.repository import Repository


@dataclass
class PipelineResult:
    run_id: str
    audits: list[AuditResult] = field(default_factory=list)
    raw_items: list[RawItem] = field(default_factory=list)
    docs: list[CleanDoc] = field(default_factory=list)
    anomalies: list[AnomalyRecord] = field(default_factory=list)
    dataset: DatasetBundle | None = None
    stages: dict[str, dict] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)

    def summary(self) -> dict:
        return {
            "run_id": self.run_id,
            "audited_sources": len(self.audits),
            "allowed_sources": sum(1 for a in self.audits if a.allowed),
            "denied_sources": sum(1 for a in self.audits if not a.allowed),
            "raw_items": len(self.raw_items),
            "clean_docs": len(self.docs),
            "anomalies": sum(1 for a in self.anomalies if a.is_anomaly),
            "dataset_rows": len(self.dataset.rows) if self.dataset else 0,
            "validation_passed": self.dataset.report.passed if self.dataset else None,
            "stages": self.stages,
            "artifacts": self.artifacts,
        }


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def _collection_stats(result) -> dict:
    """Статистика одного CollectionResult (api/page/dataset)."""
    return {
        "sources": [s.as_dict() for s in result.stats],
        "total_items": len(result.items),
    }


class Pipeline:
    """Полный конвейер «от фида до датасета»."""

    def __init__(
        self,
        settings: Settings | None = None,
        policy: LegalPolicy | None = None,
        registry: SourceRegistry | None = None,
        repository: Repository | None = None,
    ):
        self.settings = settings or get_settings()
        self.policy = policy or load_policy()
        self.registry = registry or load_registry()
        self.repo = repository or Repository()
        robots_fetcher = RobotsFetcher()
        client = PoliteHttpClient(self.settings, self.policy)
        self.collector = FeedCollector(self.settings, self.policy, client=client, robots_fetcher=robots_fetcher)
        self.api_collector = APICollector(self.settings, self.policy, client=client, robots_fetcher=robots_fetcher)
        self.html_collector = HTMLCollector(self.settings, self.policy, client=client, robots_fetcher=robots_fetcher)
        self.dataset_importer = DatasetImporter(self.settings, self.policy, client=client, robots_fetcher=robots_fetcher)
        self.cleaner = Cleaner(self.settings, self.policy)
        self.detector = AnomalyDetector(
            z_threshold=self.settings.zscore_threshold,
            min_history=self.settings.anomaly_min_history,
            min_chars=self.settings.min_text_chars,
        )
        self._artifacts: dict[str, Path] = {}

    # ---------- этапы ----------

    def stage_audit(self, strict: bool = True) -> list[AuditResult]:
        fetcher = self.collector.robots_fetcher
        results = []
        for source in self.registry.sources.values():
            # для готовых датасетов robots.txt неприменим (это не краулинг)
            check_robots = source.kind != "dataset"
            results.append(
                audit_source(
                    source, policy=self.policy, robots_fetcher=fetcher,
                    strict=strict, check_robots=check_robots,
                )
            )
        md, js = build_report(results, self.policy).save()
        self._artifacts.update({"legal_audit_md": md, "legal_audit_json": js})
        return results

    def stage_collect(self, pairs=None) -> tuple[list[RawItem], list[AuditResult], dict]:
        pairs = pairs if pairs is not None else self.registry.enabled()
        for source, _ in pairs:
            self.repo.upsert_source(source)
        result = self.collector.collect(pairs)
        deduped = dedupe_raw(result.items)
        path = self.settings.path("raw") / "raw_items.jsonl"
        _write_jsonl(path, [i.model_dump() for i in deduped])
        self._artifacts["raw_items"] = path
        return deduped, result.audits, result.stats_dict()

    def _enabled_specs(self, specs):
        """Отфильтровать включённые спецификации и подставить Source по source_id."""
        pairs = []
        for spec in specs:
            if not getattr(spec, "enabled", True):
                continue
            try:
                source = self.registry.get(spec.source_id)
            except KeyError:
                continue
            pairs.append((spec, source))
        return pairs

    def stage_collect_all(self) -> tuple[list[RawItem], list[AuditResult], dict]:
        """Собрать все виды ресурсов: фиды + API + HTML-страницы + датасеты."""
        all_raw: list[RawItem] = []
        all_audits: list[AuditResult] = []
        stats: dict = {}

        pairs = self.registry.enabled()
        for source, _ in pairs:
            self.repo.upsert_source(source)
        feed_result = self.collector.collect(pairs)
        all_raw.extend(feed_result.items)
        all_audits.extend(feed_result.audits)
        stats["feeds"] = feed_result.stats_dict()

        api_pairs = self._enabled_specs(load_api_specs())
        for _, source in api_pairs:
            self.repo.upsert_source(source)
        api_result = self.api_collector.collect(api_pairs)
        all_raw.extend(api_result.items)
        all_audits.extend(api_result.audits)
        stats["apis"] = _collection_stats(api_result)

        page_pairs = self._enabled_specs(load_page_specs())
        for _, source in page_pairs:
            self.repo.upsert_source(source)
        page_result = self.html_collector.collect(page_pairs)
        all_raw.extend(page_result.items)
        all_audits.extend(page_result.audits)
        stats["pages"] = _collection_stats(page_result)

        ds_pairs = self._enabled_specs(load_dataset_specs())
        for _, source in ds_pairs:
            self.repo.upsert_source(source)
        ds_result = self.dataset_importer.collect(ds_pairs)
        all_raw.extend(ds_result.items)
        all_audits.extend(ds_result.audits)
        stats["datasets"] = _collection_stats(ds_result)

        deduped = dedupe_raw(all_raw)
        path = self.settings.path("raw") / "raw_items.jsonl"
        _write_jsonl(path, [i.model_dump() for i in deduped])
        self._artifacts["raw_items"] = path
        stats["total_items"] = len(deduped)
        return deduped, all_audits, stats

    def stage_clean(self, raw_items: list[RawItem]) -> tuple[list[CleanDoc], dict]:
        self.cleaner = Cleaner(self.settings, self.policy)
        docs, stats = self.cleaner.clean_many(raw_items)
        inserted = self.repo.upsert_documents(docs)
        path = self.settings.path("processed") / "clean_docs.jsonl"
        _write_jsonl(path, [d.model_dump() for d in docs])
        self._artifacts["clean_docs"] = path
        out = stats.as_dict()
        out["inserted_to_db"] = inserted
        return docs, out

    def stage_detect(self, docs: list[CleanDoc]) -> tuple[list[AnomalyRecord], dict]:
        records, profile = self.detector.detect_many(docs)
        self.repo.upsert_anomalies(records)
        path = self.settings.path("processed") / "anomalies.jsonl"
        _write_jsonl(path, [r.model_dump() for r in records])
        self._artifacts["anomalies"] = path
        stats: dict = {
            "total": len(records),
            "anomalies": sum(1 for r in records if r.is_anomaly),
            "avg_score": round(sum(r.score for r in records) / len(records), 3)
            if records
            else 0.0,
            "corpus_top_terms": profile.global_centroid.top_terms(10)
            if profile.global_centroid
            else [],
        }
        return records, stats

    def stage_dataset(
        self, docs: list[CleanDoc], anomalies: list[AnomalyRecord]
    ) -> DatasetBundle:
        bundle = build_dataset(docs, anomalies, self.settings)
        self.repo.replace_dataset(bundle.rows)
        self._artifacts.update(bundle.paths)
        return bundle

    # ---------- прогон ----------

    def run(
        self,
        *,
        stages: tuple[str, ...] = ("audit", "collect", "clean", "detect", "dataset"),
        strict_audit: bool = True,
    ) -> PipelineResult:
        run_id = uuid.uuid4().hex[:16]
        result = PipelineResult(run_id=run_id)
        self.repo.start_run(f"{run_id}:pipeline", "pipeline")
        self._artifacts = {}

        try:
            raw_items: list[RawItem] = []
            docs: list[CleanDoc] = []

            if "audit" in stages or "collect" in stages:
                audits = self.stage_audit(strict=strict_audit)
                result.audits = audits
                result.stages["audit"] = {
                    "total": len(audits),
                    "allowed": sum(1 for a in audits if a.allowed),
                    "denied": sum(1 for a in audits if not a.allowed),
                    "reasons_denied": {a.source_id: a.reasons for a in audits if not a.allowed},
                }

            if "collect" in stages:
                raw_items, audits, stats = self.stage_collect_all()
                if not result.audits:
                    result.audits = audits
                result.raw_items = raw_items
                result.stages["collect"] = stats

            if "clean" in stages:
                if not raw_items:
                    raw_items = self._load_jsonl(
                        self.settings.path("raw") / "raw_items.jsonl", RawItem
                    )
                docs, stats = self.stage_clean(raw_items)
                result.docs = docs
                result.stages["clean"] = stats

            anomalies: list[AnomalyRecord] = []
            if "detect" in stages:
                if not docs:
                    docs = self.repo.load_documents()
                anomalies, stats = self.stage_detect(docs)
                result.anomalies = anomalies
                result.stages["detect"] = stats

            if "dataset" in stages:
                if not docs:
                    docs = self.repo.load_documents()
                if not anomalies:
                    anomalies = [
                        AnomalyRecord(
                            **{k: v for k, v in a.items() if k in AnomalyRecord.model_fields}
                        )
                        for a in self.repo.load_anomalies()
                    ]
                bundle = self.stage_dataset(docs, anomalies)
                result.dataset = bundle
                result.stages["dataset"] = bundle.summary()

            result.artifacts = {k: str(v) for k, v in self._artifacts.items()}
            result.stages["run_finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            self.repo.finish_run(f"{run_id}:pipeline", "success", result.summary())
            return result
        except Exception as exc:  # noqa: BLE001 - любая ошибка попадает в журнал
            self.repo.finish_run(f"{run_id}:pipeline", "failed", result.summary(), str(exc))
            raise

    @staticmethod
    def _load_jsonl(path: Path, model) -> list:
        if not path.exists():
            return []
        return [
            model.model_validate(json.loads(line))
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


def run_full_pipeline(**kwargs) -> PipelineResult:
    return Pipeline().run(**kwargs)
