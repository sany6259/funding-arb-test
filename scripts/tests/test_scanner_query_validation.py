"""Exercise scanner query validation through FastAPI without starting its lifespan."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from server.routes import scanner


@pytest.fixture
def app(monkeypatch):
    # No lifespan, exchange scanner, credentials, or order execution is involved.
    monkeypatch.setattr(scanner, "_scanning_strategies", {"pure"})
    monkeypatch.setattr(scanner, "_pure_results", {"marker": "pure", "venues": []})
    monkeypatch.setattr(scanner, "_carry_results", [{"marker": "carry"}])
    monkeypatch.setattr(scanner, "_unified_results", [{"marker": "unified"}])
    monkeypatch.setattr(scanner, "_scan_pure_fn", None)
    monkeypatch.setattr(scanner, "_scan_carry_fn", None)
    monkeypatch.setattr(scanner, "_unified_carry_cls", None)
    application = FastAPI()
    application.include_router(scanner.router, prefix="/api")
    return application


def request(app, method, path, params=None):
    async def run():
        sent = []
        delivered = False
        never = asyncio.Event()

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await never.wait()

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": method, "scheme": "http",
            "path": path, "raw_path": path.encode(),
            "query_string": urlencode(params or {}).encode(), "root_path": "",
            "headers": [], "client": ("127.0.0.1", 12345), "server": ("test", 80),
        }
        await app(scope, receive, send)
        status = next(m["status"] for m in sent if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        return status, json.loads(body)

    return asyncio.run(run())


ROUTES = [("GET", "/api/scanner/status"), ("GET", "/api/scanner/opportunities"), ("POST", "/api/scanner/trigger")]


@pytest.mark.parametrize("method,path", ROUTES)
@pytest.mark.parametrize("strategy", [None, "pure", "carry", "unified"])
def test_allowed_and_default_strategy(app, method, path, strategy):
    params = {} if strategy is None else {"strategy": strategy}
    status, body = request(app, method, path, params)
    expected = strategy or "pure"
    assert status == 200
    if path.endswith("/status"):
        assert body["data"]["scanning"] is (expected == "pure")
    elif path.endswith("/opportunities"):
        data = body["data"]
        assert (data if expected == "pure" else data[0])["marker"] == expected
    elif expected == "pure":
        assert body["error"] == "Scan already in progress"
    elif expected == "carry":
        assert body["error"] == "Cash-and-carry scanner unavailable"
    else:
        assert body["error"] == "Unified carry scanner unavailable"


@pytest.mark.parametrize("method,path", ROUTES)
@pytest.mark.parametrize("strategy", ["", "PURE", "purex", "xcarry", "pure\n", "pure|carry"])
def test_invalid_strategy_returns_422(app, method, path, strategy):
    status, body = request(app, method, path, {"strategy": strategy})
    assert status == 422
    assert any(error["loc"] == ["query", "strategy"] for error in body["detail"])
