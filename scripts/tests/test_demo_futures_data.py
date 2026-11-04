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
            payload = responses[(venue, urlsplit(request.full_url).path)]
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
    assert len(calls) == 18 and all(req.method == 'GET' for req in calls)
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
    assert len(calls) == 5 and all(req.method == 'GET' for req in calls)
    assert any(urlsplit(req.full_url).path == '/v5/execution/list' for req in calls)
    assert any(urlsplit(req.full_url).path == '/api/v5/account/positions' for req in calls)


def test_reconciliation_checks_both_futures_legs(exchange):
    responses, calls = exchange
    add_reconciliation_fixtures(responses, both=True)
    demo._reconcile_step(pair(), confirmed_legs(), 2, limits())
    assert len(calls) == 6 and all(req.method == 'GET' for req in calls)
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
    assert path.read_bytes() == original and not calls


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
