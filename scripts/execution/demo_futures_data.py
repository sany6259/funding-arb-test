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
            'conditional_orders': ('/v5/order/realtime', {'category': 'linear', 'symbol': symbol, 'openOnly': '0', 'orderFilter': 'StopOrder', 'limit': '50'}),
            'fees': ('/v5/account/fee-rate', {'category': 'linear', 'symbol': symbol}),
            'account': ('/v5/account/info', {}),
            'fills': ('/v5/execution/list', {'category': 'linear', 'symbol': symbol, 'limit': '100'}),
            'lookup_current': ('/v5/order/realtime', {'category': 'linear', 'symbol': symbol, 'limit': '50'}),
            'lookup_history': ('/v5/order/history', {'category': 'linear', 'symbol': symbol, 'limit': '50'}),
        }
    if venue == 'okx':
        resources = {
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
            'lookup_order': ('/api/v5/trade/order', {'instId': swap}),
        }
        for order_type in ('conditional', 'trigger', 'oco', 'move_order_stop', 'iceberg', 'twap'):
            resources['algo_' + order_type] = ('/api/v5/trade/orders-algo-pending',
                                               {'instType': 'SWAP', 'instId': swap, 'ordType': order_type})
        return resources
    raise DemoDataBlocked('Unsupported venue')


def read(venue: str, base: str, resource: str, *, cursor: str | None = None,
         client_id: str | None = None, order_id: str | None = None) -> ReadResult:
    """Fixed allowlisted HTTPS GET with isolated credentials and mode lock."""
    routes = _resources(venue, base)
    if resource not in routes:
        raise DemoDataBlocked('Unsupported read-only resource')
    lookup = resource in ('lookup_current', 'lookup_history', 'lookup_order')
    if lookup:
        if (client_id is None) == (order_id is None):
            raise DemoDataBlocked('Exactly one order identifier is required')
        identifier = client_id if client_id is not None else order_id
        pattern = r'[A-Za-z0-9]{1,24}' if client_id is not None else r'[A-Za-z0-9_-]{1,80}'
        if not isinstance(identifier, str) or not re.fullmatch(pattern, identifier):
            raise DemoDataBlocked('Invalid order identifier')
    elif client_id is not None or order_id is not None:
        raise DemoDataBlocked('Identifier is not valid for this resource')
    if cursor is not None and (resource != 'fills' or not isinstance(cursor, str) or
                               not re.fullmatch(r'[A-Za-z0-9%_:.=+\-]{1,512}', cursor) or
                               (venue == 'okx' and not re.fullmatch(r'[0-9]{1,32}', cursor))):
        raise DemoDataBlocked('Invalid pagination cursor')
    try:
        started_ms = int(time.time() * 1000)
        demo._permit(True)
        creds = demo._demo_creds(venue)
        path, params = routes[resource]
        params = dict(params)
        if lookup:
            name = ('orderLinkId' if venue == 'bybit' else 'clOrdId') if client_id is not None else ('orderId' if venue == 'bybit' else 'ordId')
            params[name] = client_id if client_id is not None else order_id
        if cursor is not None:
            params['cursor' if venue == 'bybit' else 'after'] = cursor
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


def _check_conditionals(venue: str, reads: dict[str, ReadResult]) -> None:
    if venue == 'bybit':
        result = reads['conditional_orders'].payload['result']
        if not isinstance(result['list'], list) or result['list'] or result['nextPageCursor'] != '':
            raise ValueError('Conditional orders present or incomplete')
        return
    for order_type in ('conditional', 'trigger', 'oco', 'move_order_stop', 'iceberg', 'twap'):
        rows = reads['algo_' + order_type].payload['data']
        if not isinstance(rows, list) or rows:
            raise ValueError('Conditional orders present or incomplete')


def _all_fills(venue: str, base: str, *, max_pages: int = 20) -> tuple[list[dict[str, Any]], list[ReadResult], list[int]]:
    """Follow exchange-provided pagination; fail on loops or incomplete data."""
    rows: list[dict[str, Any]] = []
    evidence: list[ReadResult] = []
    exchange_times: list[int] = []
    cursor: str | None = None
    seen: set[str] = set()
    seen_executions: set[str] = set()
    for _ in range(max_pages):
        result = read(venue, base, 'fills') if cursor is None else read(venue, base, 'fills', cursor=cursor)
        evidence.append(result)
        if venue == 'bybit':
            payload = result.payload['result']
            batch = payload['list']
            token = payload['nextPageCursor']
            exchange_times.append(_stamp(result.payload['time']))
            if not isinstance(token, str):
                raise ValueError('Missing pagination cursor')
        else:
            batch = result.payload['data']
            token = ''
            if isinstance(batch, list) and len(batch) == 100:
                last = batch[-1]
                if not isinstance(last, dict) or not isinstance(last.get('billId'), str) or not re.fullmatch(r'[0-9]{1,32}', last['billId']):
                    raise ValueError('Missing pagination identifier')
                token = last['billId']
        if not isinstance(batch, list) or len(batch) > 100 or not all(isinstance(row, dict) for row in batch):
            raise ValueError('Invalid execution page')
        id_field = 'execId' if venue == 'bybit' else 'tradeId'
        for row in batch:
            exec_id = row.get(id_field)
            if (not isinstance(exec_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', exec_id) or
                    exec_id in seen_executions):
                raise ValueError('Duplicate or invalid execution history')
            seen_executions.add(exec_id)
        rows.extend(batch)
        if not token:
            return rows, evidence, exchange_times
        if token in seen:
            raise ValueError('Repeated pagination cursor')
        seen.add(token)
        cursor = token
    raise ValueError('Execution history exceeds page limit')


def collect(venue: str, base: str, side: str, quantity: str) -> VenueSnapshot:
    """Acquire and normalize a complete snapshot, with no default values.

    This conservative first pass supports linear USDT perpetuals, one-way
    positions and cross margin only. Missing source data blocks the preflight.
    """
    if side not in ('buy', 'sell'):
        raise DemoDataBlocked('Invalid side')
    try:
        wanted = _num(quantity, positive=True)
        reads = {name: read(venue, base, name) for name in _resources(venue, base)
                 if name != 'fills' and not name.startswith('lookup_')}
        data = {name: item.payload for name, item in reads.items()}
        _check_conditionals(venue, reads)
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
                    *(reads[name].started_ms for name in ('wallet', 'position', 'orders', 'conditional_orders', 'fees', 'account'))),
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
            min(item.started_ms for name, item in reads.items() if name in ('account', 'wallet', 'position', 'orders', 'fees', 'leverage') or name.startswith('algo_')),
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
            conditional_names = (('conditional_orders',) if venue == 'bybit' else
                                 tuple('algo_' + order_type for order_type in
                                       ('conditional', 'trigger', 'oco', 'move_order_stop', 'iceberg', 'twap')))
            conditional = {name: read(venue, pair.base, name) for name in conditional_names}
            _check_conditionals(venue, conditional)
            evidence.extend(conditional.values())
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
                rows, fill_evidence, fill_times = _all_fills(venue, pair.base)
                evidence.extend(fill_evidence)
                exchange_times.extend(fill_times)
                if venue == 'bybit':
                    client, symbol, order = fields['orderLinkId'], fields['symbol'], confirmed['order_id']
                    identity = lambda row: (row.get('orderLinkId'), row.get('orderId'), row.get('symbol'), row.get('side'))
                    expected = (client, order, symbol, fields['side'])
                    exec_id, exec_qty = 'execId', 'execQty'
                else:
                    client, symbol, order = fields['clOrdId'], fields['instId'], confirmed['order_id']
                    identity = lambda row: (row.get('clOrdId'), row.get('ordId'), row.get('instId'), row.get('side'))
                    expected = (client, order, symbol, fields['side'])
                    exec_id, exec_qty = 'tradeId', 'fillSz'
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


def lookup_confirmation(venue: str, base: str, fields: dict[str, Any], *, order_id: str | None,
                        max_age_ms: int) -> dict[str, str]:
    """Look up an order by client ID and, when known, exchange ID.

    Read-only evidence is returned only for one fully filled matching order.
    It never authorizes retry, journal clearance, or another order.
    """
    if isinstance(max_age_ms, bool) or not isinstance(max_age_ms, int) or max_age_ms <= 0:
        raise DemoDataBlocked('Invalid reconciliation age')
    try:
        demo._validate_route(venue, 'POST', None, fields)
        symbol_field = fields['symbol' if venue == 'bybit' else 'instId']
        if symbol_field != base + ('USDT' if venue == 'bybit' else '-USDT-SWAP'):
            raise ValueError('Instrument mismatch')
        client = fields['orderLinkId' if venue == 'bybit' else 'clOrdId']
        resources = ('lookup_current', 'lookup_history') if venue == 'bybit' else ('lookup_order',)
        reads = []
        exchange_times = []
        rows: list[dict[str, Any]] = []
        for resource in resources:
            result = read(venue, base, resource, client_id=client)
            reads.append(result)
            if venue == 'bybit':
                payload = result.payload['result']
                batch = payload['list']
                if payload['nextPageCursor'] != '':
                    raise ValueError('Incomplete order history')
                exchange_times.append(_stamp(result.payload['time']))
            else:
                batch = result.payload['data']
            if not isinstance(batch, list) or len(batch) > 1 or not all(isinstance(row, dict) for row in batch):
                raise ValueError('Duplicate or ambiguous order')
            rows.extend(batch)
        if not rows:
            raise ValueError('Order unavailable')
        resolved = rows[0].get('orderId' if venue == 'bybit' else 'ordId')
        if not isinstance(resolved, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', resolved):
            raise ValueError('Order id unavailable')
        if order_id is not None:
            if resolved != order_id:
                raise ValueError('Order id mismatch')
            found_by_order_id = False
            for resource in resources:
                result = read(venue, base, resource, order_id=order_id)
                reads.append(result)
                if venue == 'bybit':
                    payload = result.payload['result']
                    batch = payload['list']
                    if payload['nextPageCursor'] != '':
                        raise ValueError('Incomplete order history')
                    exchange_times.append(_stamp(result.payload['time']))
                else:
                    batch = result.payload['data']
                if not isinstance(batch, list) or len(batch) > 1 or not all(isinstance(row, dict) for row in batch):
                    raise ValueError('Ambiguous order lookup')
                found_by_order_id = found_by_order_id or bool(batch)
                rows.extend(batch)
            if not found_by_order_id:
                raise ValueError('Order id lookup unavailable')
        now = int(time.time() * 1000)
        if any(item.started_ms > now or now - item.started_ms > max_age_ms for item in reads):
            raise ValueError('Stale order lookup')
        if any(stamp > now or now - stamp > max_age_ms for stamp in exchange_times):
            raise ValueError('Stale exchange timestamp')
        expected_qty = _num(fields['qty' if venue == 'bybit' else 'sz'], positive=True)
        prices = set()
        for row in rows:
            if venue == 'bybit':
                valid = (row.get('orderId') == resolved and row.get('orderLinkId') == client and
                         row.get('symbol') == fields['symbol'] and row.get('side') == fields['side'] and
                         row.get('orderStatus') == 'Filled')
                filled, price = _num(row.get('cumExecQty')), _num(row.get('avgPrice'))
            else:
                valid = (row.get('ordId') == resolved and row.get('clOrdId') == client and
                         row.get('instId') == fields['instId'] and row.get('side') == fields['side'] and
                         row.get('state') == 'filled')
                filled, price = _num(row.get('accFillSz')), _num(row.get('avgPx'))
            reduce_only = row.get('reduceOnly')
            if fields.get('reduceOnly') is True and not (reduce_only is True or reduce_only == 'true'):
                raise ValueError('Reduce-only not confirmed')
            if not valid or filled != expected_qty or price <= 0:
                raise ValueError('Order not fully confirmed')
            prices.add(price)
        if len(prices) != 1:
            raise ValueError('Conflicting execution prices')
        return {'venue': venue, 'order_id': resolved, 'filled_qty': format(expected_qty, 'f'),
                'avg_price': format(prices.pop(), 'f')}
    except Exception:
        raise DemoDataBlocked('Demo order lookup is uncertain') from None


def verify_close_step(pair: demo.FuturesPair, opening_legs: list[dict[str, Any]],
                      closing_legs: list[dict[str, Any]], closed_count: int, *, max_age_ms: int) -> None:
    """Read-only check of closing fills, residual positions and pending orders."""
    if (not isinstance(opening_legs, list) or len(opening_legs) != 2 or
            not isinstance(closing_legs, list) or len(closing_legs) != 2 or
            type(closed_count) is not int or closed_count not in (1, 2) or
            isinstance(max_age_ms, bool) or not isinstance(max_age_ms, int) or max_age_ms <= 0):
        raise DemoDataBlocked('Invalid close reconciliation request')
    try:
        pair.validate()
        if {entry['venue'] for entry in closing_legs} != {'bybit', 'okx'}:
            raise ValueError('Invalid close venues')
        clients = [leg['request']['orderLinkId' if leg['venue'] == 'bybit' else 'clOrdId']
                   for leg in opening_legs + closing_legs]
        if len(set(clients)) != 4:
            raise ValueError('Duplicate client order identifiers')
        close_by_venue = {entry['venue']: entry for entry in closing_legs[:closed_count]}
        if len(close_by_venue) != closed_count or not set(close_by_venue) <= {'bybit', 'okx'}:
            raise ValueError('Invalid close legs')
        now = int(time.time() * 1000)
        observed_times: list[int] = []
        for index, venue in enumerate((pair.long_venue, pair.short_venue)):
            opened = opening_legs[index]
            if opened['venue'] != venue or not isinstance(opened.get('confirmation'), dict):
                raise ValueError('Opening confirmation unavailable')
            opening_fields = opened['request']
            demo._validate_route(venue, 'POST', None, opening_fields)
            if ('reduceOnly' in opening_fields or opening_fields['side'].lower() != ('buy' if index == 0 else 'sell') or
                    opening_fields['symbol' if venue == 'bybit' else 'instId'] != pair.base + ('USDT' if venue == 'bybit' else '-USDT-SWAP')):
                raise ValueError('Opening order identity mismatch')
            initial = _num(opening_fields['qty' if venue == 'bybit' else 'sz'], positive=True)
            expected_open = _num(pair.base_quantity if venue == 'bybit' else pair.okx_contracts, positive=True)
            if (initial != expected_open or opened['confirmation'].get('venue') != venue or
                    _num(opened['confirmation'].get('filled_qty'), positive=True) != initial):
                raise ValueError('Opening quantity mismatch')
            expected = Decimal(0) if venue in close_by_venue else initial
            sign = Decimal(1) if index == 0 else Decimal(-1)
            position = read(venue, pair.base, 'position')
            orders = read(venue, pair.base, 'orders')
            conditional_names = (('conditional_orders',) if venue == 'bybit' else
                tuple('algo_' + kind for kind in ('conditional', 'trigger', 'oco', 'move_order_stop', 'iceberg', 'twap')))
            conditional = {name: read(venue, pair.base, name) for name in conditional_names}
            _check_conditionals(venue, conditional)
            observed_times.extend([position.started_ms, orders.started_ms, *(item.started_ms for item in conditional.values())])
            if venue == 'bybit':
                row = _one(position.payload['result']['list'], 'symbol', pair.base + 'USDT')
                pending = orders.payload['result']
                if (row['positionIdx'] != 0 or _num(row['size']) != expected or
                        (expected and row['side'] != ('Buy' if index == 0 else 'Sell')) or
                        not isinstance(pending['list'], list) or pending['list'] or pending['nextPageCursor'] != ''):
                    raise ValueError('Bybit residual position mismatch')
                observed_times.extend([_stamp(position.payload['time']), _stamp(orders.payload['time'])])
            else:
                rows, pending = position.payload['data'], orders.payload['data']
                if not isinstance(rows, list) or not isinstance(pending, list) or pending:
                    raise ValueError('OKX position or pending orders unavailable')
                if expected:
                    row = _one(rows, 'instId', pair.base + '-USDT-SWAP')
                    if row['posSide'] != 'net' or row['mgnMode'] != 'cross' or _num(row['pos']) != expected * sign:
                        raise ValueError('OKX residual position mismatch')
                elif any(row.get('instId') != pair.base + '-USDT-SWAP' or row.get('posSide') != 'net' or _num(row['pos']) != 0 for row in rows):
                    raise ValueError('OKX residual position mismatch')
            if venue in close_by_venue:
                close = close_by_venue[venue]
                fields, confirmation = close['request'], close['confirmation']
                demo._validate_route(venue, 'POST', None, fields)
                if (fields.get('reduceOnly') is not True or not isinstance(confirmation, dict) or
                        confirmation.get('venue') != venue or _num(confirmation.get('filled_qty'), positive=True) != initial or
                        confirmation.get('order_id') != close.get('order_id') or
                        not isinstance(close.get('order_id'), str) or
                        not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', close['order_id']) or
                        _num(confirmation.get('avg_price'), positive=True) <= 0):
                    raise ValueError('Reduce-only close not confirmed')
                close_qty = _num(fields['qty' if venue == 'bybit' else 'sz'], positive=True)
                original_side = opened['request']['side'].lower()
                if close_qty != initial or fields['side'].lower() == original_side:
                    raise ValueError('Close does not reduce the recorded position')
                rows, evidence, times = _all_fills(venue, pair.base)
                observed_times.extend([*(item.started_ms for item in evidence), *times])
                client = fields['orderLinkId' if venue == 'bybit' else 'clOrdId']
                order = confirmation['order_id']
                if venue == 'bybit':
                    identify = lambda row: (row.get('orderLinkId'), row.get('orderId'), row.get('symbol'), row.get('side'))
                    wanted = (client, order, fields['symbol'], fields['side'])
                    amount = 'execQty'
                else:
                    identify = lambda row: (row.get('clOrdId'), row.get('ordId'), row.get('instId'), row.get('side'))
                    wanted = (client, order, fields['instId'], fields['side'])
                    amount = 'fillSz'
                chosen = [row for row in rows if identify(row)[0] == client or identify(row)[1] == order]
                if not chosen or any(identify(row) != wanted for row in chosen):
                    raise ValueError('Close execution identity mismatch')
                with localcontext() as ctx:
                    ctx.prec = 80
                    if sum((_num(row[amount], positive=True) for row in chosen), Decimal(0)) != initial:
                        raise ValueError('Incomplete close execution')
        now_after = int(time.time() * 1000)
        if now_after < now or any(stamp > now_after or now_after - stamp > max_age_ms for stamp in observed_times):
            raise ValueError('Stale close reconciliation')
    except Exception:
        raise DemoDataBlocked('Demo close state is uncertain; manual reconciliation required') from None
