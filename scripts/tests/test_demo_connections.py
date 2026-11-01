"""Offline safety and routing tests for demo account checks."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / 'scripts'):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from server.routes import demo_connections as demo, positions


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(demo.urllib.request, 'build_opener', lambda *args: (_ for _ in ()).throw(AssertionError('network disabled')))
    monkeypatch.setattr(demo, '_mode', 'simulation')
    async def immediate(fn, *args):
        return fn(*args)
    monkeypatch.setattr(demo.asyncio, 'to_thread', immediate)


class Request:
    def __init__(self, value, content_type='application/json'):
        from types import SimpleNamespace
        self.value = value
        self.headers = {'content-type': content_type}
        self.client = SimpleNamespace(host='127.0.0.1')
        self.url = SimpleNamespace(hostname='127.0.0.1')
    async def json(self):
        return self.value


def run(coro):
    return asyncio.run(coro)


def body(response):
    return json.loads(response.body)


def test_modes_fail_closed_and_demo_requires_explicit_selection():
    assert run(demo.execution_mode())['mode'] == 'simulation'
    for value in ['live', 'testnet', None, '', 'production']:
        response = run(demo.set_execution_mode(Request({'mode': value})))
        assert response.status_code == 400
        assert demo.current_mode() == 'simulation'
    response = run(demo.check_demo(Request({'environment': 'demo'})))
    assert response.status_code == 409
    assert run(demo.set_execution_mode(Request({'mode': 'demo'})))['orders_enabled'] is False
    assert demo.current_mode() == 'demo'
    assert run(demo.set_execution_mode(Request({'mode': 'simulation'})))['mode'] == 'simulation'


@pytest.mark.parametrize('venue,hostname,path,header,payload', [
    ('bybit', 'https://api-demo.bybit.com', '/v5/account/wallet-balance?accountType=UNIFIED&coin=USDT', None,
     {'retCode': 0, 'result': {'list': [{'coin': [{'coin': 'USDT', 'walletBalance': '12.5'}]}]}}),
    ('okx', 'https://www.okx.com', '/api/v5/account/balance?ccy=USDT', 'X-simulated-trading',
     {'code': '0', 'data': [{'details': [{'ccy': 'USDT', 'eq': '12.5'}]}]}),
    ('bitget', 'https://api.bitget.com', '/api/v2/spot/account/assets?coin=USDT', 'Paptrading',
     {'code': '00000', 'data': [{'coin': 'USDT', 'available': '12.5'}]}),
])
def test_demo_only_get_endpoints_and_masked_output(monkeypatch, venue, hostname, path, header, payload):
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
    monkeypatch.setenv('BYBIT_API_KEY', 'production-key-must-not-be-used')
    monkeypatch.setattr(demo, '_mode', 'demo')
    result = run(demo.check_demo(Request({'venue': venue, 'environment': 'demo', 'api_key': 'demo-key',
                                          'api_secret': 'demo-secret', 'passphrase': 'demo-pass'})))
    assert result['balance'] == {'asset': 'USDT', 'amount': '12.5'}
    assert result['credentials'] == '••••' and result['orders_enabled'] is False
    assert all(secret not in repr(result) for secret in ['demo-key', 'demo-secret', 'demo-pass', 'production-key'])
    assert len(calls) == 1 and calls[0].method == 'GET' and calls[0].full_url == hostname + path
    headers = {key.lower(): value for key, value in calls[0].header_items()}
    if header:
        assert headers[header.lower()] == '1'
    if venue == 'bybit':
        assert headers['x-bapi-api-key'] == 'demo-key'
        query = path.split('?', 1)[1]
        text = headers['x-bapi-timestamp'] + 'demo-key' + '5000' + query
        expected = hmac.new(b'demo-secret', text.encode(), hashlib.sha256).hexdigest()
        assert headers['x-bapi-sign'] == expected
    else:
        prefix = 'ok-access-' if venue == 'okx' else 'access-'
        assert headers[prefix + 'key'] == 'demo-key'
        assert headers[prefix + 'passphrase'] == 'demo-pass'
        text = headers[prefix + 'timestamp'] + 'GET' + path
        expected = base64.b64encode(hmac.new(b'demo-secret', text.encode(), hashlib.sha256).digest()).decode()
        assert headers[prefix + 'sign'] == expected


def test_invalid_input_and_remote_failure_never_leak_secrets(monkeypatch, capsys):
    monkeypatch.setattr(demo, '_mode', 'demo')
    secret = 'sensitive-fixture-value'
    calls = []
    def failure(*args):
        calls.append(args[0])
        raise RuntimeError(secret)
    monkeypatch.setattr(demo, '_check', failure)
    for payload in [
        {'venue': 'bybit', 'environment': 'testnet', 'api_key': secret, 'api_secret': secret},
        {'venue': 'unknown', 'environment': 'demo', 'api_key': secret, 'api_secret': secret},
        {'venue': 'okx', 'environment': 'demo', 'api_key': secret, 'api_secret': secret},
    ]:
        response = run(demo.check_demo(Request(payload)))
        assert response.status_code == 400 and secret not in repr(body(response))
    assert not calls
    response = run(demo.check_demo(Request({'venue': 'bybit', 'environment': 'demo', 'api_key': secret, 'api_secret': secret})))
    assert response.status_code == 400 and secret not in repr(body(response))
    assert secret not in repr(capsys.readouterr())
    with pytest.raises(RuntimeError):
        demo._NoRedirect().redirect_request(None, None, None, None, None, 'https://api.bybit.com')


def test_non_simulation_orders_and_real_position_close_are_rejected(monkeypatch):
    req = positions.OpenPositionRequest(base='BTC', amount_usd=20, strategy='carry',
                                        futures_venue='bybit', spot_venue='bybit', dry_run=False)
    monkeypatch.setattr(positions, '_open_cross_fn', lambda *args, **kwargs: pytest.fail('order path invoked'))
    assert run(positions.open_position(req))['success'] is False
    monkeypatch.setattr(demo, '_mode', 'demo')
    req.dry_run = True
    assert run(positions.open_position(req))['success'] is False
    monkeypatch.setattr(positions, '_read_positions', lambda: [{'id': 'xv-1', 'dry_run': False, 'status': 'open'}])
    monkeypatch.setattr(positions, '_close_cross_fn', lambda *args, **kwargs: pytest.fail('close path invoked'))
    assert run(positions.close_position('xv-1'))['success'] is False


def test_remote_or_forwarded_credentials_are_rejected(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(demo, '_mode', 'demo')
    request = Request({'venue': 'bybit', 'environment': 'demo', 'api_key': 'secret', 'api_secret': 'secret'})
    request.client = SimpleNamespace(host='192.0.2.5')
    assert run(demo.check_demo(request)).status_code == 403
    request.client = SimpleNamespace(host='127.0.0.1')
    request.headers['x-forwarded-for'] = '192.0.2.5'
    assert run(demo.check_demo(request)).status_code == 403
    request.headers.pop('x-forwarded-for')
    request.headers['origin'] = 'https://example.com'
    assert run(demo.check_demo(request)).status_code == 403


def test_legacy_credential_routes_do_not_mutate_environment(monkeypatch):
    import os
    from server.routes import settings
    monkeypatch.setenv('BYBIT_API_KEY', 'existing-production-key')
    req = settings.WalletConnectRequest(venue='bybit', credentials={'BYBIT_API_KEY': 'demo-key', 'BYBIT_SECRET_KEY': 'demo-secret'})
    for mode in ('simulation', 'demo'):
        monkeypatch.setattr(demo, '_mode', mode)
        assert run(settings.connect_wallet(req))['success'] is False
        assert os.environ['BYBIT_API_KEY'] == 'existing-production-key'
        assert run(settings.disconnect_wallet(settings.WalletDisconnectRequest(venue='bybit')))['success'] is False
        assert os.environ['BYBIT_API_KEY'] == 'existing-production-key'


def test_paper_close_with_unlabelled_or_live_record_is_never_sent(monkeypatch):
    for record in ({'id': 'xv-1', 'status': 'open'},
                   {'id': 'xv-1', 'status': 'open', 'dry_run': False}):
        monkeypatch.setattr(positions, '_read_positions', lambda: [record])
        monkeypatch.setattr(positions, '_close_cross_fn', lambda *args, **kwargs: pytest.fail('close path invoked'))
        assert run(positions.close_position('xv-1'))['success'] is False


def test_paper_pure_futures_close_forces_dry_run_even_if_record_changes(monkeypatch):
    monkeypatch.setattr(positions, '_read_positions', lambda: [
        {'id': 'p-1', 'strategy': 'pure_futures', 'status': 'open', 'dry_run': True}])
    def close(position_id, *, dry_run):
        assert position_id == 'p-1' and dry_run is True
        return {'closed': True}
    monkeypatch.setattr(positions, '_close_fn', close)
    def immediate(self, executor, fn):
        future = self.create_future()
        future.set_result(fn())
        return future
    monkeypatch.setattr(asyncio.BaseEventLoop, 'run_in_executor', immediate)
    assert run(positions.close_position('p-1'))['success'] is True


def test_http_routes_require_demo_mode_and_return_masked_balance(monkeypatch):
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(demo.router, prefix='/api')
    monkeypatch.setattr(demo, '_check', lambda *args: '7.25')

    async def request(path, payload):
        body_bytes = json.dumps(payload).encode()
        messages = []
        received = False
        async def receive():
            nonlocal received
            if not received:
                received = True
                return {'type': 'http.request', 'body': body_bytes, 'more_body': False}
            return {'type': 'http.disconnect'}
        async def send(message):
            messages.append(message)
        scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                 'method': 'POST', 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
                 'query_string': b'', 'root_path': '', 'server': ('127.0.0.1', 8787),
                 'client': ('127.0.0.1', 30000), 'headers': [(b'host', b'127.0.0.1:8787'),
                                                              (b'content-type', b'application/json')]}
        await app(scope, receive, send)
        return messages[0]['status'], json.loads(messages[1]['body'])

    async def scenario():
        credentials = {'venue': 'bybit', 'environment': 'demo', 'api_key': 'dummy-key', 'api_secret': 'dummy-secret'}
        assert (await request('/api/demo/check', credentials))[0] == 409
        assert (await request('/api/execution-mode', {'mode': 'demo'}))[0] == 200
        status, result = await request('/api/demo/check', credentials)
        assert status == 200 and result['balance'] == {'asset': 'USDT', 'amount': '7.25'}
        assert result['credentials'] == '••••' and 'dummy-' not in repr(result)
    run(scenario())
