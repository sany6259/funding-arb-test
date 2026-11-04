"""Read-only demo preflight integration; every exchange response is mocked."""
from __future__ import annotations

import copy
import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / 'scripts'):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
from core import demo_credentials, execution_mode
from execution import demo_futures as demo, demo_futures_data as data
from execution.demo_futures_preflight import RiskLimits

NOW = 1_800_000_000_000


def bybit(result): return {'retCode': 0, 'time': NOW - 100, 'result': result}
def okx(data): return {'code': '0', 'data': data}


def fixtures():
    return {
        ('bybit', '/v5/market/instruments-info'): bybit({'list': [{'symbol': 'BTCUSDT', 'contractType': 'LinearPerpetual',
            'baseCoin': 'BTC', 'quoteCoin': 'USDT', 'settleCoin': 'USDT', 'status': 'Trading',
            'priceFilter': {'tickSize': '0.1'}, 'lotSizeFilter': {'qtyStep': '0.002', 'minOrderQty': '0.002',
            'maxMktOrderQty': '1', 'minNotionalValue': '5'}}]}),
        ('bybit', '/v5/market/tickers'): bybit({'list': [{'symbol': 'BTCUSDT', 'markPrice': '100000',
            'fundingRate': '-0.01', 'nextFundingTime': str(NOW + 300_000)}]}),
        ('bybit', '/v5/market/orderbook'): bybit({'s': 'BTCUSDT', 'ts': NOW-100,
            'a': [['100001', '1']], 'b': [['99999', '1']]}),
        ('bybit', '/v5/position/list'): bybit({'list': [{'symbol': 'BTCUSDT', 'positionIdx': 0, 'size': '0', 'leverage': '2'}]}),
        ('bybit', '/v5/account/wallet-balance'): bybit({'list': [{'accountType': 'UNIFIED', 'totalAvailableBalance': '10000'}]}),
        ('bybit', '/v5/order/realtime'): bybit({'list': [], 'nextPageCursor': ''}),
        ('bybit', '/v5/order/realtime#conditional'): bybit({'list': [], 'nextPageCursor': ''}),
        ('bybit', '/v5/order/history'): bybit({'list': [], 'nextPageCursor': ''}),
        ('bybit', '/v5/account/fee-rate'): bybit({'list': [{'symbol': 'BTCUSDT', 'takerFeeRate': '0.0005'}]}),
        ('bybit', '/v5/account/info'): bybit({'marginMode': 'REGULAR_MARGIN'}),
        ('okx', '/api/v5/public/instruments'): okx([{'instId': 'BTC-USDT-SWAP', 'instType': 'SWAP', 'ctType': 'linear',
            'ctValCcy': 'BTC', 'settleCcy': 'USDT', 'state': 'live', 'ctVal': '0.01', 'ctMult': '1',
            'tickSz': '0.1', 'lotSz': '0.3', 'minSz': '0.3', 'maxMktSz': '100'}]),
        ('okx', '/api/v5/public/mark-price'): okx([{'instId': 'BTC-USDT-SWAP', 'markPx': '100050', 'ts': str(NOW-100)}]),
        ('okx', '/api/v5/public/funding-rate'): okx([{'instId': 'BTC-USDT-SWAP', 'fundingRate': '0.01',
            'fundingTime': str(NOW+300_000), 'ts': str(NOW-100)}]),
        ('okx', '/api/v5/market/books'): okx([{'ts': str(NOW-100), 'asks': [['100051', '10', '0', '1']], 'bids': [['100049', '10', '0', '1']]}]),
        ('okx', '/api/v5/account/config'): okx([{'posMode': 'net_mode', 'acctLv': '2'}]),
        ('okx', '/api/v5/account/balance'): okx([{'details': [{'ccy': 'USDT', 'availEq': '10000'}]}]),
        ('okx', '/api/v5/account/positions'): okx([]),
        ('okx', '/api/v5/trade/orders-pending'): okx([]),
        ('okx', '/api/v5/trade/orders-algo-pending'): okx([]),
        ('okx', '/api/v5/trade/order'): okx([]),
        ('okx', '/api/v5/account/trade-fee'): okx([{'taker': '-0.0005'}]),
        ('okx', '/api/v5/account/leverage-info'): okx([{'instId': 'BTC-USDT-SWAP', 'mgnMode': 'cross', 'lever': '2'}]),
    }


def limits():
    return RiskLimits(10_000, 60_000, 30_000, 600_000, 5_000,
                      '10000', '3', '1', '0.001', '0.0005', '0.1', '0')


def pair(): return demo.FuturesPair('BTC', 'bybit', 'okx', '0.018', '1.8', '0.01')


@pytest.fixture
def exchange(monkeypatch, tmp_path):
    monkeypatch.setattr(execution_mode, '_directory', lambda: tmp_path / 'state')
    execution_mode.set_mode('demo')
    monkeypatch.setattr(demo_credentials, '_backend', lambda: 'age')
    monkeypatch.setattr(demo_credentials, 'load', lambda venue, environment: {
        'key': venue + '-dummy-key', 'secret': venue + '-dummy-secret', 'passphrase': 'dummy-pass'})
    monkeypatch.setenv('FARB_ENABLE_DEMO_ORDERS', '1')
    for name in ('FARB_DRY_RUN', 'FARB_LIVE', 'DCA_LIVE', 'BYBIT_REQUIRE_TESTNET', 'BYBIT_API_KEY', 'OKX_API_KEY'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(data, 'time', SimpleNamespace(time=lambda: NOW / 1000))
    monkeypatch.setattr(demo, 'time', SimpleNamespace(time=lambda: NOW / 1000))
    responses = fixtures()
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            assert request.method == 'GET', 'preflight must never send POST'
            host = urlsplit(request.full_url).hostname
            venue = {'api-demo.bybit.com': 'bybit', 'www.okx.com': 'okx'}.get(host)
            assert venue is not None, 'production or unknown endpoint'
            url = urlsplit(request.full_url)
            path = url.path
            if venue == 'bybit' and path == '/v5/order/realtime' and parse_qs(url.query).get('orderFilter') == ['StopOrder']:
                path += '#conditional'
            payload = responses[(venue, path)]
            class Response:
                def __enter__(self): return self
                def __exit__(self, *_): return False
                def read(self, limit): return json.dumps(payload).encode()
            return Response()
    monkeypatch.setattr(data.urllib.request, 'build_opener', lambda handler: Opener())
    return responses, calls


def test_preflight_collects_both_demo_venues_but_preserves_order_block(exchange):
    _, calls = exchange
    with pytest.raises(demo.DemoBlocked, match='reconciliation and safe close'):
        demo.execute_pair(pair(), confirmed=True, risk_limits=limits())
    assert len(calls) == 25 and all(req.method == 'GET' for req in calls)
    assert not demo._journal_path().exists()
    for req in calls:
        url = urlsplit(req.full_url)
        headers = {k.lower(): v for k, v in req.header_items()}
        assert url.scheme == 'https'
        if url.hostname == 'api-demo.bybit.com':
            assert headers['x-bapi-api-key'] == 'bybit-dummy-key'
            signed = headers['x-bapi-timestamp'] + 'bybit-dummy-key' + '5000' + url.query
            assert headers['x-bapi-sign'] == hmac.new(b'bybit-dummy-secret', signed.encode(), hashlib.sha256).hexdigest()
        else:
            assert url.hostname == 'www.okx.com'
            assert headers['x-simulated-trading'] == '1' and headers['ok-access-key'] == 'okx-dummy-key'
            target = url.path + ('?' + url.query if url.query else '')
            signed = headers['ok-access-timestamp'] + 'GET' + target
            assert headers['ok-access-sign'] == base64.b64encode(hmac.new(b'okx-dummy-secret', signed.encode(), hashlib.sha256).digest()).decode()
    query = parse_qs(urlsplit(calls[0].full_url).query)
    assert query['category'] == ['linear'] and query['symbol'] == ['BTCUSDT']


@pytest.mark.parametrize('mutate', [
    lambda f: f[('bybit', '/v5/position/list')]['result']['list'][0].update(size='0.01'),
    lambda f: f[('okx', '/api/v5/account/balance')]['data'][0]['details'][0].update(availEq='1'),
    lambda f: f[('okx', '/api/v5/public/instruments')]['data'][0].pop('ctVal'),
    lambda f: f[('bybit', '/v5/market/orderbook')]['result'].update(a=[['100001', '0.001']]),
    lambda f: f[('okx', '/api/v5/public/mark-price')]['data'][0].update(ts=str(NOW-11_000)),
    lambda f: f[('okx', '/api/v5/public/funding-rate')]['data'][0].pop('ts'),
    lambda f: f[('bybit', '/v5/account/fee-rate')].update(retCode=10003),
    lambda f: f[('okx', '/api/v5/trade/orders-pending')]['data'].append({'ordId': '1'}),
    lambda f: f[('bybit', '/v5/order/realtime#conditional')]['result']['list'].append({'orderId': '1'}),
    lambda f: f[('okx', '/api/v5/trade/orders-algo-pending')]['data'].append({'algoId': '1'}),
    lambda f: f[('okx', '/api/v5/account/config')]['data'][0].update(posMode='long_short_mode'),
])
def test_incomplete_or_unsafe_demo_data_blocks_without_order(exchange, mutate):
    responses, calls = exchange
    mutate(responses)
    with pytest.raises(demo.DemoBlocked):
        demo.execute_pair(pair(), confirmed=True, risk_limits=limits())
    assert all(req.method == 'GET' for req in calls)
    assert not demo._journal_path().exists()


def test_invalid_mode_or_resource_never_reaches_network(exchange, monkeypatch):
    _, calls = exchange
    with pytest.raises(data.DemoDataBlocked):
        data.read('bitget', 'BTC', 'instrument')
    with pytest.raises(data.DemoDataBlocked):
        data.read('bybit', 'BTC', '../../order/create')
    monkeypatch.setenv('FARB_LIVE', '1')
    with pytest.raises(data.DemoDataBlocked):
        data.read('bybit', 'BTC', 'instrument')
    assert not calls


def confirmed_legs():
    bybit = demo._order_fields('bybit', 'BTC', 'buy', '0.018', 'bybitclient')
    okx = demo._order_fields('okx', 'BTC', 'sell', '1.8', 'okxclient')
    return [
        {'venue': 'bybit', 'request': bybit, 'confirmation': {'venue': 'bybit', 'order_id': 'bybitorder'}},
        {'venue': 'okx', 'request': okx, 'confirmation': {'venue': 'okx', 'order_id': 'okxorder'}},
    ]


def add_reconciliation_fixtures(responses, both=False):
    responses[('bybit', '/v5/position/list')]['result']['list'][0].update(size='0.018', side='Buy')
    responses[('bybit', '/v5/execution/list')] = bybit({'nextPageCursor': '', 'list': [
        {'orderLinkId': 'bybitclient', 'orderId': 'bybitorder', 'symbol': 'BTCUSDT',
         'side': 'Buy', 'execId': 'bybitexec', 'execQty': '0.018'}]})
    if both:
        responses[('okx', '/api/v5/account/positions')] = okx([{
            'instId': 'BTC-USDT-SWAP', 'posSide': 'net', 'mgnMode': 'cross', 'pos': '-1.8'}])
        responses[('okx', '/api/v5/trade/fills')] = okx([{
            'clOrdId': 'okxclient', 'ordId': 'okxorder', 'instId': 'BTC-USDT-SWAP',
            'side': 'sell', 'tradeId': 'okxexec', 'fillSz': '1.8'}])


def test_reconciliation_reads_executions_and_both_positions_before_next_leg(exchange):
    responses, calls = exchange
    add_reconciliation_fixtures(responses)
    demo._reconcile_step(pair(), confirmed_legs(), 1, limits())
    assert len(calls) == 12 and all(req.method == 'GET' for req in calls)
    assert any(urlsplit(req.full_url).path == '/v5/execution/list' for req in calls)
    assert any(urlsplit(req.full_url).path == '/api/v5/account/positions' for req in calls)


def test_reconciliation_checks_both_futures_legs(exchange):
    responses, calls = exchange
    add_reconciliation_fixtures(responses, both=True)
    demo._reconcile_step(pair(), confirmed_legs(), 2, limits())
    assert len(calls) == 13 and all(req.method == 'GET' for req in calls)
    assert any(urlsplit(req.full_url).path == '/api/v5/trade/fills' for req in calls)


@pytest.mark.parametrize('mutate', [
    lambda f: f[('bybit', '/v5/execution/list')]['result']['list'].append(copy.deepcopy(f[('bybit', '/v5/execution/list')]['result']['list'][0])),
    lambda f: f[('bybit', '/v5/execution/list')]['result']['list'][0].update(execQty='0.009'),
    lambda f: f[('bybit', '/v5/execution/list')]['result']['list'][0].update(orderId='duplicate'),
    lambda f: f[('bybit', '/v5/execution/list')]['result'].update(nextPageCursor='another-page'),
    lambda f: f[('bybit', '/v5/position/list')]['result']['list'][0].update(size='0.009'),
    lambda f: f[('okx', '/api/v5/account/positions')]['data'].append({'instId': 'BTC-USDT-SWAP', 'posSide': 'net', 'pos': '1'}),
    lambda f: f[('okx', '/api/v5/trade/orders-pending')]['data'].append({'ordId': 'unexpected'}),
])
def test_reconciliation_rejects_ambiguous_fill_or_position(exchange, mutate):
    responses, calls = exchange
    add_reconciliation_fixtures(responses)
    mutate(responses)
    with pytest.raises(demo.ReconciliationRequired):
        demo._reconcile_step(pair(), confirmed_legs(), 1, limits())
    assert all(req.method == 'GET' for req in calls)


def test_reconciliation_requires_explicit_risk_limits(exchange):
    _, calls = exchange
    with pytest.raises(demo.ReconciliationRequired, match='limits'):
        demo._reconcile_step(pair(), confirmed_legs(), 1, None)
    assert not calls


def test_preflight_rejects_response_that_was_slow_before_final_check(exchange, monkeypatch):
    _, calls = exchange
    original = data.read
    def delayed(venue, base, resource):
        result = original(venue, base, resource)
        if venue == 'bybit' and resource == 'account':
            return data.ReadResult(result.payload, NOW - 11_000)
        return result
    monkeypatch.setattr(data, 'read', delayed)
    with pytest.raises(demo.DemoBlocked, match='could not validate'):
        demo.execute_pair(pair(), confirmed=True, risk_limits=limits())
    assert all(req.method == 'GET' for req in calls) and not demo._journal_path().exists()


def test_read_errors_do_not_expose_credentials(exchange, monkeypatch, capsys):
    class Failed:
        def open(self, request, timeout):
            raise RuntimeError('dummy-sensitive-key-in-exception')
    monkeypatch.setattr(data.urllib.request, 'build_opener', lambda _: Failed())
    with pytest.raises(data.DemoDataBlocked) as caught:
        data.read('okx', 'BTC', 'account')
    assert 'dummy-sensitive-key' not in str(caught.value) + repr(capsys.readouterr())


def journal_record(confirmed_count):
    legs = confirmed_legs()
    for index, leg in enumerate(legs):
        if index < confirmed_count:
            leg['order_id'] = leg['confirmation']['order_id']
            leg['confirmation']['filled_qty'] = leg['request']['qty' if leg['venue'] == 'bybit' else 'sz']
            leg['confirmation']['avg_price'] = '100000'
        else:
            leg.pop('confirmation')
    return {'version': 1, 'id': 'a' * 32, 'state': 'reconciliation_required', 'base': 'BTC',
            'okx_contract_size': '0.01', 'legs': legs}


def test_restart_reconciliation_reads_confirmed_leg_without_clearing_journal(exchange):
    responses, calls = exchange
    add_reconciliation_fixtures(responses)
    path = demo._journal_path()
    demo._write_journal(path, journal_record(1))
    original = path.read_bytes()
    result = demo.reconcile_pending(confirmed=True, risk_limits=limits())
    assert result == {'state': 'manual_reconciliation_required', 'confirmed_legs': 1, 'confirmed_leg_evidence_checked': True}
    assert path.read_bytes() == original and all(req.method == 'GET' for req in calls)
    with pytest.raises(demo.DemoBlocked, match='reconciliation'):
        demo.execute_pair(pair(), confirmed=True, risk_limits=limits())


def test_restart_before_order_confirmation_never_retries_or_clears(exchange):
    _, calls = exchange
    path = demo._journal_path()
    record = journal_record(0)
    record['state'] = 'leg_1_submitting'
    demo._write_journal(path, record)
    original = path.read_bytes()
    result = demo.reconcile_pending(confirmed=True, risk_limits=limits())
    assert result == {'state': 'manual_reconciliation_required', 'confirmed_legs': 0, 'confirmed_leg_evidence_checked': False}
    assert path.read_bytes() == original and all(req.method == 'GET' for req in calls)
    assert all(urlsplit(req.full_url).path in ('/v5/order/realtime', '/v5/order/history') for req in calls)


def test_restart_with_uncertain_positions_remains_blocked(exchange):
    responses, calls = exchange
    add_reconciliation_fixtures(responses)
    responses[('bybit', '/v5/position/list')]['result']['list'][0]['size'] = '0.009'
    path = demo._journal_path()
    demo._write_journal(path, journal_record(1))
    original = path.read_bytes()
    result = demo.reconcile_pending(confirmed=True, risk_limits=limits())
    assert result['state'] == 'manual_reconciliation_required' and not result['confirmed_leg_evidence_checked']
    assert path.read_bytes() == original and all(req.method == 'GET' for req in calls)


def test_restart_reconciliation_fails_closed_on_corrupt_or_duplicate_journal(exchange):
    _, calls = exchange
    path = demo._journal_path()
    record = journal_record(1)
    record['legs'][1]['request']['clOrdId'] = record['legs'][0]['request']['orderLinkId']
    demo._write_journal(path, record)
    with pytest.raises(demo.DemoBlocked, match='journal'):
        demo.reconcile_pending(confirmed=True, risk_limits=limits())
    assert not calls


def test_process_restart_with_unconfirmed_order_does_not_send_or_retry(exchange, monkeypatch, tmp_path):
    import os
    import subprocess
    monkeypatch.setattr(execution_mode, '_directory', lambda: tmp_path / '.funding-arb')
    execution_mode.set_mode('demo')
    path = demo._journal_path()
    record = journal_record(0)
    record['state'] = 'leg_1_submitting'
    demo._write_journal(path, record)
    original = path.read_bytes()
    env = os.environ.copy()
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), PYTHONPATH=str(ROOT / 'scripts'),
               FARB_ENABLE_DEMO_ORDERS='1', FARB_DEMO_CREDENTIAL_STORE='age')
    for name in ('FARB_DRY_RUN', 'FARB_LIVE', 'DCA_LIVE', 'BYBIT_REQUIRE_TESTNET'):
        env.pop(name, None)
    code = '''import urllib.request
from execution.demo_futures import reconcile_pending
from execution.demo_futures_preflight import RiskLimits
urllib.request.build_opener = lambda *a: (_ for _ in ()).throw(AssertionError("network forbidden"))
risk = RiskLimits(10000,60000,30000,600000,5000,"10000","3","1","0.001","0.001","0.1","0")
result = reconcile_pending(confirmed=True, risk_limits=risk)
assert result == {"state":"manual_reconciliation_required","confirmed_legs":0,"confirmed_leg_evidence_checked":False}
print("PASS")
'''
    result = subprocess.run([sys.executable, '-c', code], env=env, text=True, capture_output=True, check=True, timeout=10)
    assert result.stdout.strip() == 'PASS' and path.read_bytes() == original
    assert not exchange[1]


def test_paginated_bybit_executions_are_combined_before_confirmation(exchange, monkeypatch):
    responses, calls = exchange
    add_reconciliation_fixtures(responses)
    original = data.read
    first = {'orderLinkId': 'bybitclient', 'orderId': 'bybitorder', 'symbol': 'BTCUSDT',
             'side': 'Buy', 'execId': 'exec1', 'execQty': '0.01'}
    second = dict(first, execId='exec2', execQty='0.008')
    seen = []
    def paged(venue, base, resource, *, cursor=None):
        if venue == 'bybit' and resource == 'fills':
            seen.append(cursor)
            payload = bybit({'list': [first] if cursor is None else [second],
                             'nextPageCursor': 'next-page' if cursor is None else ''})
            return data.ReadResult(payload, NOW)
        return original(venue, base, resource)
    monkeypatch.setattr(data, 'read', paged)
    demo._reconcile_step(pair(), confirmed_legs(), 1, limits())
    assert seen == [None, 'next-page'] and all(req.method == 'GET' for req in calls)


def test_duplicate_execution_across_pages_and_repeated_cursor_block(exchange, monkeypatch):
    responses, _ = exchange
    add_reconciliation_fixtures(responses)
    original = data.read
    record = {'orderLinkId': 'bybitclient', 'orderId': 'bybitorder', 'symbol': 'BTCUSDT',
              'side': 'Buy', 'execId': 'duplicate', 'execQty': '0.009'}
    def duplicate(venue, base, resource, *, cursor=None):
        if venue == 'bybit' and resource == 'fills':
            return data.ReadResult(bybit({'list': [record], 'nextPageCursor': 'next-page' if cursor is None else ''}), NOW)
        return original(venue, base, resource)
    monkeypatch.setattr(data, 'read', duplicate)
    with pytest.raises(demo.ReconciliationRequired):
        demo._reconcile_step(pair(), confirmed_legs(), 1, limits())
    def repeated(venue, base, resource, *, cursor=None):
        if venue == 'bybit' and resource == 'fills':
            return data.ReadResult(bybit({'list': [record], 'nextPageCursor': 'same-cursor'}), NOW)
        return original(venue, base, resource)
    monkeypatch.setattr(data, 'read', repeated)
    with pytest.raises(demo.ReconciliationRequired):
        demo._reconcile_step(pair(), confirmed_legs(), 1, limits())


def test_okx_pagination_uses_last_bill_id(exchange, monkeypatch):
    _, calls = exchange
    pages = []
    def read_page(venue, base, resource, *, cursor=None):
        assert (venue, resource) == ('okx', 'fills')
        pages.append(cursor)
        batch = ([{'billId': str(1000 - index), 'tradeId': str(1000 - index)} for index in range(100)] if cursor is None
                 else [{'billId': '899', 'tradeId': '899'}])
        return data.ReadResult(okx(batch), NOW)
    monkeypatch.setattr(data, 'read', read_page)
    rows, evidence, _ = data._all_fills('okx', 'BTC')
    assert pages == [None, '901'] and len(rows) == 101 and len(evidence) == 2
    assert not calls


def test_unknown_pagination_token_never_reaches_network(exchange):
    _, calls = exchange
    with pytest.raises(data.DemoDataBlocked, match='cursor'):
        data.read('okx', 'BTC', 'fills', cursor='https://api.example.invalid')
    with pytest.raises(data.DemoDataBlocked, match='cursor'):
        data.read('bybit', 'BTC', 'wallet', cursor='anything')
    assert not calls


def add_order_lookup_fixtures(responses, *, venue='bybit', status='filled'):
    if venue == 'bybit':
        row = {'orderId': 'bybitorder', 'orderLinkId': 'bybitclient', 'symbol': 'BTCUSDT', 'side': 'Buy',
               'orderStatus': 'Filled' if status == 'filled' else status, 'cumExecQty': '0.018', 'avgPrice': '100000'}
        responses[('bybit', '/v5/order/realtime')] = bybit({'list': [row], 'nextPageCursor': ''})
        responses[('bybit', '/v5/order/history')] = bybit({'list': [copy.deepcopy(row)], 'nextPageCursor': ''})
    else:
        row = {'ordId': 'okxorder', 'clOrdId': 'okxclient', 'instId': 'BTC-USDT-SWAP', 'side': 'sell',
               'state': status, 'accFillSz': '1.8', 'avgPx': '100000'}
        responses[('okx', '/api/v5/trade/order')] = okx([row])


def test_lookup_by_client_and_exchange_id_is_demo_only(exchange):
    responses, calls = exchange
    add_order_lookup_fixtures(responses)
    fields = confirmed_legs()[0]['request']
    result = data.lookup_confirmation('bybit', 'BTC', fields, order_id='bybitorder', max_age_ms=10_000)
    assert result['order_id'] == 'bybitorder' and len(calls) == 4
    queries = [parse_qs(urlsplit(req.full_url).query) for req in calls]
    assert sum('orderLinkId' in query for query in queries) == 2
    assert sum('orderId' in query for query in queries) == 2
    assert all(urlsplit(req.full_url).hostname == 'api-demo.bybit.com' and req.method == 'GET' for req in calls)


def test_lookup_okx_by_both_ids_always_uses_demo_header(exchange):
    responses, calls = exchange
    add_order_lookup_fixtures(responses, venue='okx')
    fields = confirmed_legs()[1]['request']
    result = data.lookup_confirmation('okx', 'BTC', fields, order_id='okxorder', max_age_ms=10_000)
    assert result['order_id'] == 'okxorder' and len(calls) == 2
    assert {next(iter((set(parse_qs(urlsplit(req.full_url).query)) & {'clOrdId', 'ordId'}))) for req in calls} == {'clOrdId', 'ordId'}
    assert all({k.lower(): v for k, v in req.header_items()}['x-simulated-trading'] == '1' for req in calls)


@pytest.mark.parametrize('bad', ['New', 'PartiallyFilled', 'Cancelled'])
def test_lookup_never_treats_nonfilled_order_as_confirmed(exchange, bad):
    responses, calls = exchange
    add_order_lookup_fixtures(responses, status=bad)
    with pytest.raises(data.DemoDataBlocked):
        data.lookup_confirmation('bybit', 'BTC', confirmed_legs()[0]['request'], order_id=None, max_age_ms=10_000)
    assert all(req.method == 'GET' for req in calls)


def test_lookup_rejects_duplicate_orders_and_incomplete_history(exchange):
    responses, _ = exchange
    add_order_lookup_fixtures(responses)
    listing = responses[('bybit', '/v5/order/history')]['result']
    listing['list'].append(dict(listing['list'][0], orderId='other'))
    with pytest.raises(data.DemoDataBlocked):
        data.lookup_confirmation('bybit', 'BTC', confirmed_legs()[0]['request'], order_id=None, max_age_ms=10_000)
    listing['list'].pop()
    listing['nextPageCursor'] = 'further-history'
    with pytest.raises(data.DemoDataBlocked):
        data.lookup_confirmation('bybit', 'BTC', confirmed_legs()[0]['request'], order_id=None, max_age_ms=10_000)


def test_lookup_follows_order_history_pages_for_both_identifiers(exchange, monkeypatch):
    responses, calls = exchange
    add_order_lookup_fixtures(responses)
    row = responses[('bybit', '/v5/order/history')]['result']['list'][0]
    original = data.read
    pages = []
    def paged(venue, base, resource, **kwargs):
        if resource == 'lookup_history':
            pages.append((kwargs.get('client_id'), kwargs.get('order_id'), kwargs.get('cursor')))
            if kwargs.get('cursor') is None:
                return data.ReadResult(bybit({'list': [], 'nextPageCursor': 'page-2'}), NOW)
            assert kwargs['cursor'] == 'page-2'
            return data.ReadResult(bybit({'list': [row], 'nextPageCursor': ''}), NOW)
        return original(venue, base, resource, **kwargs)
    monkeypatch.setattr(data, 'read', paged)
    result = data.lookup_confirmation('bybit', 'BTC', confirmed_legs()[0]['request'],
                                      order_id='bybitorder', max_age_ms=10_000)
    assert result['order_id'] == 'bybitorder'
    assert pages == [('bybitclient', None, None), ('bybitclient', None, 'page-2'),
                     (None, 'bybitorder', None), (None, 'bybitorder', 'page-2')]
    assert all(req.method == 'GET' for req in calls)


def test_history_cursor_is_sent_to_demo_endpoint_with_id(exchange):
    _, calls = exchange
    data.read('bybit', 'BTC', 'lookup_history', client_id='bybitclient', cursor='page-2')
    req = calls[-1]
    assert urlsplit(req.full_url).hostname == 'api-demo.bybit.com'
    assert parse_qs(urlsplit(req.full_url).query)['cursor'] == ['page-2']
    assert parse_qs(urlsplit(req.full_url).query)['orderLinkId'] == ['bybitclient']


def test_restart_can_check_missing_ack_by_client_without_retry_or_clearing(exchange):
    responses, calls = exchange
    add_reconciliation_fixtures(responses)
    add_order_lookup_fixtures(responses)
    # Reconciliation checks active orders separately; return an empty active
    # list while the client-ID lookup returns the historical filled order.
    original = data.read
    def route(venue, base, resource, **kwargs):
        if venue == 'bybit' and resource == 'orders':
            return data.ReadResult(bybit({'list': [], 'nextPageCursor': ''}), NOW)
        return original(venue, base, resource, **kwargs)
    from pytest import MonkeyPatch
    patch = MonkeyPatch()
    patch.setattr(data, 'read', route)
    try:
        path = demo._journal_path()
        record = journal_record(0)
        record['state'] = 'leg_1_submitting'
        demo._write_journal(path, record)
        before = path.read_bytes()
        result = demo.reconcile_pending(confirmed=True, risk_limits=limits())
        assert result == {'state': 'manual_reconciliation_required', 'confirmed_legs': 1, 'confirmed_leg_evidence_checked': True}
        assert path.read_bytes() == before and all(req.method == 'GET' for req in calls)
        assert any('orderLinkId' in parse_qs(urlsplit(req.full_url).query) for req in calls)
    finally:
        patch.undo()


def close_legs():
    return [
        {'venue': 'okx', 'order_id': 'okxcloseorder', 'request': demo._order_fields('okx', 'BTC', 'buy', '1.8', 'okxclose', reduce_only=True),
         'confirmation': {'venue': 'okx', 'order_id': 'okxcloseorder', 'filled_qty': '1.8', 'avg_price': '100000'}},
        {'venue': 'bybit', 'order_id': 'bybitcloseorder', 'request': demo._order_fields('bybit', 'BTC', 'sell', '0.018', 'bybitclose', reduce_only=True),
         'confirmation': {'venue': 'bybit', 'order_id': 'bybitcloseorder', 'filled_qty': '0.018', 'avg_price': '100000'}},
    ]


def close_fixtures(responses, both=False):
    add_reconciliation_fixtures(responses, both=True)
    # First close is OKX short; the other long remains until its close.
    responses[('okx', '/api/v5/account/positions')] = okx([])
    responses[('okx', '/api/v5/trade/fills')] = okx([
        {'clOrdId': 'okxclient', 'ordId': 'okxorder', 'instId': 'BTC-USDT-SWAP', 'side': 'sell', 'tradeId': 'openokx', 'fillSz': '1.8'},
        {'clOrdId': 'okxclose', 'ordId': 'okxcloseorder', 'instId': 'BTC-USDT-SWAP', 'side': 'buy', 'tradeId': 'closeokx', 'fillSz': '1.8'},
    ])
    if both:
        responses[('bybit', '/v5/position/list')]['result']['list'][0].update(size='0', side='')
        responses[('bybit', '/v5/execution/list')]['result']['list'].append({
            'orderLinkId': 'bybitclose', 'orderId': 'bybitcloseorder', 'symbol': 'BTCUSDT',
            'side': 'Sell', 'execId': 'closebybit', 'execQty': '0.018'})


def test_close_reconciliation_checks_reduce_only_and_residuals(exchange):
    responses, calls = exchange
    opening = journal_record(2)['legs']
    closing = close_legs()
    close_fixtures(responses)
    data.verify_close_step(pair(), opening, closing, 1, max_age_ms=10_000)
    close_fixtures(responses, both=True)
    data.verify_close_step(pair(), opening, closing, 2, max_age_ms=10_000)
    assert all(req.method == 'GET' for req in calls)


@pytest.mark.parametrize('mutate', [
    lambda f: f[('okx', '/api/v5/account/positions')]['data'].append({'instId': 'BTC-USDT-SWAP', 'posSide': 'net', 'pos': '-0.9'}),
    lambda f: f[('okx', '/api/v5/trade/fills')]['data'][1].update(fillSz='0.9'),
    lambda f: f[('okx', '/api/v5/trade/fills')]['data'][1].update(side='sell'),
    lambda f: f[('bybit', '/v5/position/list')]['result']['list'][0].update(size='0.009'),
    lambda f: f[('okx', '/api/v5/trade/orders-algo-pending')]['data'].append({'algoId': 'open'}),
])
def test_close_partial_unknown_or_residual_positions_remain_blocked(exchange, mutate):
    responses, calls = exchange
    close_fixtures(responses)
    mutate(responses)
    with pytest.raises(data.DemoDataBlocked):
        data.verify_close_step(pair(), journal_record(2)['legs'], close_legs(), 1, max_age_ms=10_000)
    assert all(req.method == 'GET' for req in calls)


def test_close_pair_cannot_bypass_preflight_with_open_positions(exchange):
    responses, calls = exchange
    add_reconciliation_fixtures(responses, both=True)
    record = journal_record(2)
    record['state'] = 'position_reconciliation_required'
    path = demo._journal_path()
    demo._write_journal(path, record)
    before = path.read_bytes()
    with pytest.raises(demo.DemoBlocked):
        demo.close_pair(confirmed=True, close_confirmed=True, risk_limits=limits())
    assert path.read_bytes() == before and all(req.method == 'GET' for req in calls)


def test_close_restart_remains_manual_and_never_retries(exchange):
    from execution.demo_futures_recovery import inspect_journal
    _, calls = exchange
    record = journal_record(2)
    record['state'] = 'close_1_submitting'
    record['closing'] = close_legs()
    path = demo._journal_path()
    demo._write_journal(path, record)
    before = path.read_bytes()
    assert inspect_journal().state == 'close_1_submitting'
    result = demo.reconcile_pending(confirmed=True, risk_limits=limits())
    assert result['state'] == 'manual_reconciliation_required' and not result['confirmed_leg_evidence_checked']
    assert path.read_bytes() == before and all(req.method == 'GET' for req in calls)


def test_close_restart_reads_both_fills_and_zero_positions_without_clearing(exchange, monkeypatch):
    responses, calls = exchange
    close_fixtures(responses, both=True)
    record = journal_record(2)
    record['state'] = 'close_2_submitting'
    record['closing'] = close_legs()
    record['closing'][1].pop('order_id')
    record['closing'][1].pop('confirmation')
    path = demo._journal_path()
    demo._write_journal(path, record)
    original_journal = path.read_bytes()
    original_read = data.read
    lookups = []
    def route(venue, base, resource, **kwargs):
        if resource.startswith('lookup_'):
            lookups.append((venue, resource, kwargs))
            if venue == 'bybit':
                row = {'orderId': 'bybitcloseorder', 'orderLinkId': 'bybitclose', 'symbol': 'BTCUSDT',
                       'side': 'Sell', 'orderStatus': 'Filled', 'cumExecQty': '0.018',
                       'avgPrice': '100000', 'reduceOnly': True}
                return data.ReadResult(bybit({'list': [row], 'nextPageCursor': ''}), NOW)
            row = {'ordId': 'okxcloseorder', 'clOrdId': 'okxclose', 'instId': 'BTC-USDT-SWAP',
                   'side': 'buy', 'state': 'filled', 'accFillSz': '1.8',
                   'avgPx': '100000', 'reduceOnly': True}
            return data.ReadResult(okx([row]), NOW)
        return original_read(venue, base, resource, **kwargs)
    monkeypatch.setattr(data, 'read', route)
    result = demo.reconcile_pending(confirmed=True, risk_limits=limits())
    assert result['state'] == 'manual_reconciliation_required'
    assert result['confirmed_closes'] == 2 and result['close_evidence_checked'] is True
    assert any(item[0] == 'bybit' and item[2].get('client_id') == 'bybitclose' for item in lookups)
    assert path.read_bytes() == original_journal and all(req.method == 'GET' for req in calls)


def test_close_restart_rejects_changed_reduce_only_intent_before_lookup(exchange):
    _, calls = exchange
    record = journal_record(2)
    record['state'] = 'close_1_submitting'
    record['closing'] = close_legs()
    record['closing'][0]['request'].pop('reduceOnly')
    path = demo._journal_path()
    demo._write_journal(path, record)
    before = path.read_bytes()
    with pytest.raises(demo.DemoBlocked):
        demo.reconcile_pending(confirmed=True, risk_limits=limits())
    assert path.read_bytes() == before and not calls


@pytest.mark.parametrize('state', ['partially_filled', 'live', 'canceled'])
def test_close_restart_unconfirmed_order_blocks_and_preserves_journal(exchange, state):
    responses, calls = exchange
    record = journal_record(2)
    record['state'] = 'close_1_submitting'
    record['closing'] = close_legs()
    record['closing'][0].pop('confirmation')
    record['closing'][0].pop('order_id')
    responses[('okx', '/api/v5/trade/order')] = okx([{
        'ordId': 'okxcloseorder', 'clOrdId': 'okxclose', 'instId': 'BTC-USDT-SWAP',
        'side': 'buy', 'state': state, 'accFillSz': '0.9', 'avgPx': '100000', 'reduceOnly': True,
    }])
    path = demo._journal_path()
    demo._write_journal(path, record)
    before = path.read_bytes()
    result = demo.reconcile_pending(confirmed=True, risk_limits=limits())
    assert result['state'] == 'manual_reconciliation_required'
    assert result['reason'] == 'order_unconfirmed' and result['confirmed_closes'] == 0
    assert path.read_bytes() == before and calls and all(req.method == 'GET' for req in calls)


def test_close_restart_lost_exchange_response_stays_blocked(exchange, monkeypatch):
    _, calls = exchange
    record = journal_record(2)
    record['state'] = 'close_1_submitting'
    record['closing'] = close_legs()
    path = demo._journal_path()
    demo._write_journal(path, record)
    before = path.read_bytes()
    def unavailable(*args, **kwargs):
        raise data.DemoDataBlocked('exchange unavailable')
    monkeypatch.setattr(data, 'read', unavailable)
    result = demo.reconcile_pending(confirmed=True, risk_limits=limits())
    assert result['state'] == 'manual_reconciliation_required' and result['reason'] == 'order_unconfirmed'
    assert path.read_bytes() == before and not calls


def test_close_restart_in_new_process_does_not_send_or_change_journal(exchange, monkeypatch, tmp_path):
    import os
    import subprocess
    monkeypatch.setattr(execution_mode, '_directory', lambda: tmp_path / '.funding-arb')
    execution_mode.set_mode('demo')
    record = journal_record(2)
    record['state'] = 'close_1_submitting'
    record['closing'] = close_legs()
    path = demo._journal_path()
    demo._write_journal(path, record)
    original = path.read_bytes()
    env = os.environ.copy()
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), PYTHONPATH=str(ROOT / 'scripts'),
               FARB_ENABLE_DEMO_ORDERS='1', FARB_DEMO_CREDENTIAL_STORE='age')
    for name in ('FARB_DRY_RUN', 'FARB_LIVE', 'DCA_LIVE', 'BYBIT_REQUIRE_TESTNET'):
        env.pop(name, None)
    code = '''import urllib.request
from execution.demo_futures import reconcile_pending
from execution.demo_futures_preflight import RiskLimits
urllib.request.build_opener = lambda *a: (_ for _ in ()).throw(AssertionError("network forbidden"))
risk = RiskLimits(10000,60000,30000,600000,5000,"10000","3","1","0.001","0.001","0.1","0")
result = reconcile_pending(confirmed=True, risk_limits=risk)
assert result["state"] == "manual_reconciliation_required"
assert result["confirmed_leg_evidence_checked"] is False
print("PASS")
'''
    result = subprocess.run([sys.executable, '-c', code], env=env, text=True, capture_output=True, check=True, timeout=10)
    assert result.stdout.strip() == 'PASS' and path.read_bytes() == original
    assert not exchange[1]


def test_close_reconciliation_rejects_mismatched_recorded_order_id(exchange):
    responses, calls = exchange
    close_fixtures(responses)
    closing = close_legs()
    closing[0]['order_id'] = 'otherorder'
    with pytest.raises(data.DemoDataBlocked):
        data.verify_close_step(pair(), journal_record(2)['legs'], closing, 1, max_age_ms=10_000)
    assert all(req.method == 'GET' for req in calls)
