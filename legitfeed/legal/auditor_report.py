"""Отчёт правового аудита в Markdown + JSON (артефакт для защиты проекта)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from config.settings import get_settings
from legitfeed.legal.auditor import AuditResult
from legitfeed.legal.policy import LegalPolicy


@dataclass
class AuditReport:
    results: list[AuditResult]
    policy: LegalPolicy
    generated_at: str

    @property
    def allowed(self) -> list[AuditResult]:
        return [r for r in self.results if r.allowed]

    @property
    def denied(self) -> list[AuditResult]:
        return [r for r in self.results if not r.allowed]

    def to_markdown(self) -> str:
        lines: list[str] = []
        lines.append("# Отчёт правового аудита источников")
        lines.append("")
        lines.append(f"Сгенерирован: `{self.generated_at}`")
        lines.append(f"Версия политики: `{self.policy.version}`")
        lines.append("")
        lines.append(
            f"Проверено источников: **{len(self.results)}**, "
            f"разрешено: **{len(self.allowed)}**, запрещено: **{len(self.denied)}**."
        )
        lines.append("")
        lines.append("## Сводка")
        lines.append("")
        lines.append("| Источник | Вердикт | robots.txt | Crawl-delay | Причины |")
        lines.append("|---|---|---|---|---|")
        for r in self.results:
            verdict = "✅ разрешён" if r.allowed else "⛔ запрещён"
            delay = f"{r.crawl_delay:.1f} c" if r.crawl_delay is not None else "—"
            reason = "<br>".join(r.reasons) if r.reasons else "—"
            lines.append(
                f"| `{r.source_id}` | {verdict} | {r.robots_status} | {delay} | {reason} |"
            )
        lines.append("")
        for r in self.results:
            lines.append(f"### `{r.source_id}`")
            lines.append("")
            lines.append(f"* robots.txt: `{r.robots_url}` (HTTP {r.robots_status})")
            for path, ok in r.robots_allowed_paths.items():
                lines.append(f"  * {'ALLOW' if ok else 'DISALLOW'} — `{path}`")
            if r.request_rate:
                reqs, period = r.request_rate
                lines.append(f"* Request-rate: {reqs} запрос(ов) / {period} c")
            lines.append(f"* ToS-вердикт (авто): `{r.tos_verdict}`")
            for s in r.tos_signals:
                lines.append(f"  * [{s['kind']}] {s['reason']}: «{s['quote']}»")
            if r.reasons:
                lines.append("* Заключение:")
                for reason in r.reasons:
                    lines.append(f"  * {reason}")
            else:
                lines.append("* Заключение: ограничений не выявлено")
            lines.append("")
        lines.append("## Базовые ограничения политики")
        lines.append("")
        lines.append(f"* robots fail-closed при 5xx: `{self.policy.robots.fail_closed_on_server_error}`")
        lines.append(f"* Признание 401/403 запретом: `{self.policy.robots.disallow_on_auth_required}`")
        lines.append(f"* Требуется ToS-разрешение фида: `{self.policy.terms_of_service.require_tos_allow_feed}`")
        lines.append(f"* Требуется аудит источника: `{self.policy.terms_of_service.require_source_audit}`")
        lines.append(f"* Глобальный потолок: `{self.policy.rate_limiting.max_global_rps}` rps")
        lines.append(f"* Маскирование ПДн: `{self.policy.personal_data.mask_pii}`")
        lines.append("")
        lines.append("## Запрещённые действия")
        lines.append("")
        for action in self.policy.forbidden_actions:
            lines.append(f"* `{action}`")
        lines.append("")
        return "\n".join(lines)

    def to_json(self) -> str:
        payload = {
            "generated_at": self.generated_at,
            "policy_version": self.policy.version,
            "sources": [r.as_dict() for r in self.results],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def save(self, out_dir: Path | None = None) -> tuple[Path, Path]:
        target = out_dir or get_settings().path("audit")
        target.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        md_path = target / "legal_audit.md"
        json_path = target / f"legal_audit_{stamp}.json"
        md_path.write_text(self.to_markdown(), encoding="utf-8")
        json_path.write_text(self.to_json(), encoding="utf-8")
        return md_path, json_path


def build_report(results: list[AuditResult], policy: LegalPolicy) -> AuditReport:
    return AuditReport(
        results=results,
        policy=policy,
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
