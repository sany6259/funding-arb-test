"""Stage A: isolated, stateless demo-account checks. No order endpoints."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import threading
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["demo-readonly"])
_lock = threading.RLock()
_mode = "simulation"

# Only these exact HTTPS GET endpoints may receive demo credentials.
_ENDPOINTS = {
    "bybit": ("https://api-demo.bybit.com", "/v5/account/wallet-balance?accountType=UNIFIED&coin=USDT"),
    "okx": ("https://www.okx.com", "/api/v5/account/balance?ccy=USDT"),
    "bitget": ("https://api.bitget.com", "/api/v2/spot/account/assets?coin=USDT"),
}


def current_mode() -> str:
    with _lock:
        return _mode


@router.get("/execution-mode")
async def execution_mode():
    mode = current_mode()
    return {"mode": mode, "environment": "demo" if mode == "demo" else "none",
            "orders_enabled": False, "live_supported": False}


@router.post("/execution-mode")
async def set_execution_mode(request: Request):
    global _mode
    try:
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("JSON required")
        payload = await request.json()
        value = payload.get("mode") if isinstance(payload, dict) else None
    except Exception:
        value = None
    if value not in ("simulation", "demo"):
        return JSONResponse(status_code=400, content={"error": "Режим недоступен; реальная торговля отключена."})
    with _lock:
        _mode = value
    return {"mode": value, "environment": "demo" if value == "demo" else "none", "orders_enabled": False}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Redirects are disabled")


def _balance(venue: str, response: Any) -> str | None:
    if not isinstance(response, dict):
        raise ValueError("Unexpected response")
    if venue == "bybit":
        if response.get("retCode") != 0:
            raise ValueError("Authentication failed")
        accounts = response.get("result", {}).get("list")
        if not isinstance(accounts, list) or not accounts:
            raise ValueError("Missing account")
        entries = accounts[0].get("coin")
        field, asset = "walletBalance", "coin"
    elif venue == "okx":
        if response.get("code") != "0":
            raise ValueError("Authentication failed")
        accounts = response.get("data")
        if not isinstance(accounts, list) or not accounts:
            raise ValueError("Missing account")
        entries = accounts[0].get("details")
        field, asset = "eq", "ccy"
    else:
        if response.get("code") != "00000":
            raise ValueError("Authentication failed")
        entries = response.get("data")
        field, asset = "available", "coin"
    if not isinstance(entries, list):
        raise ValueError("Missing balance")
    for entry in entries:
        if isinstance(entry, dict) and entry.get(asset) == "USDT":
            try:
                amount = Decimal(str(entry[field]))
            except (KeyError, ValueError, InvalidOperation):
                raise ValueError("Invalid balance") from None
            if not amount.is_finite() or amount < 0:
                raise ValueError("Invalid balance")
            return format(amount, "f")
    return None  # An absent asset is not a verified zero balance.


def _check(venue: str, key: str, secret: str, passphrase: str) -> str | None:
    base, path = _ENDPOINTS[venue]
    ts_ms = str(int(time.time() * 1000))
    if venue == "bybit":
        query = path.split("?", 1)[1]
        sig = hmac.new(secret.encode(), (ts_ms + key + "5000" + query).encode(), hashlib.sha256).hexdigest()
        headers = {"X-BAPI-API-KEY": key, "X-BAPI-SIGN": sig,
                   "X-BAPI-TIMESTAMP": ts_ms, "X-BAPI-RECV-WINDOW": "5000"}
    else:
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z") if venue == "okx" else ts_ms
        sig = base64.b64encode(hmac.new(secret.encode(), (ts + "GET" + path).encode(), hashlib.sha256).digest()).decode()
        if venue == "okx":
            headers = {"OK-ACCESS-KEY": key, "OK-ACCESS-SIGN": sig, "OK-ACCESS-TIMESTAMP": ts,
                       "OK-ACCESS-PASSPHRASE": passphrase, "x-simulated-trading": "1"}
        else:
            headers = {"ACCESS-KEY": key, "ACCESS-SIGN": sig, "ACCESS-TIMESTAMP": ts,
                       "ACCESS-PASSPHRASE": passphrase, "paptrading": "1"}
    req = urllib.request.Request(base + path, method="GET", headers=headers)
    with urllib.request.build_opener(_NoRedirect()).open(req, timeout=10) as response:
        data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("Response too large")
        return _balance(venue, json.loads(data))


def _is_loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return bool(host and ipaddress.ip_address(host).is_loopback)
    except ValueError:
        return False


def _local_request(request: Request) -> bool:
    if not request.client or not _is_loopback(request.client.host) or not _is_loopback(request.url.hostname):
        return False
    if any(name in request.headers for name in ("forwarded", "x-forwarded-for", "x-forwarded-host")):
        return False
    origin = request.headers.get("origin")
    if origin:
        parsed = urllib.parse.urlsplit(origin)
        if parsed.scheme not in ("http", "https") or not _is_loopback(parsed.hostname):
            return False
    return True


@router.post("/demo/check")
async def check_demo(request: Request):
    if not _local_request(request):
        return JSONResponse(status_code=403, content={"error": "Проверка доступна только с локального компьютера."})
    if current_mode() != "demo":
        return JSONResponse(status_code=409, content={"error": "Сначала выберите демо-режим."})
    try:
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("JSON required")
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("Invalid request")
        venue, environment = payload.get("venue"), payload.get("environment")
        key, secret, passphrase = payload.get("api_key"), payload.get("api_secret"), payload.get("passphrase", "")
        if venue not in _ENDPOINTS or environment != "demo":
            raise ValueError("Unsupported environment")
        if not all(isinstance(x, str) and 0 < len(x) <= 512 and "\r" not in x and "\n" not in x for x in (key, secret)):
            raise ValueError("Missing credentials")
        if not isinstance(passphrase, str) or len(passphrase) > 512 or "\r" in passphrase or "\n" in passphrase or (venue != "bybit" and not passphrase):
            raise ValueError("Missing passphrase")
        balance = await asyncio.to_thread(_check, venue, key, secret, passphrase)
        if current_mode() != "demo":
            return JSONResponse(status_code=409, content={"error": "Режим изменился; повторите проверку."})
        return {"venue": venue, "environment": "demo", "connected": True,
                "credentials": "••••", "balance": {"asset": "USDT", "amount": balance}, "orders_enabled": False}
    except Exception:
        # No remote messages, request body or secret values in logs or response.
        return JSONResponse(status_code=400, content={"error": "Не удалось проверить демо-доступ или баланс."})
