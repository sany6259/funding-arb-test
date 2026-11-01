"""Offline regression tests for credentials, shared mode and demo routing."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / 'scripts'):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
from core import demo_credentials, execution_mode
from server.routes import demo_connections as demo, positions
REAL_KEYRING = demo_credentials._keyring


class FakeKeyring:
    def __init__(self): self.data = {}
    def set_password(self, service, name, value): self.data[(service, name)] = value
    def get_password(self, service, name): return self.data.get((service, name))
    def delete_password(self, service, name): del self.data[(service, name)]


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(execution_mode, '_directory', lambda: tmp_path / 'state')
    monkeypatch.setattr(demo_credentials, '_keyring', lambda: FakeKeyringInstance)
    FakeKeyringInstance.data.clear()
    monkeypatch.setattr(demo.urllib.request, 'build_opener', lambda *args: pytest.fail('unexpected network'))
    async def immediate(fn, *args): return fn(*args)
    monkeypatch.setattr(demo.asyncio, 'to_thread', immediate)


FakeKeyringInstance = FakeKeyring()


class Request:
    def __init__(self, value):
        self.value = value
        self.headers = {'content-type': 'application/json'}
        self.client = SimpleNamespace(host='127.0.0.1')
        self.url = SimpleNamespace(hostname='127.0.0.1')
    async def json(self): return self.value


def run(coro): return asyncio.run(coro)
def body(response): return json.loads(response.body)
def data(venue='bybit', **kwargs): return {'venue': venue, 'environment': 'demo', **kwargs}
def save(venue='bybit', key='demo-key', secret='demo-secret', passphrase='demo-pass'):
    return run(demo.save_credentials(Request(data(venue, api_key=key, api_secret=secret, passphrase=passphrase))))


def test_modes_persist_and_fail_closed(tmp_path):
    assert run(demo.execution_mode())['mode'] == 'simulation'
    for value in ('live', 'testnet', 'production', None):
        assert run(demo.set_execution_mode(Request({'mode': value}))).status_code == 400
    assert run(demo.set_execution_mode(Request({'mode': 'demo'})))['orders_enabled'] is False
    assert run(demo.execution_mode())['mode'] == 'demo'
    assert json.loads((tmp_path / 'state' / 'execution-mode.json').read_text())['orders_enabled'] is False
    (tmp_path / 'state' / 'execution-mode.json').write_text('{broken')
    assert run(demo.execution_mode()).status_code == 503
    assert run(demo.set_execution_mode(Request({'mode': 'simulation'}))).status_code == 503
    with pytest.raises(RuntimeError): execution_mode.forbid_orders()


def test_modes_are_shared_between_processes_and_inconsistent_state_denies_orders(tmp_path, monkeypatch):
    # Both processes use the same user home; no network or credentials are involved.
    monkeypatch.setattr(execution_mode, '_directory', lambda: tmp_path / '.funding-arb')
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    execution_mode.set_mode('demo')
    env = os.environ.copy()
    env['PYTHONPATH'] = str(ROOT / 'scripts')
    code = ('from core.execution_mode import current_mode,forbid_orders; '
            'print(current_mode()); '\
            '\ntry: forbid_orders()\nexcept RuntimeError: print("DENIED")')
    result = subprocess.run([sys.executable, '-c', code], env=env, text=True, capture_output=True, check=True)
    assert result.stdout.splitlines() == ['demo', 'DENIED']
    (tmp_path / '.funding-arb' / 'execution-mode.json').write_text('{"version":1,"mode":"live","orders_enabled":true}')
    result = subprocess.run([sys.executable, '-c', code], env=env, text=True, capture_output=True)
    assert result.returncode != 0
    with pytest.raises(RuntimeError): execution_mode.forbid_orders()


def test_save_update_delete_and_no_secrets_in_responses(monkeypatch):
    execution_mode.set_mode('demo')
    for venue in ('bybit', 'okx', 'bitget'):
        result = save(venue, key='mock-key', secret='mock-secret', passphrase='mock-pass')
        assert result['configured'] and not result['connected']
        assert all(x not in repr(result) for x in ('mock-key', 'mock-secret', 'mock-pass'))
    assert all(run(demo.credential_status(Request({})))['configured'].values())
    save('bybit', key='updated-key')
    assert demo_credentials.load('bybit', 'demo')['key'] == 'updated-key'
    assert demo_credentials.load('okx', 'demo')['key'] == 'mock-key'
    assert run(demo.delete_credentials(Request(data())))['configured'] is False
    assert demo_credentials.load('bybit', 'demo') is None
    assert demo_credentials.load('okx', 'demo') is not None
    with pytest.raises(ValueError): demo_credentials.load('okx', 'live')
    assert run(demo.check_demo(Request(data()))).status_code == 409
    monkeypatch.setenv('BYBIT_API_KEY', 'existing-live-key')
    assert save(key='existing-live-key').status_code == 400


def test_refuses_untrusted_keyring(monkeypatch):
    # A plaintext/keyring chainer is not an acceptable fallback.
    import keyring
    monkeypatch.setattr(keyring, 'get_keyring', lambda: object())
    with pytest.raises(RuntimeError): REAL_KEYRING()


@pytest.mark.parametrize('venue,base,path,header,payload,asset,account', [
    ('bybit', 'https://api-demo.bybit.com', '/v5/account/wallet-balance?accountType=UNIFIED&coin=USDT', None,
     {'retCode': 0, 'result': {'list': [{'totalAvailableBalance': '12.5'}]}}, 'USD', 'Unified Account'),
    ('okx', 'https://www.okx.com', '/api/v5/account/balance?ccy=USDT', 'X-simulated-trading',
     {'code': '0', 'data': [{'details': [{'ccy': 'USDT', 'availBal': '12.5'}]}]}, 'USDT', 'Trading Account'),
    ('bitget', 'https://api.bitget.com', '/api/v2/spot/account/assets?coin=USDT', 'Paptrading',
     {'code': '00000', 'data': [{'coin': 'USDT', 'available': '12.5'}]}, 'USDT', 'Spot Account'),
])
def test_demo_only_get_endpoints_headers_and_signatures(monkeypatch, venue, base, path, header, payload, asset, account):
    calls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self, limit): return json.dumps(payload).encode()
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            return Response()
    monkeypatch.setattr(demo.urllib.request, 'build_opener', lambda handler: Opener())
    execution_mode.set_mode('demo')
    assert save(venue)['configured']
    result = run(demo.check_demo(Request(data(venue))))
    assert result['balance'] == {'asset': asset, 'amount': '12.5', 'account_type': account}
    assert result['credentials'] == '••••' and result['orders_enabled'] is False
    assert all(x not in repr(result) for x in ('demo-key', 'demo-secret', 'demo-pass'))
    assert len(calls) == 1 and calls[0].method == 'GET' and calls[0].full_url == base + path
    headers = {k.lower(): v for k, v in calls[0].header_items()}
    if header: assert headers[header.lower()] == '1'
    if venue == 'bybit':
        text = headers['x-bapi-timestamp'] + 'demo-key' + '5000' + path.split('?', 1)[1]
        assert headers['x-bapi-sign'] == hmac.new(b'demo-secret', text.encode(), hashlib.sha256).hexdigest()
    else:
        prefix = 'ok-access-' if venue == 'okx' else 'access-'
        text = headers[prefix + 'timestamp'] + 'GET' + path
        assert headers[prefix + 'sign'] == base64.b64encode(hmac.new(b'demo-secret', text.encode(), hashlib.sha256).digest()).decode()
        assert headers[prefix + 'passphrase'] == 'demo-pass'


def test_failure_no_leaks_or_false_connected(monkeypatch, capsys):
    execution_mode.set_mode('demo')
    secret = 'sensitive-fixture'
    save(key=secret, secret=secret)
    monkeypatch.setattr(demo, '_check', lambda *a: (_ for _ in ()).throw(RuntimeError(secret)))
    result = run(demo.check_demo(Request(data())))
    assert result.status_code == 400 and secret not in repr(body(result)) and 'connected' not in body(result)
    assert secret not in repr(capsys.readouterr())
    for venue, payload in [('bybit', {'retCode': 10003}), ('okx', {'code': '50111'}), ('bitget', {'code': '40009'})]:
        with pytest.raises(ValueError): demo._balance(venue, payload)
    with pytest.raises(RuntimeError): demo._NoRedirect().redirect_request(None, None, None, None, None, 'https://api.bybit.com')


def test_remote_and_forwarded_requests_rejected():
    execution_mode.set_mode('demo')
    req = Request(data())
    req.client = SimpleNamespace(host='192.0.2.5')
    assert run(demo.check_demo(req)).status_code == 403
    req.client = SimpleNamespace(host='127.0.0.1')
    req.headers['x-forwarded-for'] = '192.0.2.5'
    assert run(demo.check_demo(req)).status_code == 403
    req.headers.pop('x-forwarded-for')
    req.headers['origin'] = 'https://example.com'
    assert run(demo.check_demo(req)).status_code == 403


def test_legacy_routes_and_position_orders_blocked(monkeypatch):
    from server.routes import settings
    monkeypatch.setenv('BYBIT_API_KEY', 'existing-production-key')
    request = settings.WalletConnectRequest(venue='bybit', credentials={'BYBIT_API_KEY': 'demo-key'})
    position = positions.OpenPositionRequest(base='BTC', amount_usd=20, strategy='carry', futures_venue='bybit', spot_venue='bybit', dry_run=False)
    for mode in ('simulation', 'demo'):
        execution_mode.set_mode(mode)
        assert run(settings.connect_wallet(request))['success'] is False
        assert run(settings.disconnect_wallet(settings.WalletDisconnectRequest(venue='bybit')))['success'] is False
        assert os.environ['BYBIT_API_KEY'] == 'existing-production-key'
        assert run(positions.open_position(position))['success'] is False
        monkeypatch.setattr(positions, '_read_positions', lambda: [{'id': 'xv-1', 'dry_run': False, 'status': 'open'}])
        assert run(positions.close_position('xv-1'))['success'] is False


def test_unlabelled_positions_are_not_closed(monkeypatch):
    monkeypatch.setattr(positions, '_read_positions', lambda: [{'id': 'xv-1', 'status': 'open'}])
    monkeypatch.setattr(positions, '_close_cross_fn', lambda *args, **kwargs: pytest.fail('order path invoked'))
    assert run(positions.close_position('xv-1'))['success'] is False


def test_bybit_testnet_guard_remains_additive(monkeypatch):
    from venues import bybit
    execution_mode.set_mode('demo')
    monkeypatch.setenv('BYBIT_REQUIRE_TESTNET', '1')
    monkeypatch.setenv('BYBIT_TESTNET_ALLOW_ORDERS', '1')
    monkeypatch.setenv('BYBIT_TESTNET_API_KEY', 'mock-key')
    monkeypatch.setenv('BYBIT_TESTNET_SECRET_KEY', 'mock-secret')
    with pytest.raises(RuntimeError, match='Orders disabled'):
        bybit._api_call('POST', '/v5/order/create', body={'symbol': 'BTCUSDT'})


def test_paper_pure_futures_close_forces_dry_run(monkeypatch):
    monkeypatch.setattr(positions, '_read_positions', lambda: [{'id': 'p-1', 'strategy': 'pure_futures', 'status': 'open', 'dry_run': True}])
    def close(position_id, *, dry_run):
        assert position_id == 'p-1' and dry_run is True
        return {'closed': True}
    monkeypatch.setattr(positions, '_close_fn', close)
    def immediate(self, executor, fn):
        future = self.create_future(); future.set_result(fn()); return future
    monkeypatch.setattr(asyncio.BaseEventLoop, 'run_in_executor', immediate)
    assert run(positions.close_position('p-1'))['success'] is True


@pytest.mark.parametrize('module', ['bybit', 'okx', 'bitget', 'binance', 'aster'])
def test_adapters_reject_write_before_any_network(module):
    import importlib
    adapter = importlib.import_module('venues.' + module)
    for mode in ('simulation', 'demo'):
        execution_mode.set_mode(mode)
        with pytest.raises(RuntimeError, match='Orders disabled'):
            adapter._api_call('POST', '/v5/order/create' if module == 'bybit' else '/order')


def test_saved_key_survives_a_new_backend_instance(monkeypatch):
    persistent = {}
    def new_backend():
        ring = FakeKeyring()
        ring.data = persistent
        return ring
    monkeypatch.setattr(demo_credentials, '_keyring', new_backend)
    execution_mode.set_mode('demo')
    assert save('bybit')['configured'] is True
    # A later request obtains a fresh keyring instance, as after a server restart.
    assert demo_credentials.load('bybit', 'demo')['key'] == 'demo-key'
    assert run(demo.credential_status(Request({})))['configured']['bybit'] is True


def test_store_failures_are_not_reported_as_connected(monkeypatch, capsys):
    execution_mode.set_mode('demo')
    secret = 'mock-private-value'
    def failure(): raise RuntimeError(secret)
    monkeypatch.setattr(demo_credentials, '_keyring', failure)
    response = save(key=secret, secret=secret)
    assert response.status_code == 503 and secret not in repr(body(response))
    assert run(demo.credential_status(Request({}))).status_code == 503
    assert secret not in repr(capsys.readouterr())


def test_dex_sdk_order_helpers_fail_before_signing(monkeypatch):
    from venues.dydx import _submit_market_order
    from venues.edgex import EdgexVenue
    from venues.lighter import LighterVenue
    execution_mode.set_mode('demo')
    with pytest.raises(RuntimeError, match='Orders disabled'):
        _submit_market_order(None, None, 'BTC', 'BTC-USD', 'BUY', 1, {}, 0)
    with pytest.raises(RuntimeError, match='Orders disabled'):
        run(EdgexVenue()._submit_limit_order('1', '1', '1', 'BUY'))
    with pytest.raises(RuntimeError, match='Orders disabled'):
        run(LighterVenue()._submit_market_order(1, 1, 1, True, False))


def test_http_demo_lifecycle_uses_no_secret_in_response(monkeypatch):
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(demo.router, prefix='/api')
    monkeypatch.setattr(demo, '_check', lambda *args: '7.25')
    async def request(path, payload):
        content = json.dumps(payload).encode()
        messages = []
        received = False
        async def receive():
            nonlocal received
            if not received:
                received = True
                return {'type': 'http.request', 'body': content, 'more_body': False}
            return {'type': 'http.disconnect'}
        async def send(message): messages.append(message)
        scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                 'method': 'POST', 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
                 'query_string': b'', 'root_path': '', 'server': ('127.0.0.1', 8787),
                 'client': ('127.0.0.1', 30000), 'headers': [(b'host', b'127.0.0.1:8787'),
                                                              (b'content-type', b'application/json')]}
        await app(scope, receive, send)
        return messages[0]['status'], json.loads(messages[1]['body'])
    async def scenario():
        credentials = data(api_key='dummy-key', api_secret='dummy-secret')
        assert (await request('/api/demo/credentials', credentials))[0] == 409
        assert (await request('/api/execution-mode', {'mode': 'demo'}))[0] == 200
        status, response = await request('/api/demo/credentials', credentials)
        assert status == 200 and response['connected'] is False and 'dummy-' not in repr(response)
        status, response = await request('/api/demo/check', data())
        assert status == 200 and response['balance']['amount'] == '7.25' and 'dummy-' not in repr(response)
        assert (await request('/api/demo/credentials/delete', data()))[0] == 200
        assert (await request('/api/demo/check', data()))[0] == 409
    run(scenario())
