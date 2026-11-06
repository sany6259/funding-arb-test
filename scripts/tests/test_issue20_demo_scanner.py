"""Offline regression tests for anonymous demo market scanning."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
for item in (ROOT, ROOT / 'scripts'):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

from core import execution_mode
from execution import demo_futures
from core import demo_credentials
from server.routes import scanner
from venues import bybit, http_util, okx


def run(coro):
    loop = asyncio.new_event_loop()
    async def with_clock():
        task = asyncio.create_task(coro)
        deadline = loop.time() + 5
        while not task.done():
            if loop.time() >= deadline:
                task.cancel()
                raise TimeoutError('scanner did not finish')
            await asyncio.sleep(0.01)
        return task.result()
    try:
        return loop.run_until_complete(with_clock())
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(execution_mode, '_directory', lambda: tmp_path / 'state')
    monkeypatch.delenv('FARB_DRY_RUN', raising=False)
    monkeypatch.delenv('BYBIT_REQUIRE_TESTNET', raising=False)
    monkeypatch.setattr(scanner, '_scanning_strategies', set())
    monkeypatch.setattr(scanner, '_result_modes', {})
    monkeypatch.setenv('BYBIT_API_KEY', 'dummy-production-key')
    monkeypatch.setenv('OKX_API_KEY', 'dummy-production-key')
    monkeypatch.setattr(demo_credentials, '_backend', lambda: 'age')
    monkeypatch.setattr(demo_credentials, 'load', lambda venue, environment: {
        'key': venue + '-demo-key', 'secret': 'demo-secret', 'passphrase': 'demo-pass'})
    async def no_broadcast(*args):
        return None
    monkeypatch.setattr(scanner, '_broadcast', no_broadcast)


class MarketTransport:
    def __init__(self):
        self.calls = []

    def open(self, req, timeout):
        self.calls.append(req)
        url = urlsplit(req.full_url)
        headers = {k.lower(): v for k, v in req.header_items()}
        assert req.method == 'GET'
        if url.path not in ('/v5/account/fee-rate', '/api/v5/account/trade-fee'):
            assert not ({'authorization', 'x-bapi-api-key', 'ok-access-key', 'cookie'} & headers.keys()) or url.path in (
                '/v5/market/tickers', '/v5/market/orderbook', '/v5/market/instruments-info',
                '/api/v5/market/books', '/api/v5/public/instruments', '/api/v5/public/mark-price')
        assert 'dummy-production-key' not in req.full_url + str(headers)
        now = int(time.time() * 1000)
        if url.hostname == 'api-demo.bybit.com' and url.path == '/v5/market/tickers':
            assert parse_qs(url.query)['category'] == ['linear']
            result = {'retCode': 0, 'time': now, 'result': {'list': [{'symbol': 'BTCUSDT', 'fundingRate': '-0.01',
                'markPrice': '100000', 'indexPrice': '100000', 'nextFundingTime': str(now + 3600000)}]}}
        elif url.hostname == 'www.okx.com' and url.path == '/api/v5/public/funding-rate':
            assert headers['x-simulated-trading'] == '1'
            result = {'code': '0', 'data': [{'instId': 'BTC-USDT-SWAP', 'fundingRate': '0.01',
                'fundingTime': str(now + 3600000), 'nextFundingTime': str(now + 9 * 3600000)}]}
        elif url.hostname == 'www.okx.com' and url.path == '/api/v5/public/mark-price':
            assert headers['x-simulated-trading'] == '1'
            result = {'code': '0', 'data': [{'instId': 'BTC-USDT-SWAP', 'markPx': '100000', 'ts': str(now)}]}
        elif url.hostname == 'api-demo.bybit.com' and url.path == '/v5/market/orderbook':
            result = {'retCode': 0, 'time': now, 'result': {'s': 'BTCUSDT', 'ts': now, 'b': [['99999', '2']], 'a': [['100001', '2']]}}
        elif url.hostname == 'www.okx.com' and url.path == '/api/v5/market/books':
            assert headers['x-simulated-trading'] == '1'
            result = {'code': '0', 'data': [{'ts': str(now), 'bids': [['99999', '100']], 'asks': [['100001', '100']]}]}
        elif url.hostname == 'www.okx.com' and url.path == '/api/v5/public/instruments':
            assert headers['x-simulated-trading'] == '1'
            result = {'code': '0', 'data': [{'instId': 'BTC-USDT-SWAP', 'ctVal': '0.01', 'ctMult': '2', 'lotSz': '1'}]}
        elif url.hostname == 'api-demo.bybit.com' and url.path == '/v5/market/instruments-info':
            result = {'retCode': 0, 'time': now, 'result': {'list': [{'symbol': 'BTCUSDT', 'lotSizeFilter': {'qtyStep': '0.001'}}]}}
        elif url.hostname == 'api-demo.bybit.com' and url.path == '/v5/account/fee-rate':
            assert headers['x-bapi-api-key'] == 'bybit-demo-key'
            result = {'retCode': 0, 'result': {'list': [{'symbol': 'BTCUSDT', 'takerFeeRate': '0.0005'}]}}
        elif url.hostname == 'www.okx.com' and url.path == '/api/v5/account/trade-fee':
            assert headers['ok-access-key'] == 'okx-demo-key' and headers['x-simulated-trading'] == '1'
            result = {'code': '0', 'data': [{'taker': '-0.0005'}]}
        else:
            raise AssertionError('unexpected real endpoint')
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, limit): return json.dumps(result).encode()
        return Response()


def test_demo_scanner_uses_only_anonymous_isolated_market_gets(monkeypatch):
    execution_mode.set_mode('demo')
    fake = MarketTransport()
    monkeypatch.setattr(http_util.urllib.request, 'build_opener', lambda *_: fake)
    result = run(scanner.scanner_trigger(strategy='pure', venues='bybit,okx'))
    assert result['success'] is True
    assert result['data']['market_environment'] == 'demo'
    assert set(result['data']['data_venues']) == {'bybit', 'okx'}
    assert result['data']['total_assets_scanned'] == 1
    rows = result['data']['forward'] + result['data']['reverse']
    assert rows and rows[0]['depth_ok'] is True and rows[0]['max_exec_usd'] > 0
    assert rows[0]['demo_costs_verified'] is True and rows[0]['fee_source'] == 'demo_account'
    assert fake.calls and execution_mode.current_state()['orders_enabled'] is False
    assert all(urlsplit(req.full_url).hostname in ('api-demo.bybit.com', 'www.okx.com') for req in fake.calls)


def test_demo_scanner_reports_missing_market_data_as_failure(monkeypatch):
    execution_mode.set_mode('demo')
    class Offline:
        def open(self, *_args, **_kwargs):
            raise OSError('mock unavailable')
    monkeypatch.setattr(http_util.urllib.request, 'build_opener', lambda *_: Offline())
    result = run(scanner.scanner_trigger(strategy='pure', venues='bybit,okx'))
    assert result['success'] is False
    assert 'недоступны' in result['error']


def test_demo_market_request_holds_mode_lock_until_response(monkeypatch):
    import threading
    execution_mode.set_mode('demo')
    entered, release, changed = threading.Event(), threading.Event(), threading.Event()
    fake = MarketTransport()
    original = fake.open
    def blocking(req, timeout):
        entered.set()
        assert release.wait(3)
        return original(req, timeout)
    fake.open = blocking
    monkeypatch.setattr(http_util.urllib.request, 'build_opener', lambda *_: fake)
    errors = []
    def read():
        try:
            http_util.http_get_json('https://api.bybit.com/v5/market/tickers?category=linear')
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=read)
    def change():
        execution_mode.set_mode('simulation')
        changed.set()
    worker.start()
    assert entered.wait(3)
    changer = threading.Thread(target=change)
    changer.start()
    try:
        assert not changed.wait(0.1)
    finally:
        release.set()
        worker.join(3)
        changer.join(3)
    assert not errors and changed.is_set()


def test_simulation_scanner_still_dispatches(monkeypatch):
    execution_mode.set_mode('simulation')
    seen = []
    def scan(**kwargs):
        seen.append(kwargs)
        return {'forward': [], 'reverse': [], 'venues': kwargs['venues']}
    monkeypatch.setattr(scanner, '_scan_pure_fn', scan)
    result = run(scanner.scanner_trigger(strategy='pure', venues='bybit,okx'))
    assert result['success'] and seen[0]['venues'] == ['bybit', 'okx']
    assert 'cache_ttl_sec' not in seen[0]


@pytest.mark.parametrize('strategy', ['carry', 'unified'])
def test_demo_does_not_start_spot_scanners(monkeypatch, strategy):
    execution_mode.set_mode('demo')
    seen = []
    def carry(**kwargs):
        seen.append(kwargs)
        return {'forward_candidates': [], 'reverse_candidates': [], 'total_pairs': 0}
    class Unified:
        def __init__(self, **kwargs):
            seen.append(kwargs)
            self.legs_by_base = {'BTC': []}
        def refresh(self): pass
        def scan_routes(self, **kwargs): return {}
    monkeypatch.setattr(scanner, '_scan_carry_fn', carry)
    monkeypatch.setattr(scanner, '_unified_carry_cls', Unified)
    result = run(scanner.scanner_trigger(strategy=strategy, venues='bybit,okx,bitget'))
    assert result['success'] is False
    assert not seen
    assert execution_mode.current_state()['orders_enabled'] is False


def test_unknown_mode_and_dry_run_block_scanning_before_network(monkeypatch):
    execution_mode.set_mode('demo')
    monkeypatch.setattr(http_util.urllib.request, 'build_opener', lambda *_: pytest.fail('network called'))
    monkeypatch.setenv('FARB_DRY_RUN', '1')
    assert not run(scanner.scanner_trigger(strategy='pure'))['success']
    assert not run(scanner.scanner_scan_all())['success']
    with pytest.raises(execution_mode.LegacyAccessBlocked):
        http_util.http_get_json('https://api.bybit.com/v5/market/tickers?category=linear')
    with pytest.raises(execution_mode.LegacyAccessBlocked):
        execution_mode.simulation_request(lambda: pytest.fail('network called'))
    execution_mode.set_mode('simulation')
    with pytest.raises(execution_mode.LegacyAccessBlocked):
        execution_mode.require_simulation_api()
    execution_mode.set_mode('demo')
    monkeypatch.delenv('FARB_DRY_RUN')
    (execution_mode._directory() / 'execution-mode.json').write_text('{invalid')
    assert not run(scanner.scanner_trigger(strategy='pure'))['success']
    assert not run(scanner.scanner_scan_all())['success']
    with pytest.raises(execution_mode.LegacyAccessBlocked):
        http_util.http_get_json('https://api.bybit.com/v5/market/tickers?category=linear')


def test_cached_simulation_results_are_not_shown_as_demo(monkeypatch):
    execution_mode.set_mode('demo')
    monkeypatch.setattr(scanner, '_pure_results', {'venues': ['bybit'], 'forward': [{'base': 'BTC'}], 'reverse': []})
    monkeypatch.setattr(scanner, '_result_modes', {'pure': 'simulation'})
    result = run(scanner.scanner_opportunities(strategy='pure', venues=None))
    assert result['has_data'] is False and result['data']['forward'] == []


def test_demo_default_venue_selection_can_retrieve_its_cached_results(monkeypatch):
    execution_mode.set_mode('demo')
    monkeypatch.setattr(scanner, '_pure_results', {'venues': ['bybit', 'okx'], 'forward': [{'base': 'BTC'}], 'reverse': []})
    monkeypatch.setattr(scanner, '_result_modes', {'pure': 'demo'})
    result = run(scanner.scanner_opportunities(strategy='pure', venues='binance,bitget,bybit,okx'))
    assert result['has_data'] is True and result['data']['forward'][0]['base'] == 'BTC'


@pytest.mark.parametrize('url', [
    'https://api.bybit.com/v5/order/create',
    'https://www.okx.com/api/v5/trade/order',
    'https://api.bybit.com/v5/market/tickers?category=linear&apiKey=secret',
    'https://api.bybit.com@attacker.invalid/v5/market/tickers?category=linear',
    'http://api.bybit.com/v5/market/tickers?category=linear',
    'https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES',
])
def test_demo_market_allowlist_never_opens_private_or_unknown_routes(monkeypatch, url):
    execution_mode.set_mode('demo')
    monkeypatch.setattr(http_util.urllib.request, 'build_opener', lambda *_: pytest.fail('network called'))
    with pytest.raises(execution_mode.LegacyAccessBlocked):
        http_util.http_get_json(url)


def test_demo_public_scanner_does_not_enable_orders_or_private_api(monkeypatch):
    execution_mode.set_mode('demo')
    monkeypatch.setattr(http_util.urllib.request, 'build_opener', lambda *_: pytest.fail('network called'))
    for venue, path in ((bybit, '/v5/order/create'), (okx, '/api/v5/trade/order')):
        with pytest.raises(RuntimeError):
            venue._api_call('POST', path, body={})
    with pytest.raises(demo_futures.DemoBlocked):
        demo_futures.execute_pair(demo_futures.FuturesPair('BTC', 'bybit', 'okx', '0.01', '1', '0.01'), confirmed=True)
    assert execution_mode.current_state()['orders_enabled'] is False


def test_dry_run_blocks_demo_balance_and_telegram_network(monkeypatch, capsys):
    from core.notify import send_notification
    from notify.telegram_push import send_telegram_message
    from server.routes import demo_connections
    execution_mode.set_mode('demo')
    monkeypatch.setenv('FARB_DRY_RUN', '1')
    monkeypatch.setattr(http_util.urllib.request, 'urlopen', lambda *a, **k: pytest.fail('network called'))
    monkeypatch.setattr(http_util.urllib.request, 'build_opener', lambda *a: pytest.fail('network called'))
    cfg = {'telegram_bot_token': 'dummy-token', 'telegram_chat_id': 'dummy-chat'}
    assert send_notification('test', 'test', cfg) is False
    with pytest.raises(execution_mode.LegacyAccessBlocked):
        send_telegram_message('test', cfg)
    with pytest.raises(execution_mode.LegacyAccessBlocked):
        demo_connections._check('bybit', 'dummy-key', 'dummy-secret', '')
    assert 'dummy-token' not in capsys.readouterr().err


def test_redirect_is_rejected():
    with pytest.raises(RuntimeError):
        http_util._NoRedirect().redirect_request(None, None, None, None, None, 'https://attacker.invalid/')
