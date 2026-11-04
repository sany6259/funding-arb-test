"""Mocked-only regression tests for isolated futures demo transport."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / 'scripts'):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from core import execution_mode, demo_credentials
from execution import demo_futures as demo

REAL_RECONCILE = demo._reconcile_step


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(execution_mode, '_directory', lambda: tmp_path / 'state')
    monkeypatch.setattr(demo_credentials, '_backend', lambda: 'age')
    monkeypatch.setattr(demo_credentials, 'load', lambda venue, environment: {
        'key': f'{venue}-demo-key', 'secret': f'{venue}-demo-secret', 'passphrase': 'okx-demo-pass'})
    monkeypatch.setattr(demo.urllib.request, 'build_opener', lambda *args: pytest.fail('network was not mocked'))
    # Legacy transport/journal tests stub exchange reconciliation explicitly;
    # its real read-only behavior is exercised in test_demo_futures_data.py.
    monkeypatch.setattr(demo, '_reconcile_step', lambda *args: None)
    monkeypatch.setenv('FARB_ENABLE_DEMO_ORDERS', '1')
    for name in ('FARB_DRY_RUN', 'FARB_LIVE', 'DCA_LIVE', 'BYBIT_REQUIRE_TESTNET', 'BYBIT_API_KEY', 'BYBIT_TESTNET_API_KEY', 'OKX_API_KEY'):
        monkeypatch.delenv(name, raising=False)


def pair(long='bybit', short='okx'):
    return demo.FuturesPair('BTC', long, short, '0.01', '0.1', '0.1')


class MockExchange:
    def __init__(self):
        self.requests = []
        self.orders = {}
        self.bad_status = None
        self.bad_venue = None
        self.fail_on = None

    def open(self, request, timeout):
        self.requests.append(request)
        host = urlsplit(request.full_url).hostname
        venue = 'bybit' if host == 'api-demo.bybit.com' else 'okx' if host == 'www.okx.com' else None
        assert venue in ('bybit', 'okx')
        if self.fail_on == (venue, request.method):
            raise TimeoutError('sensitive-demo-secret fixture')
        if request.method == 'POST':
            fields = json.loads(request.data)
            self.orders[venue] = fields
            response = ({'retCode': 0, 'result': {'orderId': 'bybit-order-1'}} if venue == 'bybit'
                        else {'code': '0', 'data': [{'sCode': '0', 'ordId': '123456'}]})
        elif venue == 'bybit':
            fields = self.orders[venue]
            response = {'retCode': 0, 'result': {'list': [{'orderId': 'bybit-order-1', 'orderLinkId': fields['orderLinkId'],
                'symbol': fields['symbol'], 'side': fields['side'], 'orderStatus': 'Filled',
                'cumExecQty': fields['qty'], 'avgPrice': '100000', 'reduceOnly': fields.get('reduceOnly', False)}]}}
            if self.bad_status and self.bad_venue in (None, venue):
                response['result']['list'][0].update(self.bad_status)
        else:
            fields = self.orders[venue]
            response = {'code': '0', 'data': [{'ordId': '123456', 'clOrdId': fields['clOrdId'],
                'instId': fields['instId'], 'side': fields['side'], 'state': 'filled',
                'accFillSz': fields['sz'], 'avgPx': '100000', 'reduceOnly': 'true' if fields.get('reduceOnly') is True else 'false'}]}
            if self.bad_status and self.bad_venue in (None, venue):
                response['data'][0].update(self.bad_status)
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, limit): return json.dumps(response).encode()
        return Response()


def make_mock(monkeypatch):
    exchange = MockExchange()
    monkeypatch.setattr(demo.urllib.request, 'build_opener', lambda handler: exchange)
    return exchange


def test_disabled_by_default_and_preflight_gate(monkeypatch):
    exchange = make_mock(monkeypatch)
    with pytest.raises(demo.DemoBlocked, match='Demo mode'):
        demo.execute_pair(pair(), confirmed=True)
    execution_mode.set_mode('demo')
    with pytest.raises(demo.DemoBlocked, match='preflight'):
        demo.execute_pair(pair(), confirmed=True)
    assert not exchange.requests and not demo._journal_path().exists()
    with pytest.raises(demo.DemoBlocked, match='permission'):
        demo.execute_pair(pair(), confirmed=False)
    monkeypatch.delenv('FARB_ENABLE_DEMO_ORDERS')
    with pytest.raises(demo.DemoBlocked, match='permission'):
        demo.execute_pair(pair(), confirmed=True)
    monkeypatch.setenv('FARB_ENABLE_DEMO_ORDERS', '1')
    monkeypatch.setenv('FARB_LIVE', '1')
    with pytest.raises(demo.DemoBlocked, match='Live'):
        demo.execute_pair(pair(), confirmed=True)
    monkeypatch.delenv('FARB_LIVE')
    monkeypatch.setenv('FARB_DRY_RUN', '1')
    with pytest.raises(demo.DemoBlocked, match='Dry-run'):
        demo.execute_pair(pair(), confirmed=True)
    assert not exchange.requests


def test_no_testnet_live_or_unknown_venue_fallback(monkeypatch):
    execution_mode.set_mode('demo')
    monkeypatch.setattr(demo, '_preflight', lambda _: None)
    monkeypatch.setenv('BYBIT_REQUIRE_TESTNET', '1')
    with pytest.raises(demo.DemoBlocked, match='separate'):
        demo.execute_pair(pair(), confirmed=True)
    monkeypatch.delenv('BYBIT_REQUIRE_TESTNET')
    with pytest.raises(demo.DemoBlocked, match='Exactly'):
        demo.execute_pair(pair('bybit', 'bitget'), confirmed=True)
    monkeypatch.setattr(demo_credentials, '_backend', lambda: 'keyring')
    with pytest.raises(demo.DemoBlocked, match='age'):
        demo.execute_pair(pair(), confirmed=True)


def test_pair_end_to_end_with_two_futures_and_demo_endpoints(monkeypatch):
    execution_mode.set_mode('demo')
    exchange = make_mock(monkeypatch)
    monkeypatch.setattr(demo, '_preflight', lambda _: None)
    result = demo.execute_pair(pair(), confirmed=True)
    assert result['state'] == 'position_reconciliation_required' and result['confirmed_legs'] == 2
    assert [r.method for r in exchange.requests] == ['POST', 'GET', 'POST', 'GET']
    assert [urlsplit(r.full_url).hostname for r in exchange.requests] == ['api-demo.bybit.com', 'api-demo.bybit.com', 'www.okx.com', 'www.okx.com']
    bybit, okx = exchange.orders['bybit'], exchange.orders['okx']
    assert bybit['category'] == 'linear' and bybit['side'] == 'Buy' and bybit['qty'] == '0.01'
    assert okx['instId'] == 'BTC-USDT-SWAP' and okx['side'] == 'sell' and okx['sz'] == '0.1'
    for request in exchange.requests:
        headers = {k.lower(): v for k, v in request.header_items()}
        url = urlsplit(request.full_url)
        if url.hostname == 'www.okx.com':
            assert headers['x-simulated-trading'] == '1' and headers['ok-access-key'] == 'okx-demo-key'
            payload = headers['ok-access-timestamp'] + request.method + url.path + ('?' + url.query if url.query else '') + (request.data.decode() if request.data else '')
            sig = base64.b64encode(hmac.new(b'okx-demo-secret', payload.encode(), hashlib.sha256).digest()).decode()
            assert headers['ok-access-sign'] == sig
        else:
            assert headers['x-bapi-api-key'] == 'bybit-demo-key'
            payload = headers['x-bapi-timestamp'] + 'bybit-demo-key' + '5000' + (url.query if request.method == 'GET' else request.data.decode())
            assert headers['x-bapi-sign'] == hmac.new(b'bybit-demo-secret', payload.encode(), hashlib.sha256).hexdigest()
    journal = json.loads(demo._journal_path().read_text())
    assert journal['state'] == 'position_reconciliation_required'
    assert 'demo-key' not in repr(journal) and 'demo-secret' not in repr(journal)
    with pytest.raises(demo.DemoBlocked, match='reconciliation'):
        demo.execute_pair(pair(), confirmed=True)
    assert len(exchange.requests) == 4


@pytest.mark.parametrize('bad', [
    {'orderStatus': 'New', 'cumExecQty': '0', 'avgPrice': '0'},
    {'orderStatus': 'PartiallyFilled', 'cumExecQty': '0.005'},
    {'orderStatus': 'Filled', 'cumExecQty': '0.005'},
    {'orderStatus': 'Filled', 'avgPrice': '0'},
])
def test_unconfirmed_first_leg_never_opens_second(monkeypatch, bad):
    execution_mode.set_mode('demo')
    exchange = make_mock(monkeypatch)
    exchange.bad_status = bad
    monkeypatch.setattr(demo, '_preflight', lambda _: None)
    result = demo.execute_pair(pair(), confirmed=True)
    assert result['state'] == 'reconciliation_required' and result['confirmed_legs'] == 0
    assert [r.method for r in exchange.requests] == ['POST', 'GET']
    assert json.loads(demo._journal_path().read_text())['state'] == 'reconciliation_required'


def test_unknown_submission_stops_without_retry_or_rollback(monkeypatch, capsys):
    execution_mode.set_mode('demo')
    exchange = make_mock(monkeypatch)
    exchange.fail_on = ('bybit', 'POST')
    monkeypatch.setattr(demo, '_preflight', lambda _: None)
    result = demo.execute_pair(pair(), confirmed=True)
    assert result['state'] == 'reconciliation_required' and len(exchange.requests) == 1
    assert 'sensitive-demo-secret' not in repr(result) + repr(capsys.readouterr())


def test_unknown_second_leg_never_triggers_rollback(monkeypatch):
    execution_mode.set_mode('demo')
    exchange = make_mock(monkeypatch)
    exchange.fail_on = ('okx', 'POST')
    monkeypatch.setattr(demo, '_preflight', lambda _: None)
    result = demo.execute_pair(pair(), confirmed=True)
    assert result['state'] == 'reconciliation_required' and result['confirmed_legs'] == 1
    assert [r.method for r in exchange.requests] == ['POST', 'GET', 'POST']


def test_partial_second_leg_never_triggers_rollback(monkeypatch):
    execution_mode.set_mode('demo')
    exchange = make_mock(monkeypatch)
    exchange.bad_venue = 'okx'
    exchange.bad_status = {'state': 'partially_filled', 'accFillSz': '0.05'}
    monkeypatch.setattr(demo, '_preflight', lambda _: None)
    result = demo.execute_pair(pair(), confirmed=True)
    assert result['state'] == 'reconciliation_required' and result['confirmed_legs'] == 1
    assert [r.method for r in exchange.requests] == ['POST', 'GET', 'POST', 'GET']


def test_crash_after_durable_presend_intent_blocks_restart(monkeypatch):
    execution_mode.set_mode('demo')
    monkeypatch.setattr(demo, '_preflight', lambda _: None)
    def crash(*args): raise KeyboardInterrupt()
    monkeypatch.setattr(demo, '_create_and_confirm', crash)
    with pytest.raises(KeyboardInterrupt):
        demo.execute_pair(pair(), confirmed=True)
    assert json.loads(demo._journal_path().read_text())['state'] == 'leg_1_submitting'
    with pytest.raises(demo.DemoBlocked, match='reconciliation'):
        demo.execute_pair(pair(), confirmed=True)


def test_rejects_invalid_qty_and_known_live_keys_before_network(monkeypatch):
    execution_mode.set_mode('demo')
    monkeypatch.setattr(demo, '_preflight', lambda _: None)
    with pytest.raises(demo.DemoBlocked, match='quantities'):
        demo.FuturesPair('BTC', 'bybit', 'okx', '1', '0.1', '0.1').validate()
    with pytest.raises(demo.DemoBlocked):
        demo.FuturesPair('BTC', 'bybit', 'okx', 'NaN', '0.1', '0.1').validate()
    monkeypatch.setenv('BYBIT_API_KEY', 'bybit-demo-key')
    with pytest.raises(demo.DemoBlocked, match='credentials'):
        demo._demo_creds('bybit')


def test_redirects_rejected_and_bad_mode_stops_request(monkeypatch):
    with pytest.raises(RuntimeError):
        demo._NoRedirect().redirect_request(None, None, None, None, None, 'https://api.bybit.com')
    exchange = make_mock(monkeypatch)
    monkeypatch.setattr(demo, '_preflight', lambda _: None)
    execution_mode.set_mode('demo')
    original = demo._write_journal
    def switch_after_first(path, data):
        original(path, data)
        if data['state'] == 'leg_1_filled': execution_mode.set_mode('simulation')
    monkeypatch.setattr(demo, '_write_journal', switch_after_first)
    result = demo.execute_pair(pair(), confirmed=True)
    assert result['state'] == 'reconciliation_required' and result['confirmed_legs'] == 1
    assert len(exchange.requests) == 2


def test_okx_first_and_incomplete_okx_fill_blocks_bybit(monkeypatch):
    execution_mode.set_mode('demo')
    exchange = make_mock(monkeypatch)
    exchange.bad_status = {'state': 'partially_filled', 'accFillSz': '0.05'}
    monkeypatch.setattr(demo, '_preflight', lambda _: None)
    result = demo.execute_pair(pair('okx', 'bybit'), confirmed=True)
    assert result['state'] == 'reconciliation_required' and result['confirmed_legs'] == 0
    assert [urlsplit(r.full_url).hostname for r in exchange.requests] == ['www.okx.com', 'www.okx.com']


def test_orders_disabled_on_corrupt_mode_and_invalid_inputs(monkeypatch):
    execution_mode.set_mode('demo')
    exchange = make_mock(monkeypatch)
    monkeypatch.setattr(demo, '_preflight', lambda _: None)
    (execution_mode._directory() / 'execution-mode.json').write_text('{invalid')
    with pytest.raises(RuntimeError):
        demo.execute_pair(pair(), confirmed=True)
    assert not exchange.requests
    with pytest.raises(demo.DemoBlocked):
        demo._order_fields('bitget', 'BTC', 'buy', '1', 'id')


def test_every_private_request_stays_demo_with_explicit_confirmation(monkeypatch):
    execution_mode.set_mode('demo')
    exchange = make_mock(monkeypatch)
    fields = demo._order_fields('okx', 'BTC', 'buy', '1', 'dummyclientid')
    with pytest.raises(demo.DemoBlocked):
        demo._request('okx', 'POST', None, fields, False)
    monkeypatch.setenv('FARB_LIVE', '1')
    with pytest.raises(demo.DemoBlocked):
        demo._request('okx', 'POST', None, fields, True)
    assert not exchange.requests


def test_concurrent_pair_lock_blocks_another_process(monkeypatch):
    import subprocess
    import time
    from execution.demo_futures_recovery import EvidenceBlocked, inspect_journal
    execution_mode.set_mode('demo')
    exchange = make_mock(monkeypatch)
    monkeypatch.setattr(demo, '_preflight', lambda _: None)
    journal = demo._journal_path()
    lock_path = journal.with_suffix('.lock')
    ready = journal.parent / 'lock-ready'
    release = journal.parent / 'lock-release'
    env = os.environ.copy()
    env['PYTHONPATH'] = str(ROOT / 'scripts')
    code = '''import os, sys, time
from pathlib import Path
from core.file_lock import lock_exclusive, unlock
path, ready, release = map(Path, sys.argv[1:])
fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
lock_exclusive(fd)
ready.touch()
end = time.monotonic() + 10
while not release.exists() and time.monotonic() < end:
 time.sleep(.02)
unlock(fd)
os.close(fd)
'''
    child = subprocess.Popen([sys.executable, '-c', code, str(lock_path), str(ready), str(release)], env=env)
    try:
        end = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < end:
            time.sleep(.02)
        assert ready.exists()
        with pytest.raises(demo.DemoBlocked, match='Another demo pair'):
            demo.execute_pair(pair(), confirmed=True)
        with pytest.raises(EvidenceBlocked, match='in use'):
            inspect_journal()
        assert not exchange.requests and not journal.exists()
    finally:
        release.touch()
        try: child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill(); child.wait()


def test_offline_readiness_never_claims_ready_or_sends(monkeypatch, capsys):
    from cli import demo_futures_readiness
    exchange = make_mock(monkeypatch)
    execution_mode.set_mode('demo')
    assert demo_futures_readiness.main() == 2
    assert 'NOT READY' in capsys.readouterr().out
    assert not exchange.requests
    (execution_mode._directory() / 'execution-mode.json').write_text('{broken')
    assert demo_futures_readiness.main() == 2
    assert 'unavailable' in capsys.readouterr().out
    assert not exchange.requests


def test_transport_refuses_spot_non_futures_and_arbitrary_fields(monkeypatch):
    execution_mode.set_mode('demo')
    exchange = make_mock(monkeypatch)
    bybit = demo._order_fields('bybit', 'BTC', 'buy', '0.01', 'fixtureid')
    bybit['category'] = 'spot'
    with pytest.raises(demo.DemoBlocked, match='futures'):
        demo._request('bybit', 'POST', None, bybit, True)
    okx = demo._order_fields('okx', 'BTC', 'buy', '1', 'fixtureid')
    okx['instId'] = 'BTC-USDT'
    with pytest.raises(demo.DemoBlocked, match='futures'):
        demo._request('okx', 'POST', None, okx, True)
    okx['instId'] = 'BTC-USDT-SWAP'
    okx['unexpected'] = 'value'
    with pytest.raises(demo.DemoBlocked, match='Unsupported'):
        demo._request('okx', 'POST', None, okx, True)
    assert not exchange.requests


def test_private_transport_cannot_bypass_pair_preflight_or_journal(monkeypatch):
    execution_mode.set_mode('demo')
    exchange = make_mock(monkeypatch)
    fields = demo._order_fields('bybit', 'BTC', 'buy', '0.01', 'fixtureid')
    with pytest.raises(demo.DemoBlocked, match='journalled demo pair'):
        demo._request('bybit', 'POST', None, fields, True)
    assert not exchange.requests and not demo._journal_path().exists()


def test_acknowledged_order_id_is_durable_before_status_timeout(monkeypatch):
    from execution.demo_futures_recovery import inspect_journal
    execution_mode.set_mode('demo')
    exchange = make_mock(monkeypatch)
    exchange.fail_on = ('bybit', 'GET')
    monkeypatch.setattr(demo, '_preflight', lambda _: None)
    result = demo.execute_pair(pair(), confirmed=True)
    journal = json.loads(demo._journal_path().read_text())
    assert result['state'] == 'reconciliation_required'
    assert journal['legs'][0]['order_id'] == 'bybit-order-1'
    summary = inspect_journal()
    assert summary.state == 'reconciliation_required'
    assert summary.venues == ('bybit', 'okx') and summary.acknowledged == (True, False)
    assert not summary.may_open_new_position
    assert [r.method for r in exchange.requests] == ['POST', 'GET']


def test_journal_diagnostic_fails_closed_on_corruption(monkeypatch, capsys):
    from cli import demo_futures_readiness
    from execution.demo_futures_recovery import EvidenceBlocked, inspect_journal
    assert inspect_journal().state == 'no_journal'
    execution_mode.set_mode('demo')
    path = demo._journal_path()
    path.write_text('{invalid')
    path.chmod(0o600)
    with pytest.raises(EvidenceBlocked):
        inspect_journal()
    assert demo_futures_readiness.main(show_journal=True) == 2
    output = capsys.readouterr().out
    assert 'journal is busy, unavailable or invalid' in output
    assert '{invalid' not in output
    path.write_bytes(b'x' * 1_048_577)
    with pytest.raises(EvidenceBlocked, match='size'):
        inspect_journal()


def test_reconciliation_failure_after_first_fill_blocks_second_leg(monkeypatch):
    execution_mode.set_mode('demo')
    exchange = make_mock(monkeypatch)
    monkeypatch.setattr(demo, '_preflight', lambda _: None)
    def fail(*args):
        raise demo.ReconciliationRequired('position unknown')
    monkeypatch.setattr(demo, '_reconcile_step', fail)
    result = demo.execute_pair(pair(), confirmed=True)
    assert result['state'] == 'reconciliation_required' and result['confirmed_legs'] == 1
    assert [req.method for req in exchange.requests] == ['POST', 'GET']
    journal = json.loads(demo._journal_path().read_text())
    assert journal['state'] == 'reconciliation_required' and journal['legs'][0]['confirmation']['order_id'] == 'bybit-order-1'


def test_reconciliation_hook_runs_between_futures_legs(monkeypatch):
    execution_mode.set_mode('demo')
    exchange = make_mock(monkeypatch)
    monkeypatch.setattr(demo, '_preflight', lambda _: None)
    calls = []
    def check(pair_value, legs, confirmed, risk_limits):
        calls.append((confirmed, len(exchange.requests)))
    monkeypatch.setattr(demo, '_reconcile_step', check)
    result = demo.execute_pair(pair(), confirmed=True)
    assert result['state'] == 'position_reconciliation_required'
    assert calls == [(1, 2), (2, 4)]


@pytest.mark.parametrize('venue, side', [('bybit', 'sell'), ('okx', 'buy')])
def test_reduce_only_futures_fields_cannot_be_silently_disabled(monkeypatch, venue, side):
    exchange = make_mock(monkeypatch)
    fields = demo._order_fields(venue, 'BTC', side, '0.01', 'clientclose', reduce_only=True)
    assert fields['reduceOnly'] is True
    demo._validate_route(venue, 'POST', None, fields)
    fields['reduceOnly'] = False
    with pytest.raises(demo.DemoBlocked):
        demo._validate_route(venue, 'POST', None, fields)
    with pytest.raises(demo.DemoBlocked):
        demo._order_fields(venue, 'BTC', side, '0.01', 'clientclose', reduce_only='true')
    assert not exchange.requests


def test_filled_legs_run_readonly_position_reconciliation_in_order(monkeypatch):
    # Existing preflight stays blocked in production. Here only, it is replaced
    # to exercise the order and reconciliation sequence with fake data.
    import time
    from execution import demo_futures_data
    from execution.demo_futures_preflight import RiskLimits
    execution_mode.set_mode('demo')
    exchange = make_mock(monkeypatch)
    monkeypatch.setattr(demo, '_preflight', lambda *args, **kwargs: None)
    # The module fixture stubs reconciliation for old transport tests; restore
    # the actual hook using the underlying source function captured below.
    real_reconcile = REAL_RECONCILE
    monkeypatch.setattr(demo, '_reconcile_step', real_reconcile)
    reads = []
    def read(venue, base, resource):
        now = int(time.time() * 1000)
        reads.append((venue, resource, len(exchange.requests)))
        fields = exchange.orders.get(venue)
        if venue == 'bybit':
            if resource == 'position':
                obj = {'retCode': 0, 'time': now, 'result': {'list': [{'symbol': 'BTCUSDT', 'positionIdx': 0,
                    'size': fields['qty'] if fields else '0', 'side': 'Buy' if fields else ''}]}}
            elif resource == 'orders':
                obj = {'retCode': 0, 'time': now, 'result': {'list': [], 'nextPageCursor': ''}}
            elif resource == 'conditional_orders':
                obj = {'retCode': 0, 'time': now, 'result': {'list': [], 'nextPageCursor': ''}}
            elif resource == 'fills':
                obj = {'retCode': 0, 'time': now, 'result': {'nextPageCursor': '', 'list': [{
                    'orderLinkId': fields['orderLinkId'], 'orderId': 'bybit-order-1', 'symbol': fields['symbol'],
                    'side': fields['side'], 'execId': 'bybitexec', 'execQty': fields['qty']}]}}
            else: raise AssertionError('unexpected resource')
        else:
            if resource == 'position':
                entries = ([{'instId': 'BTC-USDT-SWAP', 'posSide': 'net', 'mgnMode': 'cross', 'pos': '-' + fields['sz']}] if fields else [])
                obj = {'code': '0', 'data': entries}
            elif resource == 'orders':
                obj = {'code': '0', 'data': []}
            elif resource.startswith('algo_'):
                obj = {'code': '0', 'data': []}
            elif resource == 'fills':
                obj = {'code': '0', 'data': [{'clOrdId': fields['clOrdId'], 'ordId': '123456',
                    'instId': fields['instId'], 'side': fields['side'], 'tradeId': 'okxexec', 'fillSz': fields['sz']}]}
            else: raise AssertionError('unexpected resource')
        return demo_futures_data.ReadResult(obj, now)
    monkeypatch.setattr(demo_futures_data, 'read', read)
    risk = RiskLimits(10_000, 60_000, 30_000, 600_000, 5_000, '10000', '3', '1', '0.001', '0.001', '0.1', '0')
    result = demo.execute_pair(pair(), confirmed=True, risk_limits=risk)
    assert result['state'] == 'position_reconciliation_required' and result['confirmed_legs'] == 2
    assert [req.method for req in exchange.requests] == ['POST', 'GET', 'POST', 'GET']
    assert any(item == ('bybit', 'fills', 2) for item in reads)
    assert any(item == ('okx', 'fills', 4) for item in reads)


def test_duplicate_generated_client_order_ids_block_before_journal_or_network(monkeypatch):
    from types import SimpleNamespace
    execution_mode.set_mode('demo')
    exchange = make_mock(monkeypatch)
    monkeypatch.setattr(demo, '_preflight', lambda _: None)
    monkeypatch.setattr(demo.uuid, 'uuid4', lambda: SimpleNamespace(hex='a' * 32))
    with pytest.raises(demo.DemoBlocked, match='Duplicate client'):
        demo.execute_pair(pair(), confirmed=True)
    assert not exchange.requests and not demo._journal_path().exists()


@pytest.mark.parametrize('venue,side', [('bybit', 'sell'), ('okx', 'buy')])
def test_close_status_must_confirm_reduce_only(monkeypatch, venue, side):
    fields = demo._order_fields(venue, 'BTC', side, '0.01', 'closeclient', reduce_only=True)
    if venue == 'bybit':
        ack = {'retCode': 0, 'result': {'orderId': 'order123'}}
        status = {'retCode': 0, 'result': {'list': [{'orderId': 'order123', 'orderLinkId': 'closeclient',
            'symbol': 'BTCUSDT', 'side': 'Sell', 'orderStatus': 'Filled', 'cumExecQty': '0.01',
            'avgPrice': '100000', 'reduceOnly': False}]}}
    else:
        ack = {'code': '0', 'data': [{'sCode': '0', 'ordId': 'order123'}]}
        status = {'code': '0', 'data': [{'ordId': 'order123', 'clOrdId': 'closeclient',
            'instId': 'BTC-USDT-SWAP', 'side': 'buy', 'state': 'filled', 'accFillSz': '0.01',
            'avgPx': '100000', 'reduceOnly': 'false'}]}
    replies = iter((ack, status))
    monkeypatch.setattr(demo, '_request', lambda *args, **kwargs: next(replies))
    acknowledgements = []
    with pytest.raises(demo.ReconciliationRequired):
        demo._create_and_confirm(venue, fields, True, acknowledgements.append)
    assert acknowledgements == ['order123']
