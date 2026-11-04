"""Isolated Bybit Demo / OKX futures transport and journalled pair execution."""
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
_CANCEL_PATHS = {'bybit': '/v5/order/cancel', 'okx': '/api/v5/trade/cancel-order'}
_PAIR_ACTIVE: ContextVar[bool] = ContextVar('demo_futures_pair_active', default=False)


class DemoBlocked(RuntimeError):
    """The requested operation was rejected before submission."""


class ReconciliationRequired(RuntimeError):
    """An order may have been submitted; no new leg may be sent."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('Redirects disabled')


def _demo_base(venue: str) -> str:
    expected = {'bybit': 'https://api-demo.bybit.com', 'okx': 'https://www.okx.com'}
    if venue not in expected or _BASES.get(venue) != expected[venue]:
        raise DemoBlocked('Unsupported demo endpoint')
    return expected[venue]


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


def _permit(confirmed: bool, *, directory: Path | None = None, opening: bool = False) -> None:
    if confirmed is not True:
        raise DemoBlocked('Explicit demo order permission is required')
    if os.environ.get('FARB_DRY_RUN', '').strip().lower() not in ('', '0', 'false', 'no', 'off'):
        raise DemoBlocked('Dry-run mode forbids demo orders')
    if os.environ.get('FARB_LIVE', '').lower() not in ('', '0', 'false') or os.environ.get('DCA_LIVE', '').lower() not in ('', '0', 'false'):
        raise DemoBlocked('Live trading flags are incompatible with demo orders')
    if os.environ.get('BYBIT_REQUIRE_TESTNET') is not None:
        raise DemoBlocked('Bybit Testnet and Demo Trading are separate environments')
    if demo_credentials._backend() != 'age':
        raise DemoBlocked('Isolated age demo credentials are required')
    try:
        if directory is None:
            with execution_mode._locked() as state_dir:
                state = execution_mode._read_state(state_dir)
        else:
            state = execution_mode._read_state(directory)
        if state['mode'] != 'demo':
            raise DemoBlocked('Demo mode is required')
        if state['orders_enabled'] is not True:
            raise DemoBlocked('Explicit demo order permission is required')
        if opening and state['openings_enabled'] is not True:
            raise DemoBlocked('New demo openings are disabled')
    except DemoBlocked:
        raise
    except Exception:
        raise DemoBlocked('Demo order permission unavailable') from None


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
        if (params or not isinstance(body, dict) or set(body) not in (expected, expected | {'reduceOnly'}) or
                ('reduceOnly' in body and body['reduceOnly'] is not True)):
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


def _request(venue: str, method: str, params: dict[str, str] | None, body: dict[str, Any] | None, confirmed: bool,
             *, cancel: bool = False) -> dict[str, Any]:
    """Fixed demo-only private endpoints, no redirects, no automatic retries."""
    if cancel:
        expected = {'category', 'symbol', 'orderLinkId'} if venue == 'bybit' else {'instId', 'clOrdId'}
        if venue not in _VENUES or method != 'POST' or params is not None or not isinstance(body, dict) or set(body) != expected:
            raise DemoBlocked('Unsupported demo cancel request')
        symbol = body.get('symbol' if venue == 'bybit' else 'instId')
        client = body.get('orderLinkId' if venue == 'bybit' else 'clOrdId')
        pattern = r'[A-Z][A-Z0-9]{1,14}USDT' if venue == 'bybit' else r'[A-Z][A-Z0-9]{1,14}-USDT-SWAP'
        if (not isinstance(symbol, str) or not re.fullmatch(pattern, symbol) or not isinstance(client, str) or
                not re.fullmatch(r'[A-Za-z0-9]{1,24}', client) or
                (venue == 'bybit' and body.get('category') != 'linear')):
            raise DemoBlocked('Unsupported demo cancel request')
    else:
        _validate_route(venue, method, params, body)
    is_opening = method == 'POST' and not cancel and body is not None and body.get('reduceOnly') is not True
    _permit(confirmed, opening=is_opening)
    if not _PAIR_ACTIVE.get():
        raise DemoBlocked('Private transport requires a journalled demo pair')
    creds = _demo_creds(venue)
    route = _CANCEL_PATHS[venue] if cancel else _PATHS[venue][0 if method == 'POST' else 1]
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
    request = urllib.request.Request(_demo_base(venue) + target, data=serialized.encode() if body is not None else None,
                                     method=method, headers=headers)
    # Hold the same interprocess lock used by mode changes until the request
    # is complete. No production route or credential fallback exists here.
    with execution_mode._locked() as directory:
        try:
            mode = execution_mode._read(directory)
        except Exception:
            raise DemoBlocked('Execution mode unavailable') from None
        _permit(confirmed, directory=directory, opening=is_opening)
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


def _order_fields(venue: str, base: str, side: str, qty: str, client_id: str, *, reduce_only: bool = False) -> dict[str, Any]:
    if venue not in _VENUES or side not in ('buy', 'sell'):
        raise DemoBlocked('Unsupported demo futures leg')
    if type(reduce_only) is not bool:
        raise DemoBlocked('Invalid reduce-only flag')
    qty = format(_quantity(qty), 'f')
    if venue == 'bybit':
        fields: dict[str, Any] = {'category': 'linear', 'symbol': base + 'USDT', 'side': side.title(), 'orderType': 'Market',
                                  'qty': qty, 'orderLinkId': client_id, 'positionIdx': 0}
    else:
        fields = {'instId': base + '-USDT-SWAP', 'tdMode': 'cross', 'side': side, 'posSide': 'net',
                  'ordType': 'market', 'sz': qty, 'clOrdId': client_id}
    if reduce_only:
        fields['reduceOnly'] = True
    return fields


def _create_and_confirm(venue: str, fields: dict[str, Any], confirmed: bool, on_ack) -> dict[str, str]:
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
        reduce_only = order.get('reduceOnly')
        if fields.get('reduceOnly') is True and not (reduce_only is True or reduce_only == 'true'):
            raise ValueError('reduce-only not confirmed')
        if not valid or filled != expected or price <= 0:
            raise ValueError('not fully filled')
    except (KeyError, IndexError, TypeError, ValueError, ReconciliationRequired):
        raise ReconciliationRequired('Order is not confirmed fully filled') from None
    return {'venue': venue, 'order_id': order_id, 'filled_qty': str(filled), 'avg_price': str(price)}


def _preflight(pair: FuturesPair, *, risk_limits=None) -> None:
    """Validate the mandatory technical requirements before opening."""
    if risk_limits is None:
        raise DemoBlocked('Demo preflight requires explicit technical limits')
    from execution import demo_futures_data
    from execution.demo_futures_preflight import RiskLimits, TechnicalLimits
    if not isinstance(risk_limits, (RiskLimits, TechnicalLimits)):
        raise DemoBlocked('Demo preflight risk limits are invalid')
    try:
        demo_futures_data.verify_technical_open(pair, max_age_ms=risk_limits.max_snapshot_age_ms)
    except demo_futures_data.DemoDataBlocked:
        raise DemoBlocked('Demo preflight could not validate current evidence') from None


def _journal_path() -> Path:
    return execution_mode._directory() / 'demo-futures-intent.json'


def _reconcile_step(pair: FuturesPair, legs: list[dict[str, Any]], confirmed_count: int, risk_limits) -> None:
    """Check exchange executions and both positions before another leg."""
    from execution import demo_futures_data
    from execution.demo_futures_preflight import RiskLimits, TechnicalLimits
    if not isinstance(risk_limits, (RiskLimits, TechnicalLimits)):
        raise ReconciliationRequired('Explicit reconciliation limits required')
    try:
        demo_futures_data.verify_step(pair, legs, confirmed_count, max_age_ms=risk_limits.max_snapshot_age_ms,
                                      require_fills=False)
    except demo_futures_data.DemoDataBlocked:
        raise ReconciliationRequired('Demo position or execution state is uncertain') from None


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


def _archive_confirmed_closed_pair(path: Path, risk_limits) -> None:
    """Preserve a confirmed journal after rechecking both venues are flat."""
    from execution import demo_futures_data
    pair, record = _read_open_journal_for_close(path, allowed_states=frozenset({'closed_confirmed'}))
    try:
        demo_futures_data.verify_close_step(pair, record['legs'], record['closing'], 2,
                                            max_age_ms=risk_limits.max_snapshot_age_ms, require_fills=False)
        archive = path.parent / ('demo-futures-closed-' + record['id'] + '.json')
        os.link(path, archive, follow_symlinks=False)
        os.unlink(path)
        if os.name != 'nt':
            fd = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    except Exception:
        raise DemoBlocked('Closed pair cannot be safely archived; manual reconciliation required') from None


def execute_pair(pair: FuturesPair, *, confirmed: bool = False, risk_limits=None) -> dict[str, Any]:
    """Sequential two-futures execution with a durable journal.

    Some offline tests replace the blocking preflight and mock all exchange I/O.
    Bounded read-only reconciliation is attempted after each confirmed fill;
    there is no automatic retry, compensation or journal clearance.
    """
    pair.validate()
    _permit(confirmed, opening=True)
    if execution_mode.current_mode() != 'demo':
        raise DemoBlocked('Demo mode is required')
    path = _journal_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        if not lock_exclusive(fd, blocking=False):
            raise DemoBlocked('Another demo pair is in progress')
        if path.exists() or path.is_symlink():
            if risk_limits is None:
                raise DemoBlocked('Existing demo intent requires manual reconciliation')
            try:
                _archive_confirmed_closed_pair(path, risk_limits)
            except DemoBlocked:
                raise DemoBlocked('Existing demo intent requires manual reconciliation') from None
        if risk_limits is None:
            _preflight(pair)
        else:
            _preflight(pair, risk_limits=risk_limits)
        position_id = uuid.uuid4().hex
        long_id, short_id = uuid.uuid4().hex[:24], uuid.uuid4().hex[:24]
        if long_id == short_id:
            raise DemoBlocked('Duplicate client order identifiers')
        def qty_for(venue: str) -> str:
            return pair.base_quantity if venue == 'bybit' else pair.okx_contracts
        legs = [_order_fields(pair.long_venue, pair.base, 'buy', qty_for(pair.long_venue), long_id),
                _order_fields(pair.short_venue, pair.base, 'sell', qty_for(pair.short_venue), short_id)]
        record: dict[str, Any] = {'version': 1, 'id': position_id, 'state': 'prepared',
                                  'base': pair.base, 'okx_contract_size': pair.okx_contract_size,
                                  'legs': [{'venue': pair.long_venue, 'request': legs[0]},
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
                    record['uncertain_leg'] = index
                    record['state'] = 'reconciliation_required'
                    _write_journal(path, record)
                    return {'state': 'reconciliation_required', 'id': position_id, 'confirmed_legs': index}
                record['legs'][index]['confirmation'] = result
                record['state'] = f'leg_{index + 1}_filled'
                _write_journal(path, record)
                try:
                    _reconcile_step(pair, record['legs'], index + 1, risk_limits)
                except Exception:
                    record['uncertain_leg'] = index
                    record['state'] = 'reconciliation_required'
                    _write_journal(path, record)
                    return {'state': 'reconciliation_required', 'id': position_id, 'confirmed_legs': index + 1}
                record['state'] = f'leg_{index + 1}_reconciled'
                _write_journal(path, record)
            record['state'] = 'open_confirmed'
            _write_journal(path, record)
            return {'state': record['state'], 'id': position_id, 'confirmed_legs': 2}
        finally:
            _PAIR_ACTIVE.reset(token)
    finally:
        unlock(fd)
        os.close(fd)


def reconcile_pending(*, confirmed: bool = False, risk_limits=None) -> dict[str, Any]:
    """Read-only post-restart check; never retries, clears a journal or closes.

    A crash before an order confirmation remains manual-only even when bounded
    lookup succeeds. A matching snapshot never authorizes a subsequent order.
    """
    import stat
    from execution import demo_futures_data
    from execution.demo_futures_preflight import RiskLimits, TechnicalLimits

    _permit(confirmed)
    if not isinstance(risk_limits, (RiskLimits, TechnicalLimits)):
        raise DemoBlocked('Explicit reconciliation limits required')
    if execution_mode.current_mode() != 'demo':
        raise DemoBlocked('Demo mode is required')
    path = _journal_path()
    if not path.parent.is_dir() or path.is_symlink():
        raise DemoBlocked('Pending journal unavailable')
    lock_fd = os.open(path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    locked = False
    try:
        locked = lock_exclusive(lock_fd, blocking=False)
        if not locked:
            raise DemoBlocked('Another demo operation is in progress')
        if path.is_symlink():
            raise DemoBlocked('Pending journal unavailable')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as file:
            info = os.fstat(file.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_size > 1_048_576 or
                    (os.name != 'nt' and (info.st_uid != os.geteuid() or info.st_mode & 0o077))):
                raise DemoBlocked('Pending journal unavailable')
            raw = file.read(1_048_577)
        if len(raw) > 1_048_576:
            raise DemoBlocked('Pending journal unavailable')
        record = json.loads(raw)
        states = {'prepared', 'leg_1_submitting', 'leg_1_acknowledged', 'leg_1_filled', 'leg_1_reconciled',
                  'leg_2_submitting', 'leg_2_acknowledged', 'leg_2_filled', 'leg_2_reconciled',
                  'reconciliation_required', 'position_reconciliation_required', 'open_confirmed', 'close_prepared',
                  'close_1_submitting', 'close_1_acknowledged', 'close_1_filled', 'close_1_reconciled',
                  'close_2_submitting', 'close_2_acknowledged', 'close_2_filled', 'close_2_reconciled',
                  'close_reconciliation_required', 'closed_observed_manual_review', 'closed_confirmed',
                  'cancel_submitting', 'cancel_reconciliation_required', 'manually_resolved_flat'}
        if (not isinstance(record, dict) or type(record.get('version')) is not int or record['version'] != 1 or
                record.get('state') not in states or not isinstance(record.get('id'), str) or
                not re.fullmatch(r'[a-f0-9]{32}', record['id']) or not isinstance(record.get('legs'), list) or
                len(record['legs']) != 2):
            raise DemoBlocked('Pending journal invalid')
        if record['state'].startswith('cancel_') or record['state'] == 'manually_resolved_flat':
            return {'state': 'manual_reconciliation_required', 'confirmed_legs': 0,
                    'confirmed_leg_evidence_checked': False, 'reason': 'cancel_requires_manual_reconciliation'}
        if record['state'].startswith(('close_', 'closed_')):
            return _reconcile_closing(path, record, risk_limits)
        legs = record['legs']
        if any(not isinstance(leg, dict) or leg.get('venue') not in _VENUES or not isinstance(leg.get('request'), dict) for leg in legs):
            raise DemoBlocked('Pending journal invalid')
        if {legs[0]['venue'], legs[1]['venue']} != _VENUES:
            raise DemoBlocked('Pending journal invalid')
        for index, leg in enumerate(legs):
            fields = leg['request']
            _validate_route(leg['venue'], 'POST', None, fields)
            if 'reduceOnly' in fields or fields['side'].lower() != ('buy' if index == 0 else 'sell'):
                raise DemoBlocked('Pending journal invalid')
        bybit_leg = next(leg for leg in legs if leg['venue'] == 'bybit')
        okx_leg = next(leg for leg in legs if leg['venue'] == 'okx')
        if bybit_leg['request']['orderLinkId'] == okx_leg['request']['clOrdId']:
            raise DemoBlocked('Pending journal invalid')
        pair = FuturesPair(record['base'], legs[0]['venue'], legs[1]['venue'], bybit_leg['request']['qty'],
                           okx_leg['request']['sz'], record['okx_contract_size'])
        pair.validate()
        if (bybit_leg['request']['symbol'] != pair.base + 'USDT' or
                okx_leg['request']['instId'] != pair.base + '-USDT-SWAP'):
            raise DemoBlocked('Pending journal invalid')
        count = 0
        for index, leg in enumerate(legs):
            confirmation = leg.get('confirmation')
            if confirmation is None:
                state = record['state']
                potentially_submitted = (
                    (index == 0 and state in ('leg_1_submitting', 'leg_1_acknowledged', 'reconciliation_required')) or
                    (index == 1 and state in ('leg_2_submitting', 'leg_2_acknowledged', 'reconciliation_required'))
                )
                if not potentially_submitted:
                    break
                known_id = leg.get('order_id')
                if known_id is not None and (not isinstance(known_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', known_id)):
                    raise DemoBlocked('Pending journal invalid')
                try:
                    confirmation = demo_futures_data.lookup_confirmation(
                        leg['venue'], pair.base, leg['request'], order_id=known_id,
                        max_age_ms=risk_limits.max_snapshot_age_ms)
                except demo_futures_data.DemoDataBlocked:
                    break  # No inference from a missing or ambiguous lookup.
                # Only the in-memory copy is enriched; the durable intent is
                # unchanged and continues to block all future entries.
                leg['confirmation'] = confirmation
                leg['order_id'] = confirmation['order_id']
            if (not isinstance(confirmation, dict) or confirmation.get('venue') != leg['venue'] or
                    confirmation.get('order_id') != leg.get('order_id') or
                    not isinstance(leg.get('order_id'), str) or
                    not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', leg['order_id']) or
                    _quantity(confirmation.get('filled_qty')) != _quantity(leg['request']['qty' if leg['venue'] == 'bybit' else 'sz']) or
                    _quantity(confirmation.get('avg_price')) <= 0):
                raise DemoBlocked('Pending journal invalid')
            count += 1
        if count < 2 and legs[1].get('confirmation') is not None:
            raise DemoBlocked('Pending journal invalid')
        if count == 0:
            return {'state': 'manual_reconciliation_required', 'confirmed_legs': 0, 'confirmed_leg_evidence_checked': False}
        try:
            demo_futures_data.verify_step(pair, legs, count, max_age_ms=risk_limits.max_snapshot_age_ms)
        except demo_futures_data.DemoDataBlocked:
            return {'state': 'manual_reconciliation_required', 'confirmed_legs': count, 'confirmed_leg_evidence_checked': False}
        # Even matching bounded reads cannot clear the journal or guarantee
        # complete history, safe close, or a synchronized order opt-in.
        return {'state': 'manual_reconciliation_required', 'confirmed_legs': count, 'confirmed_leg_evidence_checked': True}
    except DemoBlocked:
        raise
    except Exception:
        raise DemoBlocked('Pending journal unavailable or invalid') from None
    finally:
        if locked:
            unlock(lock_fd)
        os.close(lock_fd)


def _read_open_journal_for_close(path: Path, *, allowed_states: frozenset[str] = frozenset({'position_reconciliation_required', 'open_confirmed'})) -> tuple[FuturesPair, dict[str, Any]]:
    """Load only a fully confirmed opening journal; reject old/ambiguous data."""
    import stat
    try:
        if path.is_symlink():
            raise ValueError('symlink')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as file:
            info = os.fstat(file.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_size > 1_048_576 or
                    (os.name != 'nt' and (info.st_uid != os.geteuid() or info.st_mode & 0o077))):
                raise ValueError('unsafe journal')
            raw = file.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ValueError('unsafe journal')
        record = json.loads(raw)
        if (not isinstance(record, dict) or type(record.get('version')) is not int or record['version'] != 1 or
                record.get('state') not in allowed_states or not isinstance(record.get('id'), str) or
                not re.fullmatch(r'[a-f0-9]{32}', record['id']) or not isinstance(record.get('legs'), list) or
                len(record['legs']) != 2):
            raise ValueError('unconfirmed opening')
        legs = record['legs']
        if any(not isinstance(leg, dict) or leg.get('venue') not in _VENUES or not isinstance(leg.get('request'), dict) for leg in legs):
            raise ValueError('invalid legs')
        if {legs[0]['venue'], legs[1]['venue']} != _VENUES:
            raise ValueError('invalid venues')
        for index, leg in enumerate(legs):
            fields, confirmation = leg['request'], leg.get('confirmation')
            _validate_route(leg['venue'], 'POST', None, fields)
            if (fields.get('reduceOnly') is not None or fields['side'].lower() != ('buy' if index == 0 else 'sell') or
                    not isinstance(confirmation, dict) or confirmation.get('venue') != leg['venue'] or
                    confirmation.get('order_id') != leg.get('order_id') or not isinstance(leg.get('order_id'), str) or
                    not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', leg['order_id']) or
                    _quantity(confirmation.get('filled_qty')) != _quantity(fields['qty' if leg['venue'] == 'bybit' else 'sz']) or
                    _quantity(confirmation.get('avg_price')) <= 0):
                raise ValueError('opening confirmation mismatch')
        bybit = next(leg for leg in legs if leg['venue'] == 'bybit')
        okx = next(leg for leg in legs if leg['venue'] == 'okx')
        if bybit['request']['orderLinkId'] == okx['request']['clOrdId']:
            raise ValueError('duplicate client ids')
        pair = FuturesPair(record['base'], legs[0]['venue'], legs[1]['venue'], bybit['request']['qty'],
                           okx['request']['sz'], record['okx_contract_size'])
        pair.validate()
        if (bybit['request']['symbol'] != pair.base + 'USDT' or
                okx['request']['instId'] != pair.base + '-USDT-SWAP'):
            raise ValueError('instrument mismatch')
        return pair, record
    except Exception:
        raise DemoBlocked('Opening journal unavailable, ambiguous or not fully confirmed') from None


def _reconcile_closing(path: Path, record: dict[str, Any], risk_limits) -> dict[str, Any]:
    """Read-only recovery of a closing intent; never retry or clear the journal."""
    from execution import demo_futures_data
    state = record['state']
    result = {'state': 'manual_reconciliation_required', 'confirmed_legs': 0,
              'confirmed_leg_evidence_checked': False, 'confirmed_closes': 0,
              'close_evidence_checked': False, 'reason': 'order_unconfirmed'}
    try:
        pair, saved = _read_open_journal_for_close(path, allowed_states=frozenset({state}))
        if saved != record:
            raise ValueError('Journal changed')
        closing = saved['closing']
        if not isinstance(closing, list) or len(closing) != 2:
            raise ValueError('Closing intent missing')
        client_ids = {leg['request']['orderLinkId' if leg['venue'] == 'bybit' else 'clOrdId']
                      for leg in saved['legs']}
        for index, original in enumerate(reversed(saved['legs'])):
            leg = closing[index]
            venue = original['venue']
            if not isinstance(leg, dict) or leg.get('venue') != venue:
                raise ValueError('Closing venue mismatch')
            fields = leg['request']
            _validate_route(venue, 'POST', None, fields)
            symbol = 'symbol' if venue == 'bybit' else 'instId'
            amount = 'qty' if venue == 'bybit' else 'sz'
            client = fields['orderLinkId' if venue == 'bybit' else 'clOrdId']
            if (fields.get('reduceOnly') is not True or fields[symbol] != original['request'][symbol] or
                    fields['side'].lower() == original['request']['side'].lower() or
                    _quantity(fields[amount]) != _quantity(original['request'][amount]) or client in client_ids):
                raise ValueError('Closing request mismatch')
            client_ids.add(client)
            known = leg.get('order_id')
            if known is not None and (not isinstance(known, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', known)):
                raise ValueError('Invalid exchange identifier')
            confirmation = leg.get('confirmation')
            if confirmation is not None and (not isinstance(confirmation, dict) or known is None or
                                             confirmation.get('order_id') != known):
                raise ValueError('Invalid recorded confirmation')
        if state == 'close_prepared':
            if any('order_id' in leg or 'confirmation' in leg for leg in closing):
                raise ValueError('Unsubmitted close contains acknowledgement')
            result['reason'] = 'close_not_submitted'
            return result
        if state.startswith('close_1_'):
            if 'order_id' in closing[1] or 'confirmation' in closing[1]:
                raise ValueError('Second close could not have been submitted')
            possible = 1
        elif state.startswith('close_2_') or state in ('closed_observed_manual_review', 'closed_confirmed'):
            if closing[0].get('confirmation') is None:
                raise ValueError('First close was not confirmed')
            possible = 2
        else:
            possible = 2 if closing[0].get('confirmation') is not None else 1
        for leg in closing[:possible]:
            try:
                observed = demo_futures_data.lookup_confirmation(
                    leg['venue'], pair.base, leg['request'], order_id=leg.get('order_id'),
                    max_age_ms=risk_limits.max_snapshot_age_ms)
            except demo_futures_data.DemoDataBlocked:
                break
            prior = leg.get('confirmation')
            if prior is not None and (prior.get('venue') != observed['venue'] or
                                      _quantity(prior.get('filled_qty')) != _quantity(observed['filled_qty']) or
                                      _quantity(prior.get('avg_price')) != _quantity(observed['avg_price'])):
                raise ValueError('Conflicting confirmations')
            leg['order_id'] = observed['order_id']
            leg['confirmation'] = observed
            result['confirmed_closes'] += 1
        if result['confirmed_closes']:
            try:
                demo_futures_data.verify_close_step(pair, saved['legs'], closing, result['confirmed_closes'],
                                                   max_age_ms=risk_limits.max_snapshot_age_ms)
                result['close_evidence_checked'] = True
                result['reason'] = ('read_only_close_evidence_consistent' if result['confirmed_closes'] == possible
                                    else 'order_unconfirmed')
            except demo_futures_data.DemoDataBlocked:
                result['reason'] = 'positions_or_executions_unconfirmed'
        return result
    except Exception:
        raise DemoBlocked('Closing journal unavailable or invalid') from None


def _preflight_close(pair: FuturesPair, opening_legs: list[dict[str, Any]],
                     closing_legs: list[dict[str, Any]], *, risk_limits) -> None:
    """Validate open positions and exact close requests; keep execution disabled."""
    from execution import demo_futures_data
    from execution.demo_futures_preflight import RiskLimits, TechnicalLimits

    if not isinstance(risk_limits, (RiskLimits, TechnicalLimits)):
        raise DemoBlocked('Explicit close risk limits required')
    try:
        positions_at = demo_futures_data.verify_step(pair, opening_legs, 2, max_age_ms=risk_limits.max_snapshot_age_ms,
                                                      require_fills=False)
        markets_at = demo_futures_data.verify_close_instruments(
            pair, opening_legs, closing_legs, max_age_ms=risk_limits.max_snapshot_age_ms,
            max_slippage_rate='0', check_liquidity=False)
        now = int(time.time() * 1000)
        if any(type(stamp) is not int or stamp > now or now - stamp > risk_limits.max_snapshot_age_ms
               for stamp in (positions_at, markets_at)):
            raise demo_futures_data.DemoDataBlocked('Close preflight evidence expired')
    except demo_futures_data.DemoDataBlocked:
        raise DemoBlocked('Demo close preflight could not verify positions or orders') from None


def close_pair(*, confirmed: bool = False, close_confirmed: bool = False, risk_limits=None) -> dict[str, Any]:
    """Sequential, confirmed reduce-only close; never retry an unknown result."""
    from execution import demo_futures_data
    from execution.demo_futures_preflight import RiskLimits, TechnicalLimits

    _permit(confirmed)
    if close_confirmed is not True or not isinstance(risk_limits, (RiskLimits, TechnicalLimits)):
        raise DemoBlocked('Explicit close confirmation and risk limits are required')
    if execution_mode.current_mode() != 'demo':
        raise DemoBlocked('Demo mode is required')
    path = _journal_path()
    if not path.parent.is_dir():
        raise DemoBlocked('Opening journal unavailable')
    fd = os.open(path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    locked = False
    try:
        locked = lock_exclusive(fd, blocking=False)
        if not locked:
            raise DemoBlocked('Another demo operation is in progress')
        pair, record = _read_open_journal_for_close(path)
        original_ids = {leg['request']['orderLinkId' if leg['venue'] == 'bybit' else 'clOrdId'] for leg in record['legs']}
        close_ids = (uuid.uuid4().hex[:24], uuid.uuid4().hex[:24])
        if len(set(close_ids) | original_ids) != 4:
            raise DemoBlocked('Duplicate client order identifiers')
        # Close the recorded short first, then long; both are reduce-only.
        closing = []
        for index, client_id in ((1, close_ids[0]), (0, close_ids[1])):
            leg = record['legs'][index]
            qty = leg['request']['qty' if leg['venue'] == 'bybit' else 'sz']
            closing.append({'venue': leg['venue'], 'request': _order_fields(
                leg['venue'], pair.base, 'buy' if index == 1 else 'sell', qty, client_id, reduce_only=True)})
        _preflight_close(pair, record['legs'], closing, risk_limits=risk_limits)
        record['closing'] = closing
        record['state'] = 'close_prepared'
        _write_journal(path, record)
        token = _PAIR_ACTIVE.set(True)
        try:
            for index, leg in enumerate(closing):
                record['state'] = f'close_{index + 1}_submitting'
                _write_journal(path, record)
                confirmed_closes = index
                try:
                    def save_ack(order_id: str) -> None:
                        leg['order_id'] = order_id
                        record['state'] = f'close_{index + 1}_acknowledged'
                        _write_journal(path, record)
                    result = _create_and_confirm(leg['venue'], leg['request'], confirmed, save_ack)
                    leg['confirmation'] = result
                    confirmed_closes = index + 1
                    record['state'] = f'close_{index + 1}_filled'
                    _write_journal(path, record)
                    demo_futures_data.verify_close_step(pair, record['legs'], closing, index + 1,
                                                        max_age_ms=risk_limits.max_snapshot_age_ms, require_fills=False)
                except Exception:
                    record['uncertain_close'] = index
                    record['state'] = 'close_reconciliation_required'
                    _write_journal(path, record)
                    return {'state': 'close_reconciliation_required', 'confirmed_closes': confirmed_closes}
                record['state'] = f'close_{index + 1}_reconciled'
                _write_journal(path, record)
            record['state'] = 'closed_confirmed'
            _write_journal(path, record)
            return {'state': record['state'], 'confirmed_closes': 2}
        finally:
            _PAIR_ACTIVE.reset(token)
    finally:
        if locked:
            unlock(fd)
        os.close(fd)


def cancel_pending(venue: str, client_id: str, *, confirmed: bool = False) -> dict[str, Any]:
    """Cancel one journalled uncertain order once, then require reconciliation."""
    import stat

    _permit(confirmed)
    if (venue not in _VENUES or not isinstance(client_id, str) or
            not re.fullmatch(r'[A-Za-z0-9]{1,24}', client_id)):
        raise DemoBlocked('Invalid cancellation identity')
    path = _journal_path()
    if not path.parent.is_dir():
        raise DemoBlocked('Pending journal unavailable')
    lock_fd = os.open(path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    locked = False
    try:
        locked = lock_exclusive(lock_fd, blocking=False)
        if not locked:
            raise DemoBlocked('Another demo operation is in progress')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as file:
            info = os.fstat(file.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_size > 1_048_576 or
                    (os.name != 'nt' and (info.st_uid != os.geteuid() or info.st_mode & 0o077))):
                raise DemoBlocked('Pending journal unavailable')
            record = json.loads(file.read(1_048_577))
        if (not isinstance(record, dict) or type(record.get('version')) is not int or record['version'] != 1 or
                not isinstance(record.get('id'), str) or not re.fullmatch(r'[a-f0-9]{32}', record['id'])):
            raise DemoBlocked('Pending journal invalid')
        state = record.get('state')
        allowed = {'leg_1_submitting', 'leg_1_acknowledged', 'leg_2_submitting', 'leg_2_acknowledged',
                   'reconciliation_required', 'close_1_submitting', 'close_1_acknowledged',
                   'close_2_submitting', 'close_2_acknowledged', 'close_reconciliation_required'}
        if state not in allowed:
            raise DemoBlocked('No cancellable uncertain order in journal')
        entries = record.get('closing') if state.startswith('close_') else record.get('legs')
        if not isinstance(entries, list) or len(entries) != 2:
            raise DemoBlocked('Pending journal invalid')
        if state.startswith(('leg_1_', 'close_1_')):
            index = 0
        elif state.startswith(('leg_2_', 'close_2_')):
            index = 1
        else:
            index = record.get('uncertain_close' if state.startswith('close_') else 'uncertain_leg')
            if type(index) is not int or index not in (0, 1):
                raise DemoBlocked('Uncertain order identity requires manual review')
        leg = entries[index]
        if not isinstance(leg, dict) or leg.get('venue') != venue or not isinstance(leg.get('request'), dict):
            raise DemoBlocked('Cancellation does not match uncertain order')
        fields = leg['request']
        _validate_route(venue, 'POST', None, fields)
        if fields['orderLinkId' if venue == 'bybit' else 'clOrdId'] != client_id:
            raise DemoBlocked('Cancellation does not match uncertain order')
        body = ({'category': 'linear', 'symbol': fields['symbol'], 'orderLinkId': client_id} if venue == 'bybit'
                else {'instId': fields['instId'], 'clOrdId': client_id})
        record['cancellation'] = {'venue': venue, 'request': body, 'previous_state': state}
        record['state'] = 'cancel_submitting'
        _write_journal(path, record)
        token = _PAIR_ACTIVE.set(True)
        try:
            try:
                _request(venue, 'POST', None, body, confirmed, cancel=True)
            except Exception:
                pass  # A timeout or rejection is never followed by a retry.
        finally:
            _PAIR_ACTIVE.reset(token)
        record['state'] = 'cancel_reconciliation_required'
        _write_journal(path, record)
        return {'state': 'cancel_reconciliation_required', 'venue': venue, 'client_id': client_id}
    except DemoBlocked:
        raise
    except Exception:
        raise DemoBlocked('Pending journal unavailable or invalid') from None
    finally:
        if locked:
            unlock(lock_fd)
        os.close(lock_fd)


def resolve_pending_flat(*, confirmed: bool = False, max_age_ms: int = 30_000) -> dict[str, str]:
    """Owner-confirmed resolution only after both venues report flat and no orders."""
    import stat
    from execution import demo_futures_data

    _permit(confirmed)
    path = _journal_path()
    if not path.parent.is_dir():
        raise DemoBlocked('Pending journal unavailable')
    lock_fd = os.open(path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    locked = False
    try:
        locked = lock_exclusive(lock_fd, blocking=False)
        if not locked:
            raise DemoBlocked('Another demo operation is in progress')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as file:
            info = os.fstat(file.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_size > 1_048_576 or
                    (os.name != 'nt' and (info.st_uid != os.geteuid() or info.st_mode & 0o077))):
                raise DemoBlocked('Pending journal unavailable')
            record = json.loads(file.read(1_048_577))
        if (not isinstance(record, dict) or type(record.get('version')) is not int or record['version'] != 1 or
                not isinstance(record.get('id'), str) or not re.fullmatch(r'[a-f0-9]{32}', record['id']) or
                not isinstance(record.get('legs'), list) or len(record['legs']) != 2):
            raise DemoBlocked('Pending journal invalid')
        legs = record['legs']
        if {leg['venue'] for leg in legs} != _VENUES:
            raise DemoBlocked('Pending journal invalid')
        for index, leg in enumerate(legs):
            fields = leg['request']
            _validate_route(leg['venue'], 'POST', None, fields)
            if 'reduceOnly' in fields or fields['side'].lower() != ('buy' if index == 0 else 'sell'):
                raise DemoBlocked('Pending journal invalid')
        bybit = next(leg for leg in legs if leg['venue'] == 'bybit')['request']
        okx = next(leg for leg in legs if leg['venue'] == 'okx')['request']
        pair = FuturesPair(record['base'], legs[0]['venue'], legs[1]['venue'], bybit['qty'], okx['sz'],
                           record['okx_contract_size'])
        pair.validate()
        if (bybit['symbol'] != pair.base + 'USDT' or okx['instId'] != pair.base + '-USDT-SWAP' or
                bybit['orderLinkId'] == okx['clOrdId']):
            raise DemoBlocked('Pending journal invalid')
        state = record.get('state')
        if state == 'manually_resolved_flat':
            state = record.get('resolution_from')
        if state in ('cancel_submitting', 'cancel_reconciliation_required'):
            state = record.get('cancellation', {}).get('previous_state')
        targets = []
        if state == 'prepared':
            pass
        elif state in ('open_confirmed', 'position_reconciliation_required', 'close_prepared'):
            targets = legs
        elif state in ('closed_confirmed', 'closed_observed_manual_review'):
            closing = record.get('closing')
            if not isinstance(closing, list) or len(closing) != 2:
                raise DemoBlocked('Pending journal invalid')
            targets = legs + closing
        elif isinstance(state, str) and state.startswith(('leg_1_', 'leg_2_')):
            targets = legs[:1 if state.startswith('leg_1_') else 2]
        elif state == 'reconciliation_required':
            index = record.get('uncertain_leg')
            if type(index) is not int or index not in (0, 1):
                raise DemoBlocked('Uncertain order identity requires manual review')
            targets = legs[:index + 1]
        elif isinstance(state, str) and state.startswith(('close_1_', 'close_2_')):
            closing = record.get('closing')
            if not isinstance(closing, list) or len(closing) != 2:
                raise DemoBlocked('Pending journal invalid')
            targets = closing[:1 if state.startswith('close_1_') else 2]
        elif state == 'close_reconciliation_required':
            closing, index = record.get('closing'), record.get('uncertain_close')
            if not isinstance(closing, list) or len(closing) != 2 or type(index) is not int or index not in (0, 1):
                raise DemoBlocked('Uncertain close identity requires manual review')
            targets = closing[:index + 1]
        else:
            raise DemoBlocked('Pending journal state requires manual review')
        for leg in targets:
            demo_futures_data.verify_terminal_order(leg['venue'], pair.base, leg['request'], max_age_ms=max_age_ms)
        demo_futures_data.verify_flat(pair, max_age_ms=max_age_ms)
        if record.get('state') != 'manually_resolved_flat':
            record['resolution_from'] = record.get('state')
        record['state'] = 'manually_resolved_flat'
        _write_journal(path, record)
        archive = path.parent / ('demo-futures-resolved-' + record['id'] + '.json')
        try:
            os.link(path, archive, follow_symlinks=False)
        except FileExistsError:
            if not os.path.samefile(path, archive):
                raise
        os.unlink(path)
        if os.name != 'nt':
            fd = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return {'state': 'manually_resolved_flat', 'id': record['id']}
    except DemoBlocked:
        raise
    except Exception:
        raise DemoBlocked('Manual resolution requires confirmed flat demo accounts') from None
    finally:
        if locked:
            unlock(lock_fd)
        os.close(lock_fd)
