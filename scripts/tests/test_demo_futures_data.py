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
    execution_mode.set_demo_orders_enabled(True, confirmed=True)
    monkeypatch.setattr(demo_credentials, '_backend', lambda: 'age')
    monkeypatch.setattr(demo_credentials, 'load', lambda venue, environment: {
        'key': venue + '-dummy-key', 'secret': venue + '-dummy-secret', 'passphrase': 'dummy-pass'})
    monkeypatch.setenv('FARB_ENABLE_DEMO_ORDERS', '1')
    for name in ('FARB_DRY_RUN', 'FARB_LIVE', 'DCA_LIVE', 'BYBIT_REQUIRE_TESTNET', 'BYBIT_API_KEY', 'BYBIT_TESTNET_API_KEY', 'OKX_API_KEY'):
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


def test_technical_open_needs_no_funding_or_fee_history(exchange):
    responses, calls = exchange
    del responses[('okx', '/api/v5/public/funding-rate')]
    del responses[('okx', '/api/v5/account/trade-fee')]
    del responses[('bybit', '/v5/account/fee-rate')]
    data.verify_technical_open(pair(), max_age_ms=10_000)
    assert calls and all(req.method == 'GET' for req in calls)
    assert not any(urlsplit(req.full_url).path in ('/v5/execution/list', '/api/v5/trade/fills') for req in calls)


def test_demo_pair_sizing_uses_common_contract_step_and_never_exceeds_notional(exchange):
    _, calls = exchange
    sized = data.pair_for_notional('BTC', 'bybit', 'okx', '2000', max_age_ms=10_000)
    assert sized == pair()
    assert all(req.method == 'GET' for req in calls)


def test_demo_pair_sizing_rejects_unknown_venue_or_too_small_notional(exchange):
    _, calls = exchange
    with pytest.raises(data.DemoDataBlocked):
        data.pair_for_notional('BTC', 'bybit', 'bitget', '2000', max_age_ms=10_000)
    assert not calls
    with pytest.raises(data.DemoDataBlocked):
        data.pair_for_notional('BTC', 'bybit', 'okx', '1', max_age_ms=10_000)
    assert all(req.method == 'GET' for req in calls)


@pytest.mark.parametrize('mutate', [
    lambda f: f[('okx', '/api/v5/account/balance')]['data'][0]['details'][0].update(availEq='1'),
    lambda f: f[('bybit', '/v5/account/wallet-balance')]['result']['list'][0].update(totalAvailableBalance='0'),
    lambda f: f[('bybit', '/v5/position/list')]['result']['list'][0].update(size='0.001'),
    lambda f: f[('okx', '/api/v5/trade/orders-pending')]['data'].append({'ordId': 'unknown'}),
    lambda f: f[('okx', '/api/v5/public/instruments')]['data'][0].update(ctVal='0.1'),
    lambda f: f[('bybit', '/v5/market/instruments-info')]['result']['list'][0]['lotSizeFilter'].update(qtyStep='0.007'),
    lambda f: f[('okx', '/api/v5/public/mark-price')]['data'][0].update(ts=str(NOW-11_000)),
])
def test_technical_open_blocks_unsafe_accounts_and_instruments(exchange, mutate):
    responses, calls = exchange
    mutate(responses)
    with pytest.raises(data.DemoDataBlocked):
        data.verify_technical_open(pair(), max_age_ms=10_000)
    assert all(req.method == 'GET' for req in calls)


def test_preflight_collects_both_demo_venues(exchange):
    _, calls = exchange
    demo._preflight(pair(), risk_limits=limits())
    assert len(calls) == 20 and all(req.method == 'GET' for req in calls)
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
    lambda f: f[('okx', '/api/v5/public/mark-price')]['data'][0].update(ts=str(NOW-11_000)),
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
    data.verify_step(pair(), confirmed_legs(), 1, max_age_ms=10_000)
    assert len(calls) == 12 and all(req.method == 'GET' for req in calls)
    assert any(urlsplit(req.full_url).path == '/v5/execution/list' for req in calls)
    assert any(urlsplit(req.full_url).path == '/api/v5/account/positions' for req in calls)


def test_reconciliation_checks_both_futures_legs(exchange):
    responses, calls = exchange
    add_reconciliation_fixtures(responses, both=True)
    data.verify_step(pair(), confirmed_legs(), 2, max_age_ms=10_000)
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
    with pytest.raises(data.DemoDataBlocked):
        data.verify_step(pair(), confirmed_legs(), 1, max_age_ms=10_000)
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


def test_normal_reconciliation_uses_full_order_confirmation_without_execution_history(exchange):
    responses, calls = exchange
    add_reconciliation_fixtures(responses)
    del responses[('bybit', '/v5/execution/list')]
    demo._reconcile_step(pair(), journal_record(1)['legs'], 1, limits())
    assert all(req.method == 'GET' for req in calls)
    assert not any(urlsplit(req.full_url).path == '/v5/execution/list' for req in calls)


@pytest.mark.parametrize('change', [
    lambda row: row.update(filled_qty='0.009'),
    lambda row: row.update(avg_price='0'),
    lambda row: row.update(order_id=''),
])
def test_normal_reconciliation_blocks_unconfirmed_order(exchange, change):
    responses, _ = exchange
    add_reconciliation_fixtures(responses)
    legs = journal_record(1)['legs']
    change(legs[0]['confirmation'])
    with pytest.raises(demo.ReconciliationRequired):
        demo._reconcile_step(pair(), legs, 1, limits())


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
    execution_mode.set_demo_orders_enabled(True, confirmed=True)
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
    data.verify_step(pair(), confirmed_legs(), 1, max_age_ms=10_000)
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
    with pytest.raises(data.DemoDataBlocked):
        data.verify_step(pair(), confirmed_legs(), 1, max_age_ms=10_000)
    def repeated(venue, base, resource, *, cursor=None):
        if venue == 'bybit' and resource == 'fills':
            return data.ReadResult(bybit({'list': [record], 'nextPageCursor': 'same-cursor'}), NOW)
        return original(venue, base, resource)
    monkeypatch.setattr(data, 'read', repeated)
    with pytest.raises(data.DemoDataBlocked):
        data.verify_step(pair(), confirmed_legs(), 1, max_age_ms=10_000)


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


def unsubmitted_close_legs():
    legs = close_legs()
    for leg in legs:
        leg.pop('order_id')
        leg.pop('confirmation')
    return legs


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


def test_close_preflight_accepts_verified_open_positions(exchange):
    responses, calls = exchange
    add_reconciliation_fixtures(responses, both=True)
    record = journal_record(2)
    record['state'] = 'position_reconciliation_required'
    path = demo._journal_path()
    demo._write_journal(path, record)
    before = path.read_bytes()
    demo._preflight_close(pair(), record['legs'], close_legs(), risk_limits=limits())
    assert path.read_bytes() == before and all(req.method == 'GET' for req in calls)
    paths = {urlsplit(req.full_url).path for req in calls}
    assert '/v5/market/instruments-info' in paths and '/api/v5/public/instruments' in paths
    assert '/v5/position/list' in paths and '/api/v5/account/positions' in paths


def test_close_preflight_supports_reversed_futures_directions(exchange):
    responses, calls = exchange
    add_reconciliation_fixtures(responses, both=True)
    responses[('bybit', '/v5/position/list')]['result']['list'][0]['side'] = 'Sell'
    responses[('bybit', '/v5/execution/list')]['result']['list'][0]['side'] = 'Sell'
    responses[('okx', '/api/v5/account/positions')]['data'][0]['pos'] = '1.8'
    responses[('okx', '/api/v5/trade/fills')]['data'][0]['side'] = 'buy'
    record = journal_record(2)
    record['legs'].reverse()
    record['legs'][0]['request']['side'] = 'buy'
    record['legs'][1]['request']['side'] = 'Sell'
    record['state'] = 'position_reconciliation_required'
    path = demo._journal_path()
    demo._write_journal(path, record)
    before = path.read_bytes()
    reversed_pair = demo.FuturesPair('BTC', 'okx', 'bybit', '0.018', '1.8', '0.01')
    closing = [
        {'venue': 'bybit', 'request': demo._order_fields('bybit', 'BTC', 'buy', '0.018', 'newbybit', reduce_only=True)},
        {'venue': 'okx', 'request': demo._order_fields('okx', 'BTC', 'sell', '1.8', 'newokx', reduce_only=True)},
    ]
    demo._preflight_close(reversed_pair, record['legs'], closing, risk_limits=limits())
    assert path.read_bytes() == before and all(req.method == 'GET' for req in calls)


def test_close_preflight_rechecks_position_freshness_after_market_reads(exchange, monkeypatch):
    responses, calls = exchange
    add_reconciliation_fixtures(responses, both=True)
    original = data.verify_close_instruments
    def slow_market(*args, **kwargs):
        earliest = original(*args, **kwargs)
        monkeypatch.setattr(demo, 'time', SimpleNamespace(time=lambda: (NOW + 10_000) / 1000))
        return earliest
    monkeypatch.setattr(data, 'verify_close_instruments', slow_market)
    with pytest.raises(demo.DemoBlocked, match='could not verify'):
        demo._preflight_close(pair(), journal_record(2)['legs'], close_legs(), risk_limits=limits())
    assert all(req.method == 'GET' for req in calls)


@pytest.mark.parametrize('mutate', [
    lambda f: f[('bybit', '/v5/market/instruments-info')]['result']['list'][0].update(status='Settled'),
    lambda f: f[('okx', '/api/v5/public/instruments')]['data'][0].update(state='suspend'),
    lambda f: f[('okx', '/api/v5/public/instruments')]['data'][0].update(ctVal='0.1'),
    lambda f: f[('bybit', '/v5/market/instruments-info')]['result']['list'][0]['lotSizeFilter'].update(qtyStep='0.007'),
    lambda f: f[('okx', '/api/v5/public/instruments')]['data'][0].update(maxMktSz='1'),
    lambda f: f[('okx', '/api/v5/public/mark-price')]['data'][0].update(ts=str(NOW - 11_000)),
    lambda f: f[('okx', '/api/v5/account/positions')]['data'][0].update(pos='1.8'),
])
def test_close_pair_rejects_unsafe_positions_and_instrument_data(exchange, mutate):
    responses, calls = exchange
    add_reconciliation_fixtures(responses, both=True)
    mutate(responses)
    record = journal_record(2)
    record['state'] = 'position_reconciliation_required'
    path = demo._journal_path()
    demo._write_journal(path, record)
    before = path.read_bytes()
    with pytest.raises(demo.DemoBlocked, match='could not verify'):
        demo.close_pair(confirmed=True, close_confirmed=True, risk_limits=limits())
    assert path.read_bytes() == before and all(req.method == 'GET' for req in calls)


def test_close_preflight_does_not_require_book_or_fill_history(exchange):
    responses, calls = exchange
    add_reconciliation_fixtures(responses, both=True)
    del responses[('bybit', '/v5/execution/list')]
    del responses[('okx', '/api/v5/trade/fills')]
    del responses[('bybit', '/v5/market/orderbook')]
    del responses[('okx', '/api/v5/market/books')]
    demo._preflight_close(pair(), journal_record(2)['legs'], close_legs(), risk_limits=limits())
    assert all(req.method == 'GET' for req in calls)


@pytest.mark.parametrize('mutate', [
    lambda legs: legs[0]['request'].pop('reduceOnly'),
    lambda legs: legs[0]['request'].update(side='sell'),
    lambda legs: legs[0]['request'].update(sz='0.9'),
    lambda legs: legs[0]['request'].update(instId='ETH-USDT-SWAP'),
    lambda legs: legs[0]['request'].update(clOrdId='okxclient'),
])
def test_close_preflight_rejects_unsafe_order_intents(exchange, mutate):
    responses, calls = exchange
    add_reconciliation_fixtures(responses, both=True)
    closing = close_legs()
    mutate(closing)
    with pytest.raises(demo.DemoBlocked, match='could not verify'):
        demo._preflight_close(pair(), journal_record(2)['legs'], closing, risk_limits=limits())
    assert all(req.method == 'GET' for req in calls)


def test_close_restart_remains_manual_and_never_retries(exchange):
    from execution.demo_futures_recovery import inspect_journal
    _, calls = exchange
    record = journal_record(2)
    record['state'] = 'close_1_submitting'
    record['closing'] = unsubmitted_close_legs()
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


@pytest.mark.parametrize(('state', 'count'), [
    ('close_prepared', 0),
    ('close_1_submitting', 1), ('close_1_acknowledged', 1),
    ('close_1_filled', 1), ('close_1_reconciled', 1),
    ('close_2_submitting', 2), ('close_2_acknowledged', 2),
    ('close_2_filled', 2), ('close_2_reconciled', 2),
    ('close_reconciliation_required', 2), ('closed_observed_manual_review', 2),
])
def test_close_restart_states_never_retry_or_clear_journal(exchange, monkeypatch, state, count):
    responses, calls = exchange
    close_fixtures(responses, both=count == 2)
    record = journal_record(2)
    record['state'] = state
    record['closing'] = close_legs()
    if count == 0:
        for leg in record['closing']:
            leg.pop('order_id')
            leg.pop('confirmation')
    elif count == 1:
        record['closing'][1].pop('order_id')
        record['closing'][1].pop('confirmation')
        if state == 'close_1_submitting':
            record['closing'][0].pop('order_id')
            record['closing'][0].pop('confirmation')
        elif state == 'close_1_acknowledged':
            record['closing'][0].pop('confirmation')
    elif state == 'close_2_submitting':
        record['closing'][1].pop('order_id')
        record['closing'][1].pop('confirmation')
    elif state == 'close_2_acknowledged':
        record['closing'][1].pop('confirmation')
    path = demo._journal_path()
    demo._write_journal(path, record)
    before = path.read_bytes()
    original_read = data.read
    def route(venue, base, resource, **kwargs):
        if resource.startswith('lookup_'):
            if venue == 'bybit':
                return data.ReadResult(bybit({'list': [{'orderId': 'bybitcloseorder', 'orderLinkId': 'bybitclose',
                    'symbol': 'BTCUSDT', 'side': 'Sell', 'orderStatus': 'Filled', 'cumExecQty': '0.018',
                    'avgPrice': '100000', 'reduceOnly': True}], 'nextPageCursor': ''}), NOW)
            return data.ReadResult(okx([{'ordId': 'okxcloseorder', 'clOrdId': 'okxclose', 'instId': 'BTC-USDT-SWAP',
                'side': 'buy', 'state': 'filled', 'accFillSz': '1.8', 'avgPx': '100000', 'reduceOnly': True}]), NOW)
        return original_read(venue, base, resource, **kwargs)
    monkeypatch.setattr(data, 'read', route)
    result = demo.reconcile_pending(confirmed=True, risk_limits=limits())
    assert result['state'] == 'manual_reconciliation_required' and result['confirmed_closes'] == count
    assert result['close_evidence_checked'] is (count > 0)
    assert path.read_bytes() == before and all(req.method == 'GET' for req in calls)


def test_close_restart_rejects_changed_reduce_only_intent_before_lookup(exchange):
    _, calls = exchange
    record = journal_record(2)
    record['state'] = 'close_1_submitting'
    record['closing'] = unsubmitted_close_legs()
    record['closing'][0]['request'].pop('reduceOnly')
    path = demo._journal_path()
    demo._write_journal(path, record)
    before = path.read_bytes()
    with pytest.raises(demo.DemoBlocked):
        demo.reconcile_pending(confirmed=True, risk_limits=limits())
    assert path.read_bytes() == before and not calls


@pytest.mark.parametrize('state', ['close_prepared', 'close_1_submitting', 'close_1_acknowledged'])
def test_close_restart_rejects_impossible_second_acknowledgement(exchange, state):
    _, calls = exchange
    record = journal_record(2)
    record['state'] = state
    record['closing'] = unsubmitted_close_legs()
    record['closing'][1]['order_id'] = 'impossibleack'
    path = demo._journal_path()
    demo._write_journal(path, record)
    before = path.read_bytes()
    with pytest.raises(demo.DemoBlocked, match='Closing journal'):
        demo.reconcile_pending(confirmed=True, risk_limits=limits())
    assert path.read_bytes() == before and not calls


@pytest.mark.parametrize('state', ['partially_filled', 'live', 'canceled'])
def test_close_restart_unconfirmed_order_blocks_and_preserves_journal(exchange, state):
    responses, calls = exchange
    record = journal_record(2)
    record['state'] = 'close_1_submitting'
    record['closing'] = unsubmitted_close_legs()
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
    record['closing'] = unsubmitted_close_legs()
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
    execution_mode.set_demo_orders_enabled(True, confirmed=True)
    record = journal_record(2)
    record['state'] = 'close_1_submitting'
    record['closing'] = unsubmitted_close_legs()
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


class LifecycleExchange:
    """In-memory exchange: the only object allowed to receive test requests."""
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.orders = {}
        self.fail_post = None
        self.reject_post = None
        self.partial = None
        self.cancel_timeout = False
        self.terminal = {}

    def open(self, request, timeout):
        from urllib.parse import urlsplit, parse_qs
        url = urlsplit(request.full_url)
        venue = {'api-demo.bybit.com': 'bybit', 'www.okx.com': 'okx'}.get(url.hostname)
        assert venue in ('bybit', 'okx') and url.scheme == 'https'
        if venue == 'okx':
            assert {k.lower(): v for k, v in request.header_items()}['x-simulated-trading'] == '1'
        self.calls.append(request)
        query = parse_qs(url.query)
        if request.method == 'POST':
            assert url.path in ('/v5/order/create', '/api/v5/trade/order', '/v5/order/cancel', '/api/v5/trade/cancel-order')
            if url.path in ('/v5/order/cancel', '/api/v5/trade/cancel-order'):
                if self.cancel_timeout:
                    raise TimeoutError('mocked cancel timeout')
                class Response:
                    def __enter__(self): return self
                    def __exit__(self, *_): return False
                    def read(self, limit): return json.dumps({'retCode': 0} if venue == 'bybit' else {'code': '0'}).encode()
                return Response()
            if self.fail_post == venue:
                raise TimeoutError('mocked transport timeout')
            if self.reject_post == venue:
                class Response:
                    def __enter__(self): return self
                    def __exit__(self, *_): return False
                    def read(self, limit): return json.dumps({'retCode': 10001} if venue == 'bybit' else {'code': '1'}).encode()
                return Response()
            fields = json.loads(request.data)
            order_id = str(len(self.orders) + 1)
            self.orders[order_id] = (venue, fields)
            is_close = fields.get('reduceOnly') is True
            if venue == 'bybit':
                pos = self.responses[('bybit', '/v5/position/list')]['result']['list'][0]
                pos.update(size='0' if is_close else fields['qty'], side='' if is_close else fields['side'])
                payload = {'retCode': 0, 'result': {'orderId': order_id}}
            else:
                rows = [] if is_close else [{'instId': fields['instId'], 'posSide': 'net', 'mgnMode': 'cross',
                                              'pos': ('-' if fields['side'] == 'sell' else '') + fields['sz']}]
                self.responses[('okx', '/api/v5/account/positions')] = okx(rows)
                payload = {'code': '0', 'data': [{'sCode': '0', 'ordId': order_id}]}
        elif venue == 'bybit' and url.path in ('/v5/order/realtime', '/v5/order/history') and 'orderLinkId' in query and venue in self.terminal:
            client = query['orderLinkId'][0]
            order_id, (_, fields) = next((oid, item) for oid, item in self.orders.items()
                                         if item[0] == venue and item[1]['orderLinkId'] == client)
            payload = bybit({'list': [{'orderId': order_id, 'orderLinkId': client, 'symbol': fields['symbol'],
                                       'side': fields['side'], 'orderStatus': self.terminal[venue]}], 'nextPageCursor': ''})
        elif (venue == 'bybit' and url.path == '/v5/order/realtime' and 'orderId' in query) or (
                venue == 'okx' and url.path == '/api/v5/trade/order' and 'ordId' in query):
            order_id = query['orderId' if venue == 'bybit' else 'ordId'][0]
            actual, fields = self.orders[order_id]
            assert actual == venue
            partial = self.partial == venue
            if venue == 'bybit':
                payload = bybit({'list': [{'orderId': order_id, 'orderLinkId': fields['orderLinkId'],
                    'symbol': fields['symbol'], 'side': fields['side'], 'orderStatus': 'PartiallyFilled' if partial else 'Filled',
                    'cumExecQty': '0.001' if partial else fields['qty'], 'avgPrice': '100000',
                    'reduceOnly': fields.get('reduceOnly', False)}]})
            else:
                payload = okx([{'ordId': order_id, 'clOrdId': fields['clOrdId'], 'instId': fields['instId'],
                    'side': fields['side'], 'state': 'partially_filled' if partial else 'filled',
                    'accFillSz': '0.3' if partial else fields['sz'], 'avgPx': '100000',
                    'reduceOnly': 'true' if fields.get('reduceOnly') is True else 'false'}])
        else:
            path = url.path
            if venue == 'bybit' and path == '/v5/order/realtime' and query.get('orderFilter') == ['StopOrder']:
                path += '#conditional'
            payload = self.responses[(venue, path)]
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, limit): return json.dumps(payload).encode()
        return Response()


def test_integrated_open_confirm_close_with_no_preflight_or_transport_bypass(exchange, monkeypatch):
    responses, _ = exchange
    for key in (('okx', '/api/v5/public/funding-rate'), ('okx', '/api/v5/account/trade-fee'),
                ('bybit', '/v5/account/fee-rate'), ('bybit', '/v5/market/orderbook'), ('okx', '/api/v5/market/books')):
        responses.pop(key)
    fake = LifecycleExchange(responses)
    monkeypatch.setattr(data.urllib.request, 'build_opener', lambda handler: fake)
    result = demo.execute_pair(pair(), confirmed=True, risk_limits=limits())
    assert result['state'] == 'open_confirmed' and result['confirmed_legs'] == 2
    result = demo.close_pair(confirmed=True, close_confirmed=True, risk_limits=limits())
    assert result['state'] == 'closed_confirmed' and result['confirmed_closes'] == 2
    posted = [(urlsplit(req.full_url).hostname, json.loads(req.data)) for req in fake.calls if req.method == 'POST']
    assert [host for host, _ in posted] == ['api-demo.bybit.com', 'www.okx.com', 'www.okx.com', 'api-demo.bybit.com']
    assert all('reduceOnly' not in fields for _, fields in posted[:2])
    assert all(fields['reduceOnly'] is True for _, fields in posted[2:])
    assert not any(urlsplit(req.full_url).path in ('/v5/execution/list', '/api/v5/trade/fills') for req in fake.calls)
    assert responses[('bybit', '/v5/position/list')]['result']['list'][0]['size'] == '0'
    assert responses[('okx', '/api/v5/account/positions')]['data'] == []
    result = demo.execute_pair(pair(), confirmed=True, risk_limits=limits())
    assert result['state'] == 'open_confirmed'
    assert len(list(demo._journal_path().parent.glob('demo-futures-closed-*.json'))) == 1


def test_pure_futures_demo_signal_runs_full_cycle_offline(exchange, monkeypatch, capsys):
    from execution import run_pure_futures_spread as runner
    from backtest.funding_providers import get_funding_provider
    from cli import pure_futures_trade
    responses, _ = exchange
    fake = LifecycleExchange(responses)
    monkeypatch.setattr(data.urllib.request, 'build_opener', lambda handler: fake)
    monkeypatch.setattr(runner, 'time', SimpleNamespace(time=lambda: NOW / 1000))
    provider = get_funding_provider('okx')
    monkeypatch.setattr(provider, '_any_cache', None)
    monkeypatch.setattr(provider, '_mark_cache', None)
    monkeypatch.setattr(provider, '_index_cache', None)
    with pytest.raises(demo.DemoBlocked):
        runner.run_once({'dry_run': False, 'pureFuturesArbitrage': {'demoBase': 'BTC', 'tradeUsdPerPair': '2000'}})
    cfg = {'dry_run': False, 'demoStrategy': {'trade_usd': '2000', 'min_spread_pct': '0.01', 'min_net_pct': '0.01',
           'exit_spread_pct': '0', 'max_mark_spread_pct': '0.5', 'max_leverage': '3', 'max_fee_pct': '0.1',
           'max_slippage_pct': '0.1', 'max_funding_gap_sec': 600, 'interval_sec': 30}}
    result = runner.run_once(cfg)
    assert result['cycle']['result']['state'] == 'open_confirmed'
    monkeypatch.setattr(sys, 'argv', ['trade', 'demo-positions', 'BTC'])
    assert pure_futures_trade.main() == 0
    displayed = json.loads(capsys.readouterr().out)
    assert displayed['bybit']['positions'][0]['size'] == '0.018'
    assert displayed['okx']['positions'][0]['pos'] == '-1.8'
    monkeypatch.setattr(sys, 'argv', ['trade', 'demo-stop'])
    assert pure_futures_trade.main() == 0
    capsys.readouterr()
    with pytest.raises(demo.DemoBlocked, match='openings'):
        demo.execute_pair(pair(), confirmed=True, risk_limits=limits())
    monkeypatch.setattr(sys, 'argv', ['trade', 'demo-close', '--confirm'])
    assert pure_futures_trade.main() == 0
    assert json.loads(capsys.readouterr().out)['state'] == 'closed_confirmed'
    assert len([req for req in fake.calls if req.method == 'POST']) == 4


def test_demo_cli_mode_enable_and_manual_open(exchange, monkeypatch, capsys):
    from cli import pure_futures_trade
    responses, _ = exchange
    fake = LifecycleExchange(responses)
    monkeypatch.setattr(data.urllib.request, 'build_opener', lambda handler: fake)
    monkeypatch.setattr(sys, 'argv', ['trade', 'demo-mode'])
    assert pure_futures_trade.main() == 0 and json.loads(capsys.readouterr().out)['orders_enabled'] is False
    monkeypatch.setattr(sys, 'argv', ['trade', 'demo-enable', '--confirm'])
    assert pure_futures_trade.main() == 0 and json.loads(capsys.readouterr().out)['orders_enabled'] is True
    monkeypatch.setattr(sys, 'argv', ['trade', 'demo-open', 'BTC', '--long-venue', 'bybit', '--short-venue', 'okx',
                                      '--trade-usd', '2000', '--confirm'])
    assert pure_futures_trade.main() == 0
    assert json.loads(capsys.readouterr().out)['state'] == 'open_confirmed'
    assert len([req for req in fake.calls if req.method == 'POST']) == 2


@pytest.mark.parametrize('failure', ['timeout', 'partial', 'rejected'])
def test_integrated_first_leg_unknown_or_partial_never_submits_second(exchange, monkeypatch, failure):
    responses, _ = exchange
    fake = LifecycleExchange(responses)
    if failure == 'timeout':
        fake.fail_post = 'bybit'
    elif failure == 'partial':
        fake.partial = 'bybit'
    else:
        fake.reject_post = 'bybit'
    monkeypatch.setattr(data.urllib.request, 'build_opener', lambda handler: fake)
    result = demo.execute_pair(pair(), confirmed=True, risk_limits=limits())
    assert result['state'] == 'reconciliation_required'
    assert len([req for req in fake.calls if req.method == 'POST']) == 1
    with pytest.raises(demo.DemoBlocked, match='reconciliation'):
        demo.execute_pair(pair(), confirmed=True, risk_limits=limits())
    assert len([req for req in fake.calls if req.method == 'POST']) == 1


@pytest.mark.parametrize('failure', ['timeout', 'partial', 'rejected'])
def test_integrated_second_leg_failure_preserves_first_without_rollback(exchange, monkeypatch, failure):
    responses, _ = exchange
    fake = LifecycleExchange(responses)
    if failure == 'timeout':
        fake.fail_post = 'okx'
    elif failure == 'partial':
        fake.partial = 'okx'
    else:
        fake.reject_post = 'okx'
    monkeypatch.setattr(data.urllib.request, 'build_opener', lambda handler: fake)
    result = demo.execute_pair(pair(), confirmed=True, risk_limits=limits())
    assert result['state'] == 'reconciliation_required' and result['confirmed_legs'] == 1
    assert len([req for req in fake.calls if req.method == 'POST']) == 2
    with pytest.raises(demo.DemoBlocked, match='reconciliation'):
        demo.execute_pair(pair(), confirmed=True, risk_limits=limits())
    assert len([req for req in fake.calls if req.method == 'POST']) == 2


@pytest.mark.parametrize(('failure', 'venue', 'close_requests'), [
    ('timeout', 'okx', 1), ('partial', 'okx', 1), ('timeout', 'bybit', 2),
])
def test_close_unknown_or_partial_never_retries(exchange, monkeypatch, failure, venue, close_requests):
    responses, _ = exchange
    fake = LifecycleExchange(responses)
    monkeypatch.setattr(data.urllib.request, 'build_opener', lambda handler: fake)
    assert demo.execute_pair(pair(), confirmed=True, risk_limits=limits())['state'] == 'open_confirmed'
    if failure == 'timeout':
        fake.fail_post = venue
    else:
        fake.partial = venue
    assert demo.close_pair(confirmed=True, close_confirmed=True, risk_limits=limits())['state'] == 'close_reconciliation_required'
    assert len([req for req in fake.calls if req.method == 'POST']) == 2 + close_requests
    with pytest.raises(demo.DemoBlocked):
        demo.close_pair(confirmed=True, close_confirmed=True, risk_limits=limits())
    assert len([req for req in fake.calls if req.method == 'POST']) == 2 + close_requests


def test_demo_transport_rejects_tampered_production_host_before_network(exchange, monkeypatch):
    _, calls = exchange
    monkeypatch.setitem(demo._BASES, 'bybit', 'https://api.bybit.com')
    with pytest.raises(data.DemoDataBlocked):
        data.read('bybit', 'BTC', 'instrument')
    token = demo._PAIR_ACTIVE.set(True)
    try:
        with pytest.raises(demo.DemoBlocked, match='endpoint'):
            demo._request('bybit', 'POST', None,
                          demo._order_fields('bybit', 'BTC', 'buy', '0.018', 'testclient'), True)
    finally:
        demo._PAIR_ACTIVE.reset(token)
    assert not calls


@pytest.mark.parametrize(('venue', 'name'), [('bybit', 'BYBIT_API_KEY'), ('bybit', 'BYBIT_TESTNET_API_KEY'),
                                             ('okx', 'OKX_API_KEY')])
def test_known_production_or_testnet_key_cannot_be_used_as_demo(exchange, monkeypatch, venue, name):
    _, calls = exchange
    monkeypatch.setenv(name, venue + '-dummy-key')
    with pytest.raises(data.DemoDataBlocked):
        data.read(venue, 'BTC', 'instrument')
    assert not calls


@pytest.mark.parametrize('timeout', [False, True])
def test_cancel_uncertain_order_is_journalled_once_without_retry(exchange, monkeypatch, timeout):
    responses, _ = exchange
    fake = LifecycleExchange(responses)
    fake.partial = 'bybit'
    fake.cancel_timeout = timeout
    monkeypatch.setattr(data.urllib.request, 'build_opener', lambda handler: fake)
    assert demo.execute_pair(pair(), confirmed=True, risk_limits=limits())['state'] == 'reconciliation_required'
    record = json.loads(demo._journal_path().read_text())
    client_id = record['legs'][0]['request']['orderLinkId']
    with pytest.raises(demo.DemoBlocked):
        demo.cancel_pending('bybit', 'otherclient', confirmed=True)
    assert demo.cancel_pending('bybit', client_id, confirmed=True)['state'] == 'cancel_reconciliation_required'
    assert json.loads(demo._journal_path().read_text())['state'] == 'cancel_reconciliation_required'
    with pytest.raises(demo.DemoBlocked):
        demo.cancel_pending('bybit', client_id, confirmed=True)
    cancels = [req for req in fake.calls if urlsplit(req.full_url).path == '/v5/order/cancel']
    assert len(cancels) == 1 and json.loads(cancels[0].data)['orderLinkId'] == client_id


def test_okx_cancel_always_uses_demo_header_and_known_client_id(exchange, monkeypatch):
    responses, _ = exchange
    fake = LifecycleExchange(responses)
    fake.partial = 'okx'
    monkeypatch.setattr(data.urllib.request, 'build_opener', lambda handler: fake)
    reverse = demo.FuturesPair('BTC', 'okx', 'bybit', '0.018', '1.8', '0.01')
    assert demo.execute_pair(reverse, confirmed=True, risk_limits=limits())['state'] == 'reconciliation_required'
    record = json.loads(demo._journal_path().read_text())
    client_id = record['legs'][0]['request']['clOrdId']
    assert demo.cancel_pending('okx', client_id, confirmed=True)['state'] == 'cancel_reconciliation_required'
    cancel = [req for req in fake.calls if urlsplit(req.full_url).path == '/api/v5/trade/cancel-order']
    assert len(cancel) == 1 and json.loads(cancel[0].data)['clOrdId'] == client_id
    assert {k.lower(): v for k, v in cancel[0].header_items()}['x-simulated-trading'] == '1'


def test_insufficient_demo_margin_rejects_before_first_order(exchange, monkeypatch):
    responses, _ = exchange
    responses[('okx', '/api/v5/account/balance')]['data'][0]['details'][0]['availEq'] = '1'
    fake = LifecycleExchange(responses)
    monkeypatch.setattr(data.urllib.request, 'build_opener', lambda handler: fake)
    with pytest.raises(demo.DemoBlocked, match='preflight'):
        demo.execute_pair(pair(), confirmed=True, risk_limits=limits())
    assert all(req.method == 'GET' for req in fake.calls) and not demo._journal_path().exists()


def test_manual_resolution_requires_terminal_status_and_flat_accounts(exchange, monkeypatch):
    responses, _ = exchange
    fake = LifecycleExchange(responses)
    fake.partial = 'bybit'
    monkeypatch.setattr(data.urllib.request, 'build_opener', lambda handler: fake)
    assert demo.execute_pair(pair(), confirmed=True, risk_limits=limits())['state'] == 'reconciliation_required'
    record = json.loads(demo._journal_path().read_text())
    client_id = record['legs'][0]['request']['orderLinkId']
    assert demo.cancel_pending('bybit', client_id, confirmed=True)['state'] == 'cancel_reconciliation_required'
    responses[('bybit', '/v5/position/list')]['result']['list'][0].update(size='0', side='')
    before = demo._journal_path().read_bytes()
    with pytest.raises(demo.DemoBlocked):
        demo.resolve_pending_flat(confirmed=True)
    assert demo._journal_path().read_bytes() == before  # Missing status is never treated as terminal.
    fake.terminal['bybit'] = 'Cancelled'
    responses[('okx', '/api/v5/account/positions')] = okx([{'instId': 'BTC-USDT-SWAP', 'posSide': 'net', 'pos': '-0.3'}])
    with pytest.raises(demo.DemoBlocked):
        demo.resolve_pending_flat(confirmed=True)
    assert demo._journal_path().read_bytes() == before  # Residual position still blocks release.
    responses[('okx', '/api/v5/account/positions')] = okx([])
    responses[('okx', '/api/v5/trade/orders-pending')] = okx([{'ordId': 'still-open'}])
    with pytest.raises(demo.DemoBlocked):
        demo.resolve_pending_flat(confirmed=True)
    assert demo._journal_path().read_bytes() == before
    responses[('okx', '/api/v5/trade/orders-pending')] = okx([])
    assert demo.resolve_pending_flat(confirmed=True)['state'] == 'manually_resolved_flat'
    assert not demo._journal_path().exists()
    assert len(list(demo._journal_path().parent.glob('demo-futures-resolved-*.json'))) == 1


def test_manual_resolution_rejects_conflicting_terminal_statuses(exchange):
    responses, calls = exchange
    add_order_lookup_fixtures(responses)
    responses[('bybit', '/v5/order/history')]['result']['list'][0]['orderStatus'] = 'Cancelled'
    with pytest.raises(data.DemoDataBlocked):
        data.verify_terminal_order('bybit', 'BTC', journal_record(1)['legs'][0]['request'], max_age_ms=10_000)
    assert calls and all(req.method == 'GET' for req in calls)


def test_issue22_read_only_positions_without_order_permission(exchange, monkeypatch):
    """Demo reads do not require order permission, but still enforce mode and dry-run."""
    responses, calls = exchange
    execution_mode.set_demo_orders_enabled(False)
    assert data.read('bybit', 'BTC', 'position').payload == responses[('bybit', '/v5/position/list')]
    assert data.read('okx', 'BTC', 'position').payload == responses[('okx', '/api/v5/account/positions')]
    assert all(request.method == 'GET' for request in calls)
    with pytest.raises(demo.DemoBlocked):
        demo.execute_pair(pair(), confirmed=True, risk_limits=limits())
    monkeypatch.setenv('FARB_DRY_RUN', '1')
    with pytest.raises(data.DemoDataBlocked):
        data.read('bybit', 'BTC', 'position')
    monkeypatch.delenv('FARB_DRY_RUN')
    execution_mode.set_mode('simulation')
    with pytest.raises(data.DemoDataBlocked):
        data.read('okx', 'BTC', 'position')


def test_issue22_web_manual_pair_full_cycle_on_mock_transport(exchange, monkeypatch):
    """Exercise web controls through real permission, preflight, journal and adapters."""
    import asyncio
    from types import SimpleNamespace
    from server.routes import demo_trading

    class Request:
        def __init__(self, payload, host='127.0.0.1'):
            self.payload = payload
            self.client = SimpleNamespace(host=host)
            self.url = SimpleNamespace(hostname='127.0.0.1')
            self.headers = {'content-type': 'application/json'}
        async def json(self):
            return self.payload

    async def immediate(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    monkeypatch.setattr(demo_trading.asyncio, 'to_thread', immediate)
    responses, _ = exchange
    fake = LifecycleExchange(responses)
    monkeypatch.setattr(data.urllib.request, 'build_opener', lambda handler: fake)
    execution_mode.set_demo_orders_enabled(False)
    async def run():
        assert (await demo_trading.positions(Request({'base': 'BTC'})))['result']['okx']['positions'] == []
        payload = {'base': 'BTC', 'long_venue': 'bybit', 'short_venue': 'okx', 'amount_usd': '2000', 'confirmed': True}
        assert (await demo_trading.open_pair(Request(payload))).status_code == 409
        assert (await demo_trading.permission(Request({'enabled': True, 'confirmed': True}, host='192.0.2.1'))).status_code == 403
        assert (await demo_trading.permission(Request({'enabled': True, 'confirmed': True})))['orders_enabled']
        result = await demo_trading.open_pair(Request(payload))
        assert result['result']['state'] == 'open_confirmed'
        summary = (await demo_trading.journal(Request({})))['result']
        assert summary['state'] == 'open_confirmed' and len(summary['legs']) == 2
        assert all(leg['confirmed'] for leg in summary['legs'])
        result = await demo_trading.positions(Request({'base': 'BTC'}))
        assert result['result']['bybit']['positions'][0]['size'] != '0'
        assert result['result']['okx']['positions'][0]['pos'] != '0'
        assert not (await demo_trading.stop_openings(Request({'confirmed': True})))['openings_enabled']
        assert (await demo_trading.open_pair(Request(payload))).status_code == 409
        assert (await demo_trading.close_pair(Request({'confirmed': True})))['result']['state'] == 'closed_confirmed'
        assert (await demo_trading.permission(Request({'enabled': False, 'confirmed': True})))['orders_enabled'] is False
    asyncio.run(run())
    posted = [(urlsplit(req.full_url).hostname, json.loads(req.data)) for req in fake.calls if req.method == 'POST']
    assert [host for host, _ in posted] == ['api-demo.bybit.com', 'www.okx.com', 'www.okx.com', 'api-demo.bybit.com']
    assert all(fields['reduceOnly'] is True for _, fields in posted[2:])
    assert all(req.get_header('X-simulated-trading') == '1' for req in fake.calls if urlsplit(req.full_url).hostname == 'www.okx.com')


@pytest.mark.parametrize('failure', ['timeout', 'partial', 'rejected'])
def test_issue22_web_uncertain_order_blocks_repeat(exchange, monkeypatch, failure):
    import asyncio
    from types import SimpleNamespace
    from server.routes import demo_trading

    class Request:
        def __init__(self, payload):
            self.payload = payload
            self.client = SimpleNamespace(host='127.0.0.1')
            self.url = SimpleNamespace(hostname='127.0.0.1')
            self.headers = {'content-type': 'application/json'}
        async def json(self):
            return self.payload

    async def immediate(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    monkeypatch.setattr(demo_trading.asyncio, 'to_thread', immediate)
    fake = LifecycleExchange(exchange[0])
    if failure == 'timeout':
        fake.fail_post = 'bybit'
    elif failure == 'partial':
        fake.partial = 'bybit'
    else:
        fake.reject_post = 'bybit'
    monkeypatch.setattr(data.urllib.request, 'build_opener', lambda handler: fake)
    payload = {'base': 'BTC', 'long_venue': 'bybit', 'short_venue': 'okx', 'amount_usd': '2000', 'confirmed': True}
    async def run():
        result = await demo_trading.open_pair(Request(payload))
        assert result['result']['state'] == 'reconciliation_required'
        journal = (await demo_trading.journal(Request({})))['result']
        assert journal['state'] == 'reconciliation_required'
        assert journal['legs'][0]['client_id']
        assert (await demo_trading.open_pair(Request(payload))).status_code == 409
    asyncio.run(run())
    assert len([request for request in fake.calls if request.method == 'POST']) == 1


def test_issue22_auto_strategy_scans_opens_and_closes_via_mock_transport(exchange, monkeypatch):
    from execution import run_pure_futures_spread as runner
    from backtest.funding_providers import get_funding_provider
    responses, _ = exchange
    fake = LifecycleExchange(responses)
    monkeypatch.setattr(data.urllib.request, 'build_opener', lambda handler: fake)
    monkeypatch.setattr(runner, 'time', SimpleNamespace(time=lambda: NOW / 1000))
    provider = get_funding_provider('okx')
    monkeypatch.setattr(provider, '_any_cache', None)
    monkeypatch.setattr(provider, '_mark_cache', None)
    monkeypatch.setattr(provider, '_index_cache', None)
    settings = {'trade_usd': '2000', 'min_spread_pct': '0.01', 'min_net_pct': '0.01',
                'exit_spread_pct': '0', 'max_mark_spread_pct': '0.5', 'max_leverage': '3',
                'max_fee_pct': '0.1', 'max_slippage_pct': '0.1', 'max_funding_gap_sec': 600,
                'interval_sec': 30}
    opened = runner.run_demo_strategy_once(settings)
    assert opened['action'] == 'open' and opened['result']['state'] == 'open_confirmed'
    assert len([req for req in fake.calls if req.method == 'POST']) == 2
    responses[('okx', '/api/v5/public/funding-rate')]['data'][0]['fundingRate'] = '-0.01'
    closed = runner.run_demo_strategy_once(settings)
    assert closed['action'] == 'close' and closed['result']['state'] == 'closed_confirmed'
    orders = [json.loads(req.data) for req in fake.calls if req.method == 'POST']
    assert len(orders) == 4 and all(order['reduceOnly'] for order in orders[2:])


@pytest.mark.parametrize('failure', ['partial', 'timeout', 'margin'])
def test_issue22_auto_strategy_blocks_unknown_or_insufficient_margin(exchange, monkeypatch, failure):
    from execution import run_pure_futures_spread as runner
    from backtest.funding_providers import get_funding_provider
    responses, _ = exchange
    fake = LifecycleExchange(responses)
    if failure == 'partial':
        fake.partial = 'bybit'
    elif failure == 'timeout':
        fake.fail_post = 'bybit'
    else:
        responses[('bybit', '/v5/account/wallet-balance')]['result']['list'][0]['totalAvailableBalance'] = '0'
    monkeypatch.setattr(data.urllib.request, 'build_opener', lambda handler: fake)
    monkeypatch.setattr(runner, 'time', SimpleNamespace(time=lambda: NOW / 1000))
    provider = get_funding_provider('okx')
    monkeypatch.setattr(provider, '_any_cache', None)
    monkeypatch.setattr(provider, '_mark_cache', None)
    monkeypatch.setattr(provider, '_index_cache', None)
    settings = {'trade_usd': '2000', 'min_spread_pct': '0.01', 'min_net_pct': '0.01',
                'exit_spread_pct': '0', 'max_mark_spread_pct': '0.5', 'max_leverage': '3',
                'max_fee_pct': '0.1', 'max_slippage_pct': '0.1', 'max_funding_gap_sec': 600, 'interval_sec': 30}
    outcome = runner.run_demo_strategy_once(settings)
    posted = [req for req in fake.calls if req.method == 'POST']
    if failure == 'margin':
        assert outcome['action'] == 'none' and not posted
    else:
        assert outcome['result']['state'] == 'reconciliation_required' and len(posted) == 1
        with pytest.raises(demo.DemoBlocked):
            runner.run_demo_strategy_once(settings)
        assert len([req for req in fake.calls if req.method == 'POST']) == 1


@pytest.mark.parametrize(('venue', 'side'), [('bybit', 'buy'), ('bybit', 'sell'), ('okx', 'buy'), ('okx', 'sell')])
def test_issue22_single_demo_order_open_and_reduce_only_close(exchange, monkeypatch, venue, side):
    from execution.demo_futures_preflight import TechnicalLimits
    responses, _ = exchange
    fake = LifecycleExchange(responses)
    monkeypatch.setattr(data.urllib.request, 'build_opener', lambda handler: fake)
    pair = data.single_for_notional(venue, 'BTC', side, '2000', max_age_ms=30_000)
    assert demo.execute_single(pair, venue, side, confirmed=True, risk_limits=TechnicalLimits(30_000))['state'] == 'open_confirmed'
    assert demo.close_single(confirmed=True, risk_limits=TechnicalLimits(30_000))['state'] == 'closed_confirmed'
    sent = [json.loads(req.data) for req in fake.calls if req.method == 'POST']
    assert len(sent) == 2 and 'reduceOnly' not in sent[0] and sent[1]['reduceOnly'] is True
    assert sent[0]['side'].lower() != sent[1]['side'].lower()
    assert demo.execute_single(pair, venue, side, confirmed=True, risk_limits=TechnicalLimits(30_000))['state'] == 'open_confirmed'


@pytest.mark.parametrize('failure', ['timeout', 'partial', 'rejected'])
def test_issue22_single_uncertain_never_retries(exchange, monkeypatch, failure):
    from execution.demo_futures_preflight import TechnicalLimits
    responses, _ = exchange
    fake = LifecycleExchange(responses)
    if failure == 'timeout': fake.fail_post = 'bybit'
    elif failure == 'partial': fake.partial = 'bybit'
    else: fake.reject_post = 'bybit'
    monkeypatch.setattr(data.urllib.request, 'build_opener', lambda handler: fake)
    pair = data.single_for_notional('bybit', 'BTC', 'buy', '2000', max_age_ms=30_000)
    result = demo.execute_single(pair, 'bybit', 'buy', confirmed=True, risk_limits=TechnicalLimits(30_000))
    assert result['state'] == 'reconciliation_required'
    with pytest.raises(demo.DemoBlocked):
        demo.execute_single(pair, 'bybit', 'buy', confirmed=True, risk_limits=TechnicalLimits(30_000))
    with pytest.raises(demo.DemoBlocked):
        demo.close_single(confirmed=True, risk_limits=TechnicalLimits(30_000))
    assert len([req for req in fake.calls if req.method == 'POST']) == 1


def test_issue22_reconcile_terminal_partial_single_then_reduce_only_close(exchange, monkeypatch):
    from execution.demo_futures_preflight import TechnicalLimits
    responses, _ = exchange
    fake = LifecycleExchange(responses)
    fake.partial = 'bybit'
    monkeypatch.setattr(data.urllib.request, 'build_opener', lambda handler: fake)
    pair = data.single_for_notional('bybit', 'BTC', 'buy', '2000', max_age_ms=30_000)
    assert demo.execute_single(pair, 'bybit', 'buy', confirmed=True, risk_limits=TechnicalLimits(30_000))['state'] == 'reconciliation_required'
    responses[('bybit', '/v5/position/list')]['result']['list'][0]['size'] = '0.004'
    original_open = fake.open
    def terminal(request, timeout):
        query = parse_qs(urlsplit(request.full_url).query)
        if request.method == 'GET' and 'orderLinkId' in query:
            order_id, (_, fields) = next(iter(fake.orders.items()))
            payload = bybit({'list': [{'orderId': order_id, 'orderLinkId': fields['orderLinkId'],
                       'symbol': fields['symbol'], 'side': fields['side'], 'orderStatus': 'Cancelled',
                       'cumExecQty': '0.004', 'avgPrice': '100000'}], 'nextPageCursor': ''})
            class Response:
                def __enter__(self): return self
                def __exit__(self, *_): return False
                def read(self, limit): return json.dumps(payload).encode()
            fake.calls.append(request)
            return Response()
        return original_open(request, timeout)
    fake.open = terminal
    assert demo.reconcile_single(confirmed=True)['state'] == 'open_partial_confirmed'
    fake.partial = None
    assert demo.close_single(confirmed=True, risk_limits=TechnicalLimits(30_000))['state'] == 'closed_confirmed'
    posted = [json.loads(req.data) for req in fake.calls if req.method == 'POST']
    assert len(posted) == 2 and posted[1]['reduceOnly'] is True and posted[1]['qty'] == '0.004'


def test_issue22_reconcile_partial_close_requires_explicit_new_close(exchange, monkeypatch):
    from execution.demo_futures_preflight import TechnicalLimits
    responses, _ = exchange
    fake = LifecycleExchange(responses)
    monkeypatch.setattr(data.urllib.request, 'build_opener', lambda handler: fake)
    pair = data.single_for_notional('bybit', 'BTC', 'buy', '2000', max_age_ms=30_000)
    assert demo.execute_single(pair, 'bybit', 'buy', confirmed=True, risk_limits=TechnicalLimits(30_000))['state'] == 'open_confirmed'
    fake.partial = 'bybit'
    assert demo.close_single(confirmed=True, risk_limits=TechnicalLimits(30_000))['state'] == 'close_reconciliation_required'
    responses[('bybit', '/v5/position/list')]['result']['list'][0].update(size='0.016', side='Buy')
    original_open = fake.open
    def terminal(request, timeout):
        query = parse_qs(urlsplit(request.full_url).query)
        if request.method == 'GET' and 'orderLinkId' in query:
            oid, (_, fields) = next((oid, item) for oid, item in fake.orders.items() if item[1]['orderLinkId'] == query['orderLinkId'][0])
            payload = bybit({'list': [{'orderId': oid, 'orderLinkId': fields['orderLinkId'], 'symbol': fields['symbol'],
                       'side': fields['side'], 'orderStatus': 'Cancelled', 'cumExecQty': '0.004',
                       'avgPrice': '100000', 'reduceOnly': True}], 'nextPageCursor': ''})
            class Response:
                def __enter__(self): return self
                def __exit__(self, *_): return False
                def read(self, limit): return json.dumps(payload).encode()
            fake.calls.append(request)
            return Response()
        return original_open(request, timeout)
    fake.open = terminal
    result = demo.reconcile_single(confirmed=True)
    assert result['state'] == 'open_partial_confirmed' and result['remaining_qty'] == '0.016'
    assert len([req for req in fake.calls if req.method == 'POST']) == 2
    fake.partial = None
    assert demo.close_single(confirmed=True, risk_limits=TechnicalLimits(30_000))['state'] == 'closed_confirmed'
    posted = [json.loads(req.data) for req in fake.calls if req.method == 'POST']
    assert len(posted) == 3 and posted[-1]['qty'] == '0.016' and posted[-1]['reduceOnly'] is True


def test_issue22_history_uses_demo_credentials_and_redacts_response(exchange, monkeypatch):
    from server.routes.demo_trading import _history
    responses, calls = exchange
    responses[('bybit', '/v5/order/history')] = bybit({'list': [{'symbol': 'BTCUSDT', 'orderId': 'a', 'extra_secret': 'dummy-secret'}], 'nextPageCursor': ''})
    responses[('bybit', '/v5/position/closed-pnl')] = bybit({'list': [{'symbol': 'BTCUSDT', 'closedPnl': '2'}], 'nextPageCursor': ''})
    responses[('bybit', '/v5/account/transaction-log')] = bybit({'list': [{'symbol': 'BTCUSDT', 'funding': '0.4'}, {'symbol': 'ETHUSDT', 'funding': '1'}], 'nextPageCursor': ''})
    responses[('bybit', '/v5/execution/list')] = bybit({'list': [{'symbol': 'BTCUSDT', 'execId': 'b', 'execFee': '0.1'}], 'nextPageCursor': ''})
    responses[('okx', '/api/v5/trade/orders-history')] = okx([{'instId': 'BTC-USDT-SWAP', 'ordId': 'c', 'extra_secret': 'dummy-secret'}])
    responses[('okx', '/api/v5/account/positions-history')] = okx([{'instId': 'BTC-USDT-SWAP', 'realizedPnl': '3', 'fundingFee': '1'}])
    responses[('okx', '/api/v5/account/bills')] = okx([{'instId': 'BTC-USDT-SWAP', 'pnl': '0.5'}])
    responses[('okx', '/api/v5/trade/fills')] = okx([{'instId': 'BTC-USDT-SWAP', 'tradeId': 'd', 'fee': '-0.1'}])
    execution_mode.set_demo_orders_enabled(False)
    result = _history('BTC')
    assert result['bybit']['fills'][0]['execFee'] == '0.1'
    assert result['okx']['closed_positions'][0]['fundingFee'] == '1'
    assert len(result['bybit']['funding']) == 1 and result['bybit']['funding'][0]['funding'] == '0.4'
    assert 'dummy-secret' not in json.dumps(result)
    assert all(request.method == 'GET' for request in calls)
    assert all(urlsplit(request.full_url).hostname in ('api-demo.bybit.com', 'www.okx.com') for request in calls)


def test_issue22_demo_strategy_settings_survive_reload_and_fail_closed(exchange):
    from execution import run_pure_futures_spread as runner
    settings = {'trade_usd': '2000', 'min_spread_pct': '0.01', 'min_net_pct': '0.01',
                'exit_spread_pct': '0', 'max_mark_spread_pct': '0.5', 'max_leverage': '3',
                'max_fee_pct': '0.1', 'max_slippage_pct': '0.1', 'max_funding_gap_sec': 600, 'interval_sec': 30}
    assert runner.save_demo_strategy_settings(settings) == runner.load_demo_strategy_settings()
    path = execution_mode._directory() / 'demo-strategy-settings.json'
    path.write_text('{broken')
    with pytest.raises(demo.DemoBlocked): runner.load_demo_strategy_settings()


def test_issue22_shared_stop_blocks_automatic_close_but_preserves_manual_close(exchange):
    token = demo._STRATEGY_ACTIVE.set(True)
    try:
        execution_mode.set_demo_openings_enabled(False)
        with pytest.raises(demo.DemoBlocked):
            demo._permit(True)
    finally:
        demo._STRATEGY_ACTIVE.reset(token)
    demo._permit(True)
    with pytest.raises(demo.DemoBlocked):
        demo._permit(True, opening=True)


def test_issue22_first_pair_leg_partial_recovery_then_explicit_close(exchange, monkeypatch):
    from execution.demo_futures_preflight import TechnicalLimits
    responses, _ = exchange
    fake = LifecycleExchange(responses)
    fake.partial = 'bybit'
    monkeypatch.setattr(data.urllib.request, 'build_opener', lambda handler: fake)
    assert demo.execute_pair(pair(), confirmed=True, risk_limits=limits())['state'] == 'reconciliation_required'
    responses[('bybit', '/v5/position/list')]['result']['list'][0].update(size='0.004', side='Buy')
    original_open = fake.open
    def terminal(request, timeout):
        query = parse_qs(urlsplit(request.full_url).query)
        if request.method == 'GET' and 'orderLinkId' in query:
            oid, (_, fields) = next(iter(fake.orders.items()))
            payload = bybit({'list': [{'orderId': oid, 'orderLinkId': fields['orderLinkId'], 'symbol': fields['symbol'],
                       'side': fields['side'], 'orderStatus': 'Cancelled', 'cumExecQty': '0.004',
                       'avgPrice': '100000'}], 'nextPageCursor': ''})
            class Response:
                def __enter__(self): return self
                def __exit__(self, *_): return False
                def read(self, limit): return json.dumps(payload).encode()
            fake.calls.append(request)
            return Response()
        return original_open(request, timeout)
    fake.open = terminal
    result = demo.reconcile_pair_first_leg(confirmed=True)
    assert result['state'] == 'open_partial_confirmed' and result['recovered_from_pair'] is True
    assert len([req for req in fake.calls if req.method == 'POST']) == 1
    fake.partial = None
    assert demo.close_single(confirmed=True, risk_limits=TechnicalLimits(30_000))['state'] == 'closed_confirmed'
    posted = [json.loads(req.data) for req in fake.calls if req.method == 'POST']
    assert len(posted) == 2 and posted[-1]['qty'] == '0.004' and posted[-1]['reduceOnly'] is True
    assert list(demo._journal_path().parent.glob('demo-futures-pair-recovery-*.json'))


def test_issue22_second_pair_leg_partial_recovery_then_close_both(exchange, monkeypatch):
    from server.routes.demo_trading import _close_pair_or_recovery
    responses, _ = exchange
    fake = LifecycleExchange(responses)
    fake.partial = 'okx'
    monkeypatch.setattr(data.urllib.request, 'build_opener', lambda handler: fake)
    assert demo.execute_pair(pair(), confirmed=True, risk_limits=limits())['state'] == 'reconciliation_required'
    responses[('okx', '/api/v5/account/positions')]['data'][0]['pos'] = '-0.3'
    original_open = fake.open
    def terminal(request, timeout):
        query = parse_qs(urlsplit(request.full_url).query)
        if request.method == 'GET' and 'clOrdId' in query:
            oid, (_, fields) = next((oid, item) for oid, item in fake.orders.items() if item[0] == 'okx')
            payload = okx([{'ordId': oid, 'clOrdId': fields['clOrdId'], 'instId': fields['instId'],
                            'side': fields['side'], 'state': 'canceled', 'accFillSz': '0.3', 'avgPx': '100000'}])
            class Response:
                def __enter__(self): return self
                def __exit__(self, *_): return False
                def read(self, limit): return json.dumps(payload).encode()
            fake.calls.append(request)
            return Response()
        return original_open(request, timeout)
    fake.open = terminal
    result = demo.reconcile_pair_second_leg(confirmed=True)
    assert result['state'] == 'open_partial_confirmed' and result['pending_leg'] == 'bybit'
    assert len([req for req in fake.calls if req.method == 'POST']) == 2
    fake.partial = None
    assert _close_pair_or_recovery()['state'] == 'closed_confirmed'
    posted = [(urlsplit(req.full_url).hostname, json.loads(req.data)) for req in fake.calls if req.method == 'POST']
    assert [host for host, _ in posted] == ['api-demo.bybit.com', 'www.okx.com', 'www.okx.com', 'api-demo.bybit.com']
    assert all(fields['reduceOnly'] is True for _, fields in posted[2:])
    assert posted[2][1]['sz'] == '0.3'
    assert responses[('okx', '/api/v5/account/positions')]['data'] == []
    assert responses[('bybit', '/v5/position/list')]['result']['list'][0]['size'] == '0'


@pytest.mark.parametrize('failed_venue', ['okx', 'bybit'])
def test_issue22_pair_close_partial_reconciles_then_closes_residuals(exchange, monkeypatch, failed_venue):
    from server.routes.demo_trading import _close_pair_or_recovery
    responses, _ = exchange
    fake = LifecycleExchange(responses)
    monkeypatch.setattr(data.urllib.request, 'build_opener', lambda handler: fake)
    assert demo.execute_pair(pair(), confirmed=True, risk_limits=limits())['state'] == 'open_confirmed'
    fake.partial = failed_venue
    assert demo.close_pair(confirmed=True, close_confirmed=True, risk_limits=limits())['state'] == 'close_reconciliation_required'
    if failed_venue == 'okx':
        responses[('okx', '/api/v5/account/positions')]['data'] = [{'instId': 'BTC-USDT-SWAP', 'posSide': 'net', 'mgnMode': 'cross', 'pos': '-1.5'}]
    else:
        responses[('bybit', '/v5/position/list')]['result']['list'][0].update(size='0.014', side='Buy')
    original_open = fake.open
    def terminal(request, timeout):
        query = parse_qs(urlsplit(request.full_url).query)
        url = urlsplit(request.full_url)
        if request.method == 'GET' and ('clOrdId' in query or 'orderLinkId' in query):
            client = query.get('clOrdId', query.get('orderLinkId'))[0]
            oid, (venue, fields) = next((oid, item) for oid, item in fake.orders.items() if
                                       item[1]['orderLinkId' if item[0] == 'bybit' else 'clOrdId'] == client)
            if venue == 'okx':
                payload = okx([{'ordId': oid, 'clOrdId': client, 'instId': fields['instId'], 'side': fields['side'],
                                'state': 'canceled', 'accFillSz': '0.3', 'avgPx': '100000', 'reduceOnly': 'true'}])
            else:
                payload = bybit({'list': [{'orderId': oid, 'orderLinkId': client, 'symbol': fields['symbol'], 'side': fields['side'],
                                  'orderStatus': 'Cancelled', 'cumExecQty': '0.004', 'avgPrice': '100000', 'reduceOnly': True}], 'nextPageCursor': ''})
            class Response:
                def __enter__(self): return self
                def __exit__(self, *_): return False
                def read(self, limit): return json.dumps(payload).encode()
            fake.calls.append(request)
            return Response()
        return original_open(request, timeout)
    fake.open = terminal
    result = demo.reconcile_pair_closing(confirmed=True)
    assert result['state'] == 'open_partial_confirmed'
    old_count = len([req for req in fake.calls if req.method == 'POST'])
    assert old_count == (3 if failed_venue == 'okx' else 4)
    fake.partial = None
    assert _close_pair_or_recovery()['state'] == 'closed_confirmed'
    assert responses[('okx', '/api/v5/account/positions')]['data'] == []
    assert responses[('bybit', '/v5/position/list')]['result']['list'][0]['size'] == '0'
    posted = [json.loads(req.data) for req in fake.calls if req.method == 'POST']
    assert all(order['reduceOnly'] is True for order in posted[2:])


def test_issue22_web_strategy_start_stop_and_shared_permission(exchange, monkeypatch):
    import asyncio
    import threading
    from types import SimpleNamespace
    from server.routes import demo_trading
    gate = threading.Event()
    entered = threading.Event()
    calls = []
    def cycle(settings):
        calls.append(settings)
        entered.set()
        assert gate.wait(5)
        return {'action': 'hold', 'base': 'BTC'}
    monkeypatch.setattr(demo_trading, 'run_demo_strategy_once', cycle)
    class Request:
        def __init__(self, payload):
            self.payload = payload
            self.client = SimpleNamespace(host='127.0.0.1')
            self.url = SimpleNamespace(hostname='127.0.0.1')
            self.headers = {'content-type': 'application/json'}
        async def json(self): return self.payload
    settings = {'trade_usd': '2000', 'min_spread_pct': '0.01', 'min_net_pct': '0.01',
                'exit_spread_pct': '0', 'max_mark_spread_pct': '0.5', 'max_leverage': '3',
                'max_fee_pct': '0.1', 'max_slippage_pct': '0.1', 'max_funding_gap_sec': 600, 'interval_sec': 30}
    async def run():
        result = await demo_trading.start_strategy(Request({'confirmed': True, 'settings': settings}))
        assert result['running'] is True and entered.wait(2)
        duplicate = await demo_trading.start_strategy(Request({'confirmed': True, 'settings': settings}))
        assert duplicate.status_code == 409
        stopped = await demo_trading.stop_strategy(Request({'confirmed': True}))
        assert stopped['stopping'] is True and not execution_mode.current_state()['openings_enabled']
        with pytest.raises(ValueError, match='strategy'):
            execution_mode.set_demo_openings_enabled(True, confirmed=True)
        gate.set()
    try:
        asyncio.run(run())
    finally:
        gate.set()
        if demo_trading._auto_thread is not None:
            demo_trading._auto_thread.join(5)
    assert not demo_trading._auto_state['running'] and len(calls) == 1
    execution_mode.set_demo_openings_enabled(True, confirmed=True)
    events = (execution_mode._directory() / 'demo-strategy-events.jsonl').read_text()
    assert [json.loads(line)['action'] for line in events.splitlines()] == ['started', 'hold', 'stopped']
    assert 'dummy-secret' not in events


def test_issue22_history_follows_cursors_and_rejects_loops(exchange, monkeypatch):
    class HistoryTransport:
        def __init__(self): self.loop = False
        def open(self, request, timeout):
            url = urlsplit(request.full_url)
            query = parse_qs(url.query)
            assert url.hostname == 'api-demo.bybit.com' and request.method == 'GET'
            cursor = query.get('cursor', [''])[0]
            if not cursor:
                payload = bybit({'list': [{'orderId': '1'}], 'nextPageCursor': 'cursor1'})
            else:
                payload = bybit({'list': [{'orderId': '2'}], 'nextPageCursor': 'cursor1' if self.loop else ''})
            class Response:
                def __enter__(self): return self
                def __exit__(self, *_): return False
                def read(self, limit): return json.dumps(payload).encode()
            return Response()
    fake = HistoryTransport()
    monkeypatch.setattr(data.urllib.request, 'build_opener', lambda handler: fake)
    assert [row['orderId'] for row in data.all_history('bybit', 'BTC', 'history')] == ['1', '2']
    fake.loop = True
    with pytest.raises(data.DemoDataBlocked): data.all_history('bybit', 'BTC', 'history')


def test_issue22_lost_ack_recovers_by_client_id_without_retry(exchange, monkeypatch):
    from execution.demo_futures_preflight import TechnicalLimits
    responses, _ = exchange
    fake = LifecycleExchange(responses)
    original_open = fake.open
    failed = False
    def transport(request, timeout):
        nonlocal failed
        url = urlsplit(request.full_url)
        query = parse_qs(url.query)
        if request.method == 'POST' and url.path == '/v5/order/create' and not failed:
            original_open(request, timeout)  # Exchange accepted, response was lost.
            failed = True
            raise TimeoutError('mock lost acknowledgement')
        if request.method == 'GET' and 'orderLinkId' in query:
            oid, (_, fields) = next(iter(fake.orders.items()))
            payload = bybit({'list': [{'orderId': oid, 'orderLinkId': fields['orderLinkId'], 'symbol': fields['symbol'],
                       'side': fields['side'], 'orderStatus': 'Filled', 'cumExecQty': fields['qty'],
                       'avgPrice': '100000'}], 'nextPageCursor': ''})
            class Response:
                def __enter__(self): return self
                def __exit__(self, *_): return False
                def read(self, limit): return json.dumps(payload).encode()
            fake.calls.append(request)
            return Response()
        return original_open(request, timeout)
    fake.open = transport
    monkeypatch.setattr(data.urllib.request, 'build_opener', lambda handler: fake)
    assert demo.execute_pair(pair(), confirmed=True, risk_limits=limits())['state'] == 'reconciliation_required'
    # A new reader reconstructs state only from the durable journal and exchange responses.
    record = json.loads(demo._journal_path().read_text())
    assert 'order_id' not in record['legs'][0]
    recovered = demo.reconcile_pair_first_leg(confirmed=True)
    assert recovered['state'] == 'open_confirmed' and recovered['order']['order_id'] == '1'
    assert len([req for req in fake.calls if req.method == 'POST']) == 1
    assert demo.close_single(confirmed=True, risk_limits=TechnicalLimits(30_000))['state'] == 'closed_confirmed'
    assert len([req for req in fake.calls if req.method == 'POST']) == 2


def test_issue22_cancel_existing_demo_order_once_then_reconcile(exchange, monkeypatch):
    responses, _ = exchange
    fake = LifecycleExchange(responses)
    responses[('bybit', '/v5/order/realtime')] = bybit({'list': [{'symbol': 'BTCUSDT', 'orderId': 'ext1',
        'orderLinkId': 'client123', 'side': 'Buy', 'qty': '0.01', 'orderStatus': 'New'}], 'nextPageCursor': ''})
    original_open = fake.open
    terminal = False
    def transport(request, timeout):
        query = parse_qs(urlsplit(request.full_url).query)
        if request.method == 'GET' and 'orderLinkId' in query:
            payload = bybit({'list': [{'symbol': 'BTCUSDT', 'orderId': 'ext1', 'orderLinkId': 'client123',
                'side': 'Buy', 'orderStatus': 'Cancelled' if terminal else 'New', 'cumExecQty': '0', 'avgPrice': ''}], 'nextPageCursor': ''})
            class Response:
                def __enter__(self): return self
                def __exit__(self, *_): return False
                def read(self, limit): return json.dumps(payload).encode()
            fake.calls.append(request)
            return Response()
        return original_open(request, timeout)
    fake.open = transport
    monkeypatch.setattr(data.urllib.request, 'build_opener', lambda handler: fake)
    assert demo.cancel_open_order('bybit', 'BTC', 'client123', confirmed=True)['state'] == 'cancel_reconciliation_required'
    with pytest.raises(demo.DemoBlocked): demo.cancel_open_order('bybit', 'BTC', 'client123', confirmed=True)
    with pytest.raises(demo.DemoBlocked): demo._permit(True)
    with pytest.raises(demo.DemoBlocked): demo.reconcile_external_cancel(confirmed=True)
    terminal = True
    responses[('bybit', '/v5/order/realtime')] = bybit({'list': [], 'nextPageCursor': ''})
    assert demo.reconcile_external_cancel(confirmed=True)['state'] == 'cancel_resolved'
    demo._permit(True)
    posts = [req for req in fake.calls if req.method == 'POST']
    assert len(posts) == 1 and urlsplit(posts[0].full_url).path == '/v5/order/cancel'
