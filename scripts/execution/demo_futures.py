"""Isolated Bybit Demo / OKX demo futures transport and guarded pair skeleton.

The existing strategy is not wired to this module. Pair execution remains blocked
until exchange-specific preflight and position reconciliation are implemented.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from core import demo_credentials, execution_mode
from core.file_lock import lock_exclusive, unlock

_VENUES = {'bybit', 'okx'}
_BASES = {'bybit': 'https://api-demo.bybit.com', 'okx': 'https://www.okx.com'}
_PATHS = {'bybit': ('/v5/order/create', '/v5/order/realtime'),
          'okx': ('/api/v5/trade/order', '/api/v5/trade/order')}
_PAIR_ACTIVE: ContextVar[bool] = ContextVar('demo_futures_pair_active', default=False)


class DemoBlocked(RuntimeError):
    """The requested operation was rejected before submission."""


class ReconciliationRequired(RuntimeError):
    """An order may have been submitted; no new leg may be sent."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('Redirects disabled')


def _quantity(value: str) -> Decimal:
    if not isinstance(value, str):
        raise DemoBlocked('Invalid quantity')
    try:
        quantity = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        raise DemoBlocked('Invalid quantity') from None
    if not quantity.is_finite() or quantity <= 0 or quantity.adjusted() > 9 or quantity.adjusted() < -12:
        raise DemoBlocked('Invalid quantity')
    return quantity


def _number(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ReconciliationRequired('Invalid exchange execution data') from None
    if not result.is_finite() or result < 0:
        raise ReconciliationRequired('Invalid exchange execution data')
    return result


def _permit(confirmed: bool) -> None:
    if confirmed is not True or os.environ.get('FARB_ENABLE_DEMO_ORDERS') != '1':
        raise DemoBlocked('Explicit demo order permission is required')
    if os.environ.get('FARB_DRY_RUN', '').strip().lower() not in ('', '0', 'false', 'no', 'off'):
        raise DemoBlocked('Dry-run mode forbids demo orders')
    if os.environ.get('FARB_LIVE', '').lower() not in ('', '0', 'false') or os.environ.get('DCA_LIVE', '').lower() not in ('', '0', 'false'):
        raise DemoBlocked('Live trading flags are incompatible with demo orders')
    if os.environ.get('BYBIT_REQUIRE_TESTNET') is not None:
        raise DemoBlocked('Bybit Testnet and Demo Trading are separate environments')
    if demo_credentials._backend() != 'age':
        raise DemoBlocked('Isolated age demo credentials are required')


def _demo_creds(venue: str) -> dict[str, str]:
    if venue not in _VENUES:
        raise DemoBlocked('Unsupported demo venue')
    try:
        creds = demo_credentials.load(venue, 'demo')
        if not creds or not all(isinstance(creds.get(k), str) and 0 < len(creds[k]) <= 512 and '\r' not in creds[k] and '\n' not in creds[k] for k in ('key', 'secret')):
            raise ValueError('Invalid credentials')
        if venue == 'okx' and (not creds.get('passphrase') or '\r' in creds['passphrase'] or '\n' in creds['passphrase']):
            raise ValueError('Invalid passphrase')
        existing = ('BYBIT_API_KEY', 'BYBIT_TESTNET_API_KEY') if venue == 'bybit' else ('OKX_API_KEY',)
        if any(os.environ.get(name) and hmac.compare_digest(creds['key'], os.environ[name]) for name in existing):
            raise ValueError('Credential belongs to another environment')
        return creds
    except Exception:
        raise DemoBlocked('Demo credentials unavailable or invalid') from None


def _validate_route(venue: str, method: str, params: dict[str, str] | None, body: dict[str, Any] | None) -> None:
    """Accept only the narrow futures calls constructed by this module."""
    if venue not in _VENUES or method not in ('GET', 'POST'):
        raise DemoBlocked('Unsupported demo route')
    if method == 'POST':
        expected = ({'category', 'symbol', 'side', 'orderType', 'qty', 'orderLinkId', 'positionIdx'}
                    if venue == 'bybit' else {'instId', 'tdMode', 'side', 'posSide', 'ordType', 'sz', 'clOrdId'})
        if params or not isinstance(body, dict) or set(body) != expected:
            raise DemoBlocked('Unsupported demo order')
        if venue == 'bybit':
            valid = (body['category'] == 'linear' and body['orderType'] == 'Market' and
                     body['side'] in ('Buy', 'Sell') and body['positionIdx'] == 0 and
                     isinstance(body['symbol'], str) and re.fullmatch(r'[A-Z][A-Z0-9]{1,14}USDT', body['symbol']) and
                     isinstance(body['orderLinkId'], str) and re.fullmatch(r'[A-Za-z0-9]{1,24}', body['orderLinkId']))
            amount = body['qty']
        else:
            valid = (body['tdMode'] == 'cross' and body['posSide'] == 'net' and body['ordType'] == 'market' and
                     body['side'] in ('buy', 'sell') and isinstance(body['instId'], str) and
                     re.fullmatch(r'[A-Z][A-Z0-9]{1,14}-USDT-SWAP', body['instId']) and
                     isinstance(body['clOrdId'], str) and re.fullmatch(r'[A-Za-z0-9]{1,24}', body['clOrdId']))
            amount = body['sz']
        if not valid:
            raise DemoBlocked('Only isolated demo futures orders are supported')
        _quantity(amount)
        return
    expected = {'category', 'symbol', 'orderId'} if venue == 'bybit' else {'instId', 'ordId'}
    if body is not None or not isinstance(params, dict) or set(params) != expected:
        raise DemoBlocked('Unsupported demo order query')
    symbol = params.get('symbol' if venue == 'bybit' else 'instId')
    order_id = params.get('orderId' if venue == 'bybit' else 'ordId')
    pattern = r'[A-Z][A-Z0-9]{1,14}USDT' if venue == 'bybit' else r'[A-Z][A-Z0-9]{1,14}-USDT-SWAP'
    if (not isinstance(symbol, str) or not re.fullmatch(pattern, symbol) or not isinstance(order_id, str) or
            not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', order_id) or
            (venue == 'bybit' and params.get('category') != 'linear')):
        raise DemoBlocked('Unsupported demo order query')


def _request(venue: str, method: str, params: dict[str, str] | None, body: dict[str, Any] | None, confirmed: bool) -> dict[str, Any]:
    """Fixed demo-only private endpoints, no redirects, no automatic retries."""
    _permit(confirmed)
    _validate_route(venue, method, params, body)
    if not _PAIR_ACTIVE.get():
        raise DemoBlocked('Private transport requires a journalled demo pair')
    creds = _demo_creds(venue)
    route = _PATHS[venue][0 if method == 'POST' else 1]
    query = urllib.parse.urlencode(params or {})
    target = route + ('?' + query if query else '')
    serialized = json.dumps(body, separators=(',', ':'), ensure_ascii=True) if body is not None else ''
    if venue == 'bybit':
        timestamp = str(int(time.time() * 1000))
        signature = hmac.new(creds['secret'].encode(), (timestamp + creds['key'] + '5000' + (query if method == 'GET' else serialized)).encode(), hashlib.sha256).hexdigest()
        headers = {'X-BAPI-API-KEY': creds['key'], 'X-BAPI-SIGN': signature,
                   'X-BAPI-TIMESTAMP': timestamp, 'X-BAPI-RECV-WINDOW': '5000'}
    else:
        timestamp = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        signature = base64.b64encode(hmac.new(creds['secret'].encode(), (timestamp + method + target + serialized).encode(), hashlib.sha256).digest()).decode()
        headers = {'OK-ACCESS-KEY': creds['key'], 'OK-ACCESS-SIGN': signature,
                   'OK-ACCESS-TIMESTAMP': timestamp, 'OK-ACCESS-PASSPHRASE': creds['passphrase'],
                   'x-simulated-trading': '1'}
    headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(_BASES[venue] + target, data=serialized.encode() if body is not None else None,
                                     method=method, headers=headers)
    # Hold the same interprocess lock used by mode changes until the request
    # is complete. No production route or credential fallback exists here.
    with execution_mode._locked() as directory:
        try:
            mode = execution_mode._read(directory)
        except Exception:
            raise DemoBlocked('Execution mode unavailable') from None
        _permit(confirmed)
        if mode != 'demo':
            raise DemoBlocked('Demo mode is required')
        try:
            with urllib.request.build_opener(_NoRedirect()).open(request, timeout=10) as response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError('response too large')
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError('invalid response')
                return result
        except Exception:
            # Exchange exceptions can include sensitive headers or response bodies.
            raise ReconciliationRequired('Exchange request outcome unknown') from None


@dataclass(frozen=True)
class FuturesPair:
    base: str
    long_venue: str
    short_venue: str
    base_quantity: str
    okx_contracts: str
    okx_contract_size: str

    def validate(self) -> None:
        if not isinstance(self.base, str) or not re.fullmatch(r'[A-Z][A-Z0-9]{1,14}', self.base):
            raise DemoBlocked('Invalid underlying')
        if not isinstance(self.long_venue, str) or not isinstance(self.short_venue, str) or {self.long_venue, self.short_venue} != _VENUES:
            raise DemoBlocked('Exactly Bybit and OKX futures are supported')
        if _quantity(self.okx_contracts) * _quantity(self.okx_contract_size) != _quantity(self.base_quantity):
            raise DemoBlocked('Futures quantities do not match')


def _order_fields(venue: str, base: str, side: str, qty: str, client_id: str) -> dict[str, str]:
    if venue not in _VENUES or side not in ('buy', 'sell'):
        raise DemoBlocked('Unsupported demo futures leg')
    qty = format(_quantity(qty), 'f')
    if venue == 'bybit':
        return {'category': 'linear', 'symbol': base + 'USDT', 'side': side.title(), 'orderType': 'Market',
                'qty': qty, 'orderLinkId': client_id, 'positionIdx': 0}
    return {'instId': base + '-USDT-SWAP', 'tdMode': 'cross', 'side': side, 'posSide': 'net',
            'ordType': 'market', 'sz': qty, 'clOrdId': client_id}


def _create_and_confirm(venue: str, fields: dict[str, str], confirmed: bool, on_ack) -> dict[str, str]:
    ack = _request(venue, 'POST', None, fields, confirmed)
    try:
        if venue == 'bybit':
            if ack.get('retCode') != 0:
                raise ValueError('rejected')
            order_id = ack['result']['orderId']
            params = {'category': 'linear', 'symbol': fields['symbol'], 'orderId': order_id}
        else:
            if ack.get('code') != '0' or ack['data'][0]['sCode'] != '0':
                raise ValueError('rejected')
            order_id = ack['data'][0]['ordId']
            params = {'instId': fields['instId'], 'ordId': order_id}
        if not isinstance(order_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', order_id):
            raise ValueError('invalid order id')
    except (KeyError, IndexError, TypeError, ValueError):
        raise ReconciliationRequired('Order acknowledgement is not confirmed') from None
    # Persist the exchange ID before querying execution status. The client ID
    # was already journalled even if the process died before receiving an ack.
    on_ack(order_id)
    status = _request(venue, 'GET', params, None, confirmed)
    try:
        if venue == 'bybit':
            if status.get('retCode') != 0: raise ValueError('status')
            order = status['result']['list'][0]
            valid = (order['orderId'] == order_id and order['orderLinkId'] == fields['orderLinkId'] and
                     order['symbol'] == fields['symbol'] and order['side'] == fields['side'] and
                     order['orderStatus'] == 'Filled')
            filled, price = _number(order['cumExecQty']), _number(order['avgPrice'])
            expected = _quantity(fields['qty'])
        else:
            if status.get('code') != '0': raise ValueError('status')
            order = status['data'][0]
            valid = (order['ordId'] == order_id and order['clOrdId'] == fields['clOrdId'] and
                     order['instId'] == fields['instId'] and order['side'] == fields['side'] and
                     order['state'] == 'filled')
            filled, price = _number(order['accFillSz']), _number(order['avgPx'])
            expected = _quantity(fields['sz'])
        if not valid or filled != expected or price <= 0:
            raise ValueError('not fully filled')
    except (KeyError, IndexError, TypeError, ValueError, ReconciliationRequired):
        raise ReconciliationRequired('Order is not confirmed fully filled') from None
    return {'venue': venue, 'order_id': order_id, 'filled_qty': str(filled), 'avg_price': str(price)}


def _preflight(pair: FuturesPair) -> None:
    # The legacy pure-futures executor has fail-open balance checks and can
    # submit legs in parallel. Do not reuse it for demo orders. Exchange-backed
    # checks of contract metadata, quantity steps, funding, margin, leverage,
    # fees and current positions must be implemented before this gate opens.
    raise DemoBlocked('Exchange preflight is not implemented; demo pair execution is disabled')


def _journal_path() -> Path:
    return execution_mode._directory() / 'demo-futures-intent.json'


def _write_journal(path: Path, value: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix='.demo-futures-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(value, file, separators=(',', ':'), sort_keys=True)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def execute_pair(pair: FuturesPair, *, confirmed: bool = False) -> dict[str, Any]:
    """Sequential two-futures skeleton; intentionally blocked by preflight.

    Tests replace only the unfinished preflight and mock all exchange I/O.
    No automatic retry, compensation or reconciliation is attempted.
    """
    pair.validate()
    _permit(confirmed)
    if execution_mode.current_mode() != 'demo':
        raise DemoBlocked('Demo mode is required')
    path = _journal_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        if not lock_exclusive(fd, blocking=False):
            raise DemoBlocked('Another demo pair is in progress')
        # An existing intent (including one created immediately before a crash)
        # blocks new trades until an operator reconciles it outside this module.
        if path.exists() or path.is_symlink():
            raise DemoBlocked('Existing demo intent requires manual reconciliation')
        _preflight(pair)
        position_id = uuid.uuid4().hex
        long_id, short_id = uuid.uuid4().hex[:24], uuid.uuid4().hex[:24]
        def qty_for(venue: str) -> str:
            return pair.base_quantity if venue == 'bybit' else pair.okx_contracts
        legs = [_order_fields(pair.long_venue, pair.base, 'buy', qty_for(pair.long_venue), long_id),
                _order_fields(pair.short_venue, pair.base, 'sell', qty_for(pair.short_venue), short_id)]
        record: dict[str, Any] = {'version': 1, 'id': position_id, 'state': 'prepared',
                                  'base': pair.base, 'legs': [{'venue': pair.long_venue, 'request': legs[0]},
                                                               {'venue': pair.short_venue, 'request': legs[1]}]}
        _write_journal(path, record)
        token = _PAIR_ACTIVE.set(True)
        try:
            for index, venue in enumerate((pair.long_venue, pair.short_venue)):
                record['state'] = f'leg_{index + 1}_submitting'
                _write_journal(path, record)  # Durable intent precedes each POST.
                try:
                    def save_ack(order_id: str) -> None:
                        record['legs'][index]['order_id'] = order_id
                        record['state'] = f'leg_{index + 1}_acknowledged'
                        _write_journal(path, record)
                    result = _create_and_confirm(venue, legs[index], confirmed, save_ack)
                except Exception:
                    record['state'] = 'reconciliation_required'
                    _write_journal(path, record)
                    return {'state': 'reconciliation_required', 'id': position_id, 'confirmed_legs': index}
                record['legs'][index]['confirmation'] = result
                record['state'] = f'leg_{index + 1}_filled'
                _write_journal(path, record)
            # Both order fills alone do not prove exchange positions match.
            record['state'] = 'position_reconciliation_required'
            _write_journal(path, record)
            return {'state': record['state'], 'id': position_id, 'confirmed_legs': 2}
        finally:
            _PAIR_ACTIVE.reset(token)
    finally:
        unlock(fd)
        os.close(fd)
