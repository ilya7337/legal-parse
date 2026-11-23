"""Работа с секретами (API-ключи) без хранения их в конфиге.

Секрет берётся из переменной окружения и никогда не попадает в логи/кэш/датасет.
"""

from __future__ import annotations

import os


class MissingSecretError(Exception):
    """Нужный секрет не задан в окружении."""


def resolve_secret(env: str) -> str:
    """Прочитать секрет из переменной окружения. Бросает MissingSecretError."""
    token = os.environ.get(env, "").strip()
    if not token:
        raise MissingSecretError(
            f"Секрет не задан: установите переменную окружения {env} "
            "(например, `$env:{env}=\"...\"` в PowerShell). Секреты не хранятся в конфиге."
        )
    return token
