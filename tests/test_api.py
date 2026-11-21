"""Smoke-тесты FastAPI-приложения (без внешней сети)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from legitfeed.api.app import app


def test_health():
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ok"
    assert "version" in payload


def test_index_html():
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "LegitFeed" in resp.text


def test_stats_endpoint():
    client = TestClient(app)
    resp = client.get("/api/stats")
    assert resp.status_code == 200
    assert set(resp.json()) >= {"documents", "anomalies", "dataset_rows"}
