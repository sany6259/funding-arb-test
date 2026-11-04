"""Strict, read-only demo futures data acquisition. Never sends orders.

The callers must supply explicit risk limits. Missing or ambiguous exchange
fields fail closed; this module is not a trade permission or an E2E verifier.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext
from typing import Any

from core import execution_mode
from execution import demo_futures as demo
from execution.demo_futures_preflight import VenueSnapshot


class DemoDataBlocked(RuntimeError):
    """Read-only evidence is missing, malformed or unavailable."""


@dataclass(frozen=True)
class ReadResult:
    payload: dict[str, Any]
    started_ms: int


def _resources(venue: str, base: str) -> dict[str, tuple[str, dict[str, str]]]:
    if not isinstance(base, str) or not re.fullmatch(r'[A-Z][A-Z0-9]{1,14}', base):
        raise DemoDataBlocked('Invalid underlying')
    symbol = base + 'USDT'
    swap = base + '-USDT-SWAP'
    if venue == 'bybit':
        return {
            'instrument': ('/v5/market/instruments-info', {'category': 'linear', 'symbol': symbol}),
            'ticker': ('/v5/market/tickers', {'category': 'linear', 'symbol': symbol}),
            'book': ('/v5/market/orderbook', {'category': 'linear', 'symbol': symbol, 'limit': '50'}),
            'position': ('/v5/position/list', {'category': 'linear', 'symbol': symbol}),
            'wallet': ('/v5/account/wallet-balance', {'accountType': 'UNIFIED', 'coin': 'USDT'}),
            'orders': ('/v5/order/realtime', {'category': 'linear', 'symbol': symbol, 'openOnly': '0', 'limit': '50'}),
            'fees': ('/v5/account/fee-rate', {'category': 'linear', 'symbol': symbol}),
            'account': ('/v5/account/info', {}),
            'fills': ('/v5/execution/list', {'category': 'linear', 'symbol': symbol, 'limit': '100'}),
        }
    if venue == 'okx':
        return {
            'instrument': ('/api/v5/public/instruments', {'instType': 'SWAP', 'instId': swap}),
            'mark': ('/api/v5/public/mark-price', {'instType': 'SWAP', 'instId': swap}),
            'funding': ('/api/v5/public/funding-rate', {'instId': swap}),
            'book': ('/api/v5/market/books', {'instId': swap, 'sz': '50'}),
            'account': ('/api/v5/account/config', {}),
            'wallet': ('/api/v5/account/balance', {'ccy': 'USDT'}),
            'position': ('/api/v5/account/positions', {'instType': 'SWAP', 'instId': swap}),
            'orders': ('/api/v5/trade/orders-pending', {'instType': 'SWAP', 'instId': swap}),
            'fees': ('/api/v5/account/trade-fee', {'instType': 'SWAP', 'instFamily': base + '-USDT'}),
            'leverage': ('/api/v5/account/leverage-info', {'instId': swap, 'mgnMode': 'cross'}),
            'fills': ('/api/v5/trade/fills', {'instType': 'SWAP', 'instId': swap, 'limit': '100'}),
        }
    raise DemoDataBlocked('Unsupported venue')


def read(venue: str, base: str, resource: str) -> ReadResult:
    """Fixed allowlisted HTTPS GET with isolated credentials and mode lock."""
    routes = _resources(venue, base)
    if resource not in routes:
        raise DemoDataBlocked('Unsupported read-only resource')
    try:
        started_ms = int(time.time() * 1000)
        demo._permit(True)
        creds = demo._demo_creds(venue)
        path, params = routes[resource]
        query = urllib.parse.urlencode(params)
        target = path + ('?' + query if query else '')
        if venue == 'bybit':
            timestamp = str(int(time.time() * 1000))
            sig = hmac.new(creds['secret'].encode(), (timestamp + creds['key'] + '5000' + query).encode(), hashlib.sha256).hexdigest()
            headers = {'X-BAPI-API-KEY': creds['key'], 'X-BAPI-SIGN': sig,
                       'X-BAPI-TIMESTAMP': timestamp, 'X-BAPI-RECV-WINDOW': '5000'}
        else:
            timestamp = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
            sig = base64.b64encode(hmac.new(creds['secret'].encode(), (timestamp + 'GET' + target).encode(), hashlib.sha256).digest()).decode()
            headers = {'OK-ACCESS-KEY': creds['key'], 'OK-ACCESS-SIGN': sig,
                       'OK-ACCESS-TIMESTAMP': timestamp, 'OK-ACCESS-PASSPHRASE': creds['passphrase'],
                       'x-simulated-trading': '1'}
        request = urllib.request.Request(demo._BASES[venue] + target, method='GET', headers=headers)
        with execution_mode._locked() as directory:
            demo._permit(True)
            if execution_mode._read(directory) != 'demo':
                raise DemoDataBlocked('Demo mode required')
            with urllib.request.build_opener(demo._NoRedirect()).open(request, timeout=10) as response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError('Response too large')
                result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError('Invalid response')
        if (venue == 'bybit' and result.get('retCode') != 0) or (venue == 'okx' and result.get('code') != '0'):
            raise ValueError('Exchange rejected request')
        return ReadResult(result, started_ms)
    except Exception:
        # Never return remote payload, URLs with credentials, or exception text.
        raise DemoDataBlocked('Demo data unavailable or invalid') from None


def _num(value: object, *, positive: bool = False) -> Decimal:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError('Invalid decimal')
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError('Invalid decimal') from None
    if (not result.is_finite() or result.adjusted() > 12 or result.adjusted() < -12 or
            len(result.as_tuple().digits) > 24 or abs(result.as_tuple().exponent) > 24 or
            (positive and result <= 0)):
        raise ValueError('Invalid decimal')
    return result


def _stamp(value: object) -> int:
    if not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value).isdigit():
        raise ValueError('Invalid timestamp')
    number = int(value)
    if number <= 0 or number > 10**15:
        raise ValueError('Invalid timestamp')
    return number


def _one(rows: object, field: str, expected: str) -> dict[str, Any]:
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict) or rows[0].get(field) != expected:
        raise ValueError('Ambiguous instrument response')
    return rows[0]


def _one_scoped(rows: object, field: str, expected: str) -> dict[str, Any]:
    # Some OKX book responses omit instId; the fixed request already scopes it.
    if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict) or
            (field in rows[0] and rows[0][field] != expected)):
        raise ValueError('Ambiguous scoped response')
    return rows[0]


def _book_slippage(levels: object, wanted_base: Decimal, contract_size: Decimal, mark: Decimal, side: str) -> str:
    if not isinstance(levels, list) or not levels:
        raise ValueError('Missing order book')
    with localcontext() as ctx:
        ctx.prec = 80
        remaining, value, previous = wanted_base, Decimal(0), None
        for level in levels:
            if not isinstance(level, list) or len(level) < 2:
                raise ValueError('Invalid order book level')
            price, quantity = _num(level[0], positive=True), _num(level[1], positive=True) * contract_size
            if previous is not None and ((side == 'buy' and price < previous) or (side == 'sell' and price > previous)):
                raise ValueError('Order book levels out of order')
            previous = price
            taken = min(remaining, quantity)
            value += taken * price
            remaining -= taken
            if remaining == 0:
                average = value / wanted_base
                adverse = (average - mark) / mark if side == 'buy' else (mark - average) / mark
                # Round adverse slippage upward, never understate a cost.
                bounded = max(Decimal(0), adverse).quantize(Decimal('0.000000000001'), rounding=ROUND_CEILING)
                return format(bounded, 'f')
    raise ValueError('Insufficient book depth')


def collect(venue: str, base: str, side: str, quantity: str) -> VenueSnapshot:
    """Acquire and normalize a complete snapshot, with no default values.

    This conservative first pass supports linear USDT perpetuals, one-way
    positions and cross margin only. Missing source data blocks the preflight.
    """
    if side not in ('buy', 'sell'):
        raise DemoDataBlocked('Invalid side')
    try:
        wanted = _num(quantity, positive=True)
        reads = {name: read(venue, base, name) for name in _resources(venue, base) if name != 'fills'}
        data = {name: item.payload for name, item in reads.items()}
        symbol, swap = base + 'USDT', base + '-USDT-SWAP'
        if venue == 'bybit':
            inst = _one(data['instrument']['result']['list'], 'symbol', symbol)
            tick = _one(data['ticker']['result']['list'], 'symbol', symbol)
            pos = _one(data['position']['result']['list'], 'symbol', symbol)
            wallet = data['wallet']['result']['list']
            fee = _one(data['fees']['result']['list'], 'symbol', symbol)
            account = data['account']['result']
            orders = data['orders']['result']
            if (inst['contractType'] != 'LinearPerpetual' or inst['baseCoin'] != base or inst['quoteCoin'] != 'USDT' or
                    inst['settleCoin'] != 'USDT' or account['marginMode'] != 'REGULAR_MARGIN' or
                    pos['positionIdx'] != 0 or not isinstance(wallet, list) or len(wallet) != 1 or
                    wallet[0]['accountType'] != 'UNIFIED' or not isinstance(orders['list'], list) or
                    orders['nextPageCursor'] != ''):
                raise ValueError('Unsupported account or instrument')
            book = data['book']['result']
            if book['s'] != symbol:
                raise ValueError('Unexpected book')
            price, contract = _num(tick['markPrice'], positive=True), Decimal(1)
            slip = _book_slippage(book['a' if side == 'buy' else 'b'], wanted, contract, price, side)
            lot = inst['lotSizeFilter']
            return VenueSnapshot('bybit', 'demo', symbol, 'linear_perpetual',
                'trading' if inst['status'] == 'Trading' else 'unavailable',
                min(_stamp(data['ticker']['time']), _stamp(book['ts']), reads['instrument'].started_ms,
                    reads['ticker'].started_ms, reads['book'].started_ms),
                min(_stamp(data['wallet']['time']), _stamp(data['position']['time']), _stamp(data['orders']['time']),
                    *(reads[name].started_ms for name in ('wallet', 'position', 'orders', 'fees', 'account'))),
                min(_stamp(data['ticker']['time']), reads['ticker'].started_ms), _stamp(tick['nextFundingTime']), tick['fundingRate'],
                tick['markPrice'], inst['priceFilter']['tickSize'], lot['qtyStep'], lot['minOrderQty'],
                lot['maxMktOrderQty'], lot['minNotionalValue'], '1', pos['leverage'],
                wallet[0]['totalAvailableBalance'], fee['takerFeeRate'], slip, 'cross', 'net', pos['size'], len(orders['list']))
        inst = _one(data['instrument']['data'], 'instId', swap)
        mark = _one(data['mark']['data'], 'instId', swap)
        funding = _one(data['funding']['data'], 'instId', swap)
        book = _one_scoped(data['book']['data'], 'instId', swap)
        config = data['account']['data']
        accounts = data['wallet']['data']
        positions = data['position']['data']
        orders = data['orders']['data']
        fees = data['fees']['data']
        leverage = _one(data['leverage']['data'], 'instId', swap)
        if (inst['instType'] != 'SWAP' or inst['ctType'] != 'linear' or inst['ctValCcy'] != base or
                inst['settleCcy'] != 'USDT' or not isinstance(config, list) or len(config) != 1 or
                config[0]['posMode'] != 'net_mode' or config[0]['acctLv'] != '2' or
                not isinstance(accounts, list) or len(accounts) != 1 or not isinstance(positions, list) or
                not isinstance(orders, list) or not isinstance(fees, list) or len(fees) != 1 or leverage['mgnMode'] != 'cross'):
            raise ValueError('Unsupported account or instrument')
        usdt = _one([row for row in accounts[0]['details'] if row.get('ccy') == 'USDT'], 'ccy', 'USDT')
        if any(p.get('instId') != swap or p.get('posSide') != 'net' or _num(p['pos']) != 0 for p in positions):
            raise ValueError('Existing or ambiguous position')
        contract = _num(inst['ctVal'], positive=True) * _num(inst['ctMult'], positive=True)
        price = _num(mark['markPx'], positive=True)
        slip = _book_slippage(book['asks' if side == 'buy' else 'bids'], wanted, contract, price, side)
        taker = abs(_num(fees[0]['taker']))
        min_notional = _num(inst['minSz'], positive=True) * contract * price
        return VenueSnapshot('okx', 'demo', swap, 'linear_perpetual',
            'trading' if inst['state'] == 'live' else 'unavailable',
            min(_stamp(mark['ts']), _stamp(book['ts']), reads['instrument'].started_ms,
                reads['mark'].started_ms, reads['book'].started_ms),
            min(reads[name].started_ms for name in ('account', 'wallet', 'position', 'orders', 'fees', 'leverage')),
            min(_stamp(funding['ts']), reads['funding'].started_ms),
            _stamp(funding['fundingTime']), funding['fundingRate'], mark['markPx'], inst['tickSz'],
            inst['lotSz'], inst['minSz'], inst['maxMktSz'], format(min_notional, 'f'), format(contract, 'f'),
            leverage['lever'], usdt['availEq'], format(taker, 'f'), slip, 'cross', 'net', '0', len(orders))
    except Exception:
        raise DemoDataBlocked('Demo preflight data incomplete or inconsistent') from None


def verify_step(pair: demo.FuturesPair, legs: list[dict[str, Any]], confirmed_count: int, *, max_age_ms: int) -> None:
    """Read demo executions and both positions; never place or retry an order.

    A bounded, non-paginated history is required. This routine blocks if any
    execution, position, order or source time is ambiguous.
    """
    if (not isinstance(legs, list) or len(legs) != 2 or type(confirmed_count) is not int or confirmed_count not in (1, 2) or
            isinstance(max_age_ms, bool) or not isinstance(max_age_ms, int) or max_age_ms <= 0):
        raise DemoDataBlocked('Invalid reconciliation request')
    try:
        pair.validate()
        venues = (pair.long_venue, pair.short_venue)
        now = int(time.time() * 1000)
        all_times: list[int] = []
        for index, venue in enumerate(venues):
            entry = legs[index]
            fields = entry['request']
            if entry['venue'] != venue or not isinstance(fields, dict):
                raise ValueError('Leg mismatch')
            confirmed = entry.get('confirmation') if index < confirmed_count else None
            if index < confirmed_count and (not isinstance(confirmed, dict) or confirmed.get('venue') != venue):
                raise ValueError('Confirmation unavailable')
            position_result = read(venue, pair.base, 'position')
            orders_result = read(venue, pair.base, 'orders')
            evidence = [position_result, orders_result]
            expected_qty = _num(fields['qty' if venue == 'bybit' else 'sz'], positive=True) if confirmed else Decimal(0)
            expected_sign = Decimal(1) if index == 0 else Decimal(-1)
            if venue == 'bybit':
                position = _one(position_result.payload['result']['list'], 'symbol', pair.base + 'USDT')
                orders = orders_result.payload['result']
                if (position['positionIdx'] != 0 or not isinstance(orders['list'], list) or orders['list'] or
                        orders['nextPageCursor'] != '' or _num(position['size']) != expected_qty or
                        (expected_qty and position['side'] != ('Buy' if index == 0 else 'Sell'))):
                    raise ValueError('Bybit position mismatch')
                exchange_times = [_stamp(position_result.payload['time']), _stamp(orders_result.payload['time'])]
            else:
                positions, orders = position_result.payload['data'], orders_result.payload['data']
                if not isinstance(positions, list) or not isinstance(orders, list) or orders:
                    raise ValueError('OKX pending orders')
                if expected_qty:
                    position = _one(positions, 'instId', pair.base + '-USDT-SWAP')
                    if (position['posSide'] != 'net' or position['mgnMode'] != 'cross' or
                            _num(position['pos']) != expected_qty * expected_sign):
                        raise ValueError('OKX position mismatch')
                elif positions:
                    if any(p.get('instId') != pair.base + '-USDT-SWAP' or p.get('posSide') != 'net' or
                           _num(p['pos']) != 0 for p in positions):
                        raise ValueError('Unexpected OKX position')
                exchange_times = []
            if confirmed:
                fills_result = read(venue, pair.base, 'fills')
                evidence.append(fills_result)
                if venue == 'bybit':
                    payload = fills_result.payload['result']
                    rows = payload['list']
                    if payload['nextPageCursor'] != '':
                        raise ValueError('Paginated executions require manual reconciliation')
                    exchange_times.append(_stamp(fills_result.payload['time']))
                    client, symbol, order = fields['orderLinkId'], fields['symbol'], confirmed['order_id']
                    identity = lambda row: (row.get('orderLinkId'), row.get('orderId'), row.get('symbol'), row.get('side'))
                    expected = (client, order, symbol, fields['side'])
                    exec_id, exec_qty = 'execId', 'execQty'
                else:
                    rows = fills_result.payload['data']
                    client, symbol, order = fields['clOrdId'], fields['instId'], confirmed['order_id']
                    identity = lambda row: (row.get('clOrdId'), row.get('ordId'), row.get('instId'), row.get('side'))
                    expected = (client, order, symbol, fields['side'])
                    exec_id, exec_qty = 'tradeId', 'fillSz'
                if not isinstance(rows, list) or len(rows) >= 100:
                    raise ValueError('Incomplete execution history')
                selected = [row for row in rows if isinstance(row, dict) and
                            (identity(row)[0] == client or identity(row)[1] == order)]
                if not selected or any(identity(row) != expected for row in selected):
                    raise ValueError('Missing or duplicate client/order identity')
                ids = [row.get(exec_id) for row in selected]
                if (not all(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value) for value in ids) or
                        len(set(ids)) != len(ids)):
                    raise ValueError('Duplicate or invalid execution id')
                with localcontext() as ctx:
                    ctx.prec = 80
                    amounts = [_num(row[exec_qty], positive=True) for row in selected]
                    if sum(amounts, Decimal(0)) != expected_qty:
                        raise ValueError('Execution quantity mismatch')
            now_after = int(time.time() * 1000)
            if any(item.started_ms > now_after or now_after - item.started_ms > max_age_ms for item in evidence):
                raise ValueError('Stale reconciliation response')
            if any(stamp > now_after or now_after - stamp > max_age_ms for stamp in exchange_times):
                raise ValueError('Stale exchange timestamp')
            if now_after < now:
                raise ValueError('Clock moved backwards')
            all_times.extend(item.started_ms for item in evidence)
            all_times.extend(exchange_times)
        now_after = int(time.time() * 1000)
        if now_after < now or any(stamp > now_after or now_after - stamp > max_age_ms for stamp in all_times):
            raise ValueError('Reconciliation window expired')
    except Exception:
        raise DemoDataBlocked('Demo reconciliation is uncertain; stop and reconcile manually') from None
