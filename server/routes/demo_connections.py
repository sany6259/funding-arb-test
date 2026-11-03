"""Isolated read-only demo-account checks. No order endpoints."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from core import demo_credentials, execution_mode as shared_mode

router = APIRouter(tags=["demo-readonly"])

# Only these exact HTTPS GET endpoints may receive demo credentials.
_ENDPOINTS = {
    "bybit": ("https://api-demo.bybit.com", "/v5/account/wallet-balance?accountType=UNIFIED&coin=USDT"),
    "okx": ("https://www.okx.com", "/api/v5/account/balance?ccy=USDT"),
    "bitget": ("https://api.bitget.com", "/api/v2/spot/account/assets?coin=USDT"),
}


def current_mode() -> str:
    return shared_mode.current_mode()


@router.get("/execution-mode")
async def execution_mode():
    try:
        mode = current_mode()
        return {"mode": mode, "environment": "demo" if mode == "demo" else "none",
                "orders_enabled": False, "live_supported": False}
    except Exception:
        return JSONResponse(status_code=503, content={"error": "Состояние режима недоступно; ордера отключены."})


@router.post("/execution-mode")
async def set_execution_mode(request: Request):
    if not _local_request(request):
        return JSONResponse(status_code=403, content={"error": "Доступ только с локального компьютера."})
    try:
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("JSON required")
        payload = await request.json()
        value = payload.get("mode") if isinstance(payload, dict) else None
        if value not in ("simulation", "demo"):
            raise ValueError("invalid mode")
        shared_mode.set_mode(value)
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "Режим недоступен; реальная торговля отключена."})
    except Exception:
        return JSONResponse(status_code=503, content={"error": "Состояние режима недоступно; ордера отключены."})
    return {"mode": value, "environment": "demo" if value == "demo" else "none", "orders_enabled": False}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Redirects are disabled")


def _balance(venue: str, response: Any) -> str | None:
    if not isinstance(response, dict):
        raise ValueError("Unexpected response")
    if venue == "bybit":
        if response.get("retCode") != 0:
            raise ValueError("Authentication or exchange error")
        accounts = response.get("result", {}).get("list")
        if not isinstance(accounts, list) or not accounts or not isinstance(accounts[0], dict):
            raise ValueError("Missing account")
        # UTA reports account-wide availability in USD, not per-coin USDT.
        raw = accounts[0].get("totalAvailableBalance")
    elif venue == "okx":
        if response.get("code") != "0":
            raise ValueError("Authentication or exchange error")
        accounts = response.get("data")
        if not isinstance(accounts, list) or not accounts or not isinstance(accounts[0], dict):
            raise ValueError("Missing account")
        entries = accounts[0].get("details")
        if not isinstance(entries, list):
            raise ValueError("Missing balance")
        raw = next((entry.get("availBal") for entry in entries if isinstance(entry, dict) and entry.get("ccy") == "USDT"), None)
    else:
        if response.get("code") != "00000":
            raise ValueError("Authentication or exchange error")
        entries = response.get("data")
        if not isinstance(entries, list):
            raise ValueError("Missing balance")
        raw = next((entry.get("available") for entry in entries if isinstance(entry, dict) and entry.get("coin") == "USDT"), None)
    if raw is None or raw == "":
        return None
    try:
        amount = Decimal(str(raw))
    except (ValueError, InvalidOperation):
        raise ValueError("Invalid balance") from None
    if not amount.is_finite() or amount < 0 or amount.adjusted() > 30 or amount.adjusted() < -30:
        raise ValueError("Invalid balance")
    return format(amount, "f")


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
    if venue == "okx":
        import httpx
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            response = client.get(base + path, headers=headers)
            response.raise_for_status()
            data = response.content
            if len(data) > 1024 * 1024:
                raise ValueError("Response too large")
            return _balance(venue, json.loads(data))

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


def _failure(message: str, status: int = 400):
    return JSONResponse(status_code=status, content={"error": message})


@router.get("/demo/credentials")
async def credential_status(request: Request):
    if not _local_request(request):
        return _failure("Доступ только с локального компьютера.", 403)
    try:
        configured = {venue: demo_credentials.load(venue, 'demo') is not None for venue in _ENDPOINTS}
        return {"environment": "demo", "configured": configured}
    except Exception:
        return _failure("Защищённое хранилище недоступно.", 503)


async def _demo_payload(request: Request):
    if not _local_request(request):
        return None, _failure("Доступ только с локального компьютера.", 403)
    try:
        if current_mode() != 'demo':
            return None, _failure("Сначала выберите демо-режим.", 409)
        if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
            raise ValueError('JSON required')
        payload = await request.json()
        if not isinstance(payload, dict) or payload.get('venue') not in _ENDPOINTS or payload.get('environment') != 'demo':
            raise ValueError('Invalid environment')
        return payload, None
    except RuntimeError:
        return None, _failure("Состояние режима недоступно; ордера отключены.", 503)
    except Exception:
        return None, _failure("Некорректный запрос.")


@router.post("/demo/credentials")
async def save_credentials(request: Request):
    payload, error = await _demo_payload(request)
    if error:
        return error
    try:
        venue = payload['venue']
        key, secret, passphrase = payload.get('api_key'), payload.get('api_secret'), payload.get('passphrase', '')
        if not all(isinstance(x, str) and 0 < len(x) <= 512 and '\r' not in x and '\n' not in x for x in (key, secret)):
            raise ValueError('Invalid credentials')
        if not isinstance(passphrase, str) or len(passphrase) > 512 or '\r' in passphrase or '\n' in passphrase or (venue != 'bybit' and not passphrase):
            raise ValueError('Invalid passphrase')
        # Reject known live/testnet API keys; exchange authenticates the remaining key as demo.
        live_key_names = {'bybit': ('BYBIT_API_KEY', 'BYBIT_TESTNET_API_KEY'),
                          'okx': ('OKX_API_KEY',), 'bitget': ('BITGET_API_KEY',)}
        if any(os.environ.get(name) and hmac.compare_digest(key, os.environ[name]) for name in live_key_names[venue]):
            raise ValueError('Key already assigned to another environment')
        await asyncio.to_thread(demo_credentials.save, venue, 'demo', key, secret, passphrase)
        return {"venue": venue, "environment": "demo", "configured": True, "connected": False}
    except ValueError:
        return _failure("Проверьте поля и используйте отдельный демоключ.")
    except Exception:
        return _failure("Не удалось сохранить ключ в защищённом хранилище.", 503)


@router.post("/demo/credentials/delete")
async def delete_credentials(request: Request):
    payload, error = await _demo_payload(request)
    if error:
        return error
    try:
        await asyncio.to_thread(demo_credentials.delete, payload['venue'], 'demo')
        return {"venue": payload['venue'], "environment": "demo", "configured": False}
    except Exception:
        return _failure("Не удалось удалить ключ из защищённого хранилища.", 503)


@router.post("/demo/check")
async def check_demo(request: Request):
    payload, error = await _demo_payload(request)
    if error:
        return error
    try:
        venue = payload['venue']
        creds = await asyncio.to_thread(demo_credentials.load, venue, 'demo')
        if creds is None:
            return _failure("Сначала сохраните демоключ.", 409)
        # The remote adapter has no credential fallback and fixes the demo endpoint and headers.
        balance = await asyncio.to_thread(_check, venue, creds['key'], creds['secret'], creds['passphrase'])
        if current_mode() != 'demo':
            return _failure("Режим изменился; повторите проверку.", 409)
        return {"venue": venue, "environment": "demo", "connected": True,
                "credentials": "••••", "balance": {"asset": "USD" if venue == "bybit" else "USDT", "amount": balance,
                    "account_type": {"bybit": "Unified Account", "okx": "Trading Account", "bitget": "Spot Account"}[venue]},
                "orders_enabled": False}
    except Exception:
        return _failure("Проверка не пройдена: ошибка авторизации, баланса или связи с биржей.")
