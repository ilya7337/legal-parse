"""Отрисовка динамических (JavaScript) страниц через Playwright.

Playwright — **опциональная** зависимость. Устанавливается:
    pip install playwright && playwright install chromium

Рендер выполняется с честным User-Agent, без обхода капчи/логина; правовой аудит
(robots.txt) по-прежнему выполняется до рендера.
"""

from __future__ import annotations

from config.settings import get_settings


class PlaywrightUnavailableError(RuntimeError):
    """Playwright не установлен (или не найден браузер)."""


def _user_agent() -> str:
    return get_settings().user_agent


def render_html(url: str, *, wait_selector: str | None = None, timeout: int = 20_000) -> str:
    """Вернуть отрендеренный HTML динамической страницы."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise PlaywrightUnavailableError(
            "Playwright не установлен. Установите: "
            "`pip install playwright && playwright install chromium`"
        ) from exc

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=_user_agent())
                page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                if wait_selector:
                    page.wait_for_selector(wait_selector, timeout=timeout)
                return page.content()
            finally:
                browser.close()
    except ImportError:
        raise
    except Exception as exc:  # noqa: BLE001 - переоформляем в понятную ошибку
        raise PlaywrightUnavailableError(
            f"Не удалось отрендерить страницу Playwright: {type(exc).__name__}: {exc}"
        ) from exc
