#!/usr/bin/env python3
"""Shared HTTP helpers for CEX venue adapters."""

from __future__ import annotations

import os
import json
import re
import time
import urllib.parse
import urllib.request
from typing import Any, Optional

import requests

_DEFAULT_UA = "Mozilla/5.0 (compatible; funding-arb/1.0)"


def credentials_file() -> str:
    """Plaintext credentials JSON path used by venue adapters as an env fallback."""
    return os.path.expanduser("~/.funding-arb/credentials.json")

# Module-level session with connection pooling and keep-alive.
# Thread-safe: requests.Session is safe for concurrent use across threads.
_session = requests.Session()
_session.headers.update({"User-Agent": _DEFAULT_UA})
# Connection pool settings: reuse up to 20 connections per host, 100 total
_adapter = requests.adapters.HTTPAdapter(
    pool_connections=64, pool_maxsize=64, max_retries=0
)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('Redirects are disabled')


_DEMO_PUBLIC_PATHS = {
    'api.bybit.com': {
        '/v5/market/tickers': {'category', 'symbol'},
        '/v5/market/instruments-info': {'category', 'symbol', 'limit', 'cursor'},
        '/v5/market/orderbook': {'category', 'symbol', 'limit'},
    },
    'www.okx.com': {
        '/api/v5/public/funding-rate': {'instId'},
        '/api/v5/public/funding-rate-history': {'instId', 'limit', 'after', 'before'},
        '/api/v5/public/mark-price': {'instType', 'instId'},
        '/api/v5/public/instruments': {'instType', 'instId'},
        '/api/v5/public/interest-rate-loan-quota': set(),
        '/api/v5/market/ticker': {'instId'},
        '/api/v5/market/tickers': {'instType'},
        '/api/v5/market/books': {'instId', 'sz'},
    },
}


def _demo_public_get(url: str, *, timeout: int) -> Any:
    """Anonymous, allowlisted demo-market GET; never forwards credentials."""
    from core import execution_mode
    if not isinstance(url, str) or len(url) > 2048:
        raise execution_mode.LegacyAccessBlocked('Unsupported demo market URL')
    try:
        parsed = urllib.parse.urlsplit(url)
        paths = _DEMO_PUBLIC_PATHS.get(parsed.netloc, {})
        if (parsed.scheme != 'https' or parsed.fragment or parsed.path not in paths or
                parsed.username is not None or parsed.password is not None):
            raise ValueError('unsupported endpoint')
        params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
        if (len({key for key, _ in params}) != len(params) or
                any(key not in paths[parsed.path] or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', value)
                    for key, value in params)):
            raise ValueError('unsupported query')
        target = ('https://api-demo.bybit.com' if parsed.netloc == 'api.bybit.com' else 'https://www.okx.com')
        target += parsed.path + ('?' + urllib.parse.urlencode(params) if params else '')
        headers = {'User-Agent': _DEFAULT_UA}
        if parsed.netloc == 'www.okx.com':
            headers['x-simulated-trading'] = '1'
        req = urllib.request.Request(target, headers=headers, method='GET')
        with execution_mode._locked() as directory:
            if execution_mode._read(directory) != 'demo':
                raise ValueError('mode changed')
            execution_mode.require_network_enabled()
            with urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise ValueError('response too large')
                payload = json.loads(raw)
        if (not isinstance(payload, dict) or
                (parsed.netloc == 'api.bybit.com' and payload.get('retCode') != 0) or
                (parsed.netloc == 'www.okx.com' and payload.get('code') != '0')):
            raise ValueError('invalid market response')
        return payload
    except Exception:
        raise execution_mode.LegacyAccessBlocked('Demo market request blocked or unavailable') from None


def http_get_json(
    url: str, timeout: int = 8, retries: int = 3, backoff: float = 0.5
) -> Any:
    """Fetch JSON from URL with retries and connection pooling."""
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        from core.execution_mode import LegacyAccessBlocked, current_mode, require_network_enabled, simulation_request
        try:
            require_network_enabled()
            try:
                mode = current_mode()
            except Exception:
                raise LegacyAccessBlocked('Execution mode unavailable') from None
            if mode == 'demo':
                return _demo_public_get(url, timeout=timeout)
            resp = simulation_request(_session.get, url, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except LegacyAccessBlocked:
            raise
        except requests.HTTPError as e:
            last_err = e
            status = e.response.status_code if e.response is not None else 0
            # Client errors (bad symbol, auth, 404...) won't change on retry —
            # fail fast. 429 (rate limit) is transient, so keep retrying it.
            if 400 <= status < 500 and status != 429:
                break
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
    raise last_err if last_err else RuntimeError("http_get_json failed")


def parse_kline_ohlcv(k: list) -> dict[str, Any]:
    """Normalize [ts, o, h, l, c, vol, ...] candle rows."""
    return {
        "ts": int(k[0]),
        "open": float(k[1]),
        "high": float(k[2]),
        "low": float(k[3]),
        "close": float(k[4]),
        "vol": float(k[5]),
    }


def rules_for_price(rules: dict[str, Any], price: float) -> dict[str, Any]:
    min_usdt = float(rules.get("min_trade_usdt", 0))
    min_base = float(rules.get("min_trade_base", 0))
    min_from_base = min_base * price if price > 0 else 0.0
    min_buy_usdt = max(min_usdt, min_from_base)
    min_buy_usdt = round(min_buy_usdt * 1.02, 2)
    qp = int(rules.get("quote_precision", 2))
    qty_prec = int(rules.get("quantity_precision", 6))
    return {
        "min_buy_usdt": min_buy_usdt,
        "min_sell_usdt": min_buy_usdt,
        "min_base_amount": min_base,
        "min_base_usdt_equiv": min_from_base,
        "quote_precision": qp,
        "quantity_precision": qty_prec,
    }
