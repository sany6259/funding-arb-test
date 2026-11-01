"""Offline evidence for the first Bybit testnet cycle; no network or credentials."""
from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from execution import cross_venue_executor as executor
from venues import bybit


@pytest.fixture(autouse=True)
def block_network_and_secrets(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("offline smoke attempted network or credentials access")
    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(bybit, "_ensure_env", blocked)
    monkeypatch.setattr(executor, "send_notification", lambda *args, **kwargs: None)


class FakeBybitLeg:
    def __init__(self, events, *, fail_short=False, fail_sell=False):
        self.events = events
        self.fail_short = fail_short
        self.fail_sell = fail_sell

    def fetch_symbol_rules(self, pair):
        return {"quantity_precision": 4, "quote_precision": 2,
                "min_trade_usdt": 5, "min_trade_base": 0}

    fetch_futures_symbol_rules = fetch_symbol_rules

    def get_ticker(self, pair):
        return 100.0

    get_futures_ticker = get_ticker

    def fetch_usdt_account_balances(self):
        return {"futures": 10000.0, "spot": 10000.0}

    def initialize_futures_symbol(self, pair):
        pass

    def execute_trades(self, trades, market, dry_run=True):
        results = []
        for trade in trades:
            self.events.append((trade["type"], dry_run, trade["amount_base"]))
            failed = not dry_run and ((self.fail_short and trade["type"] == "open_short") or
                                      (self.fail_sell and trade["type"] == "sell"))
            results.append({"status": "failed" if failed else "simulated" if dry_run else "filled",
                            "exec_qty": trade["amount_base"], "exec_price": 100.0,
                            "error": "definite mock rejection" if failed else None})
        return results


def test_offline_btc_two_leg_cycle_and_definite_second_leg_rejection(tmp_path):
    events = []
    spot = FakeBybitLeg(events)
    futures = FakeBybitLeg(events)
    path = tmp_path / "positions.json"
    # Explicitly injected fakes; default dry_run=True cannot create exchange orders.
    opened = executor.open_cross_venue_position("BTC", "forward", "bybit", "bybit", 100,
                                               futures_venue=futures, spot_venue=spot,
                                               positions_path=path)
    assert opened.ok and opened.state == "simulated"
    assert [event[:2] for event in events] == [("buy", True), ("open_short", True)]
    assert events[0][2] == events[1][2]
    closed = executor.close_cross_venue_position(opened.position_id, futures_venue=futures,
                                                spot_venue=spot, positions_path=path)
    assert closed.ok and closed.state == "simulated"
    assert [event[:2] for event in events[2:]] == [("close_short", True), ("sell", True)]
    assert executor.load_positions(path)[0]["status"] == "closed"

    events.clear()
    futures.fail_short = True
    # Non-dry execution is restricted to in-memory fakes; socket and credentials are blocked.
    failed = executor.open_cross_venue_position("BTC", "forward", "bybit", "bybit", 100,
                                               dry_run=False, futures_venue=futures,
                                               spot_venue=spot, positions_path=tmp_path / "failed.json")
    assert not failed.ok and failed.state == "rolled_back"
    assert [event[0] for event in events] == ["buy", "open_short", "sell"]
    assert events[0][2] == events[2][2]
    assert not (tmp_path / "failed.json").exists()

    events.clear()
    spot.fail_sell = True
    naked = executor.open_cross_venue_position("BTC", "forward", "bybit", "bybit", 100,
                                              dry_run=False, futures_venue=futures,
                                              spot_venue=spot, positions_path=tmp_path / "naked.json")
    assert not naked.ok and naked.state == "naked"
    assert [event[0] for event in events] == ["buy", "open_short", "sell"]
    assert not (tmp_path / "naked.json").exists()


@pytest.mark.parametrize("enabled", ["true", "yes", "on", "", "0", "typo"])
def test_invalid_testnet_flag_fails_before_credentials(monkeypatch, enabled):
    monkeypatch.setenv("BYBIT_REQUIRE_TESTNET", enabled)
    with pytest.raises(RuntimeError, match="exactly 1"):
        bybit._api_call("POST", "/v5/order/create")


def test_testnet_only_routing_and_credentials(monkeypatch):
    import json
    from backtest.funding_providers import BybitFundingProvider
    from venues import get_venue
    calls = []
    monkeypatch.setenv("BYBIT_REQUIRE_TESTNET", "1")
    monkeypatch.setenv("BYBIT_TESTNET_ALLOW_ORDERS", "1")
    monkeypatch.setenv("BYBIT_TESTNET_API_KEY", "test-key")
    monkeypatch.setenv("BYBIT_TESTNET_SECRET_KEY", "test-secret")
    monkeypatch.setenv("BYBIT_API_KEY", "must-not-be-used")
    monkeypatch.setenv("BYBIT_SECRET_KEY", "must-not-be-used")
    assert bybit._get_key() == "test-key"
    assert bybit._get_secret() == "test-secret"

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self): return json.dumps({"retCode": 0, "result": {}}).encode()
    class Opener:
        def open(self, request, timeout):
            calls.append((request.full_url, request.get_header("X-bapi-api-key")))
            return Response()
    monkeypatch.setattr(bybit.urllib.request, "build_opener", lambda handler: Opener())
    bybit._api_call("POST", "/v5/order/create", body={"symbol": "BTCUSDT"})
    assert calls == [("https://api-testnet.bybit.com/v5/order/create", "test-key")]
    assert BybitFundingProvider().BASE == "https://api-testnet.bybit.com"
    with pytest.raises(RuntimeError, match="Bybit venue only"):
        get_venue({"venue": {"type": "binance"}})
    with pytest.raises(RuntimeError, match="redirect rejected"):
        bybit._RejectRedirect().redirect_request(None, None, None, None, None, "https://api.bybit.com")


def test_testnet_uses_separate_keys_and_is_read_only_by_default(monkeypatch):
    monkeypatch.setenv("BYBIT_REQUIRE_TESTNET", "1")
    monkeypatch.delenv("BYBIT_TESTNET_ALLOW_ORDERS", raising=False)
    monkeypatch.setenv("BYBIT_API_KEY", "production-key")
    monkeypatch.setenv("BYBIT_SECRET_KEY", "production-secret")
    with pytest.raises(RuntimeError, match="explicit BYBIT_TESTNET_ALLOW_ORDERS"):
        bybit._api_call("POST", "/v5/order/create")


@pytest.mark.parametrize("status,qty,price", [("New", "0", "0"), ("PartiallyFilled", "0.1", "100"), ("Filled", "0", "0")])
def test_unfilled_order_must_not_be_reported_as_filled(monkeypatch, status, qty, price):
    def fake_api(method, path, params=None, body=None):
        if path == "/v5/order/create":
            return {"result": {"orderId": "mock"}}
        return {"result": {"list": [{"orderStatus": status, "cumExecQty": qty, "avgPrice": price}]}}
    monkeypatch.setattr(bybit, "_api_call", fake_api)
    monkeypatch.setattr(bybit.time, "sleep", lambda *_: None)
    ok, detail = bybit.BybitSpotVenue().place_buy("BTCUSDT", 100, ref_price=100)
    assert not ok and detail["outcome"] == "reconcile_required"
    assert detail["order_id"] == "mock"


def test_unknown_and_partial_outcomes_never_trigger_next_leg_or_rollback(tmp_path):
    class Uncertain(FakeBybitLeg):
        def execute_trades(self, trades, market, dry_run=True):
            self.events.append((trades[0]["type"], dry_run, trades[0]["amount_base"]))
            return [{"status": "reconcile_required", "exec_qty": 0.2, "order_id": "mock"}]
    for first_leg_uncertain in (True, False):
        events = []
        spot = Uncertain(events) if first_leg_uncertain else FakeBybitLeg(events)
        futures = FakeBybitLeg(events) if first_leg_uncertain else Uncertain(events)
        path = tmp_path / f"{first_leg_uncertain}.json"
        result = executor.open_cross_venue_position("BTC", "forward", "bybit", "bybit", 100,
                                                   dry_run=False, futures_venue=futures,
                                                   spot_venue=spot, positions_path=path)
        assert result.state == "reconcile_required" and not result.ok
        assert [event[0] for event in events] == (["buy"] if first_leg_uncertain else ["buy", "open_short"])
        assert executor.load_positions(path)[0]["status"] == "reconcile_required"
        result2 = executor.open_cross_venue_position("BTC", "forward", "bybit", "bybit", 100,
                                                    dry_run=False, futures_venue=futures,
                                                    spot_venue=spot, positions_path=path)
        assert result2.state == "reconcile_required"
        assert len(events) == (1 if first_leg_uncertain else 2)


def test_testnet_credentials_never_fall_back_to_production(monkeypatch):
    monkeypatch.setenv("BYBIT_REQUIRE_TESTNET", "1")
    monkeypatch.setenv("BYBIT_API_KEY", "production-key")
    monkeypatch.setenv("BYBIT_SECRET_KEY", "production-secret")
    monkeypatch.delenv("BYBIT_TESTNET_API_KEY", raising=False)
    monkeypatch.delenv("BYBIT_TESTNET_SECRET_KEY", raising=False)
    assert bybit._get_key() == ""
    assert bybit._get_secret() == ""


def test_read_only_connection_requires_isolated_environment(monkeypatch):
    from cli import bybit_testnet_check
    monkeypatch.setenv("BYBIT_REQUIRE_TESTNET", "1")
    monkeypatch.setenv("FARB_DRY_RUN", "1")
    monkeypatch.setenv("BYBIT_TESTNET_API_KEY", "test-key")
    monkeypatch.setenv("BYBIT_TESTNET_SECRET_KEY", "test-secret")
    for key in ("FARB_LIVE", "DCA_LIVE", "BYBIT_API_KEY", "BYBIT_SECRET_KEY", "BYBIT_TESTNET_ALLOW_ORDERS"):
        monkeypatch.delenv(key, raising=False)
    calls = []
    monkeypatch.setattr(bybit, "_api_call", lambda method, path: calls.append((method, path)))
    bybit_testnet_check.check()
    assert calls == [("GET", "/v5/account/info")]
    monkeypatch.setenv("BYBIT_TESTNET_ALLOW_ORDERS", "1")
    with pytest.raises(RuntimeError, match="read-only"):
        bybit_testnet_check.check()
    assert calls == [("GET", "/v5/account/info")]


def test_uncertain_close_never_sends_next_order(tmp_path):
    class Uncertain(FakeBybitLeg):
        def execute_trades(self, trades, market, dry_run=True):
            self.events.append((trades[0]["type"], dry_run, trades[0]["amount_base"]))
            return [{"status": "reconcile_required", "exec_qty": 0.2, "order_id": "mock"}]
    for futures_uncertain in (True, False):
        events = []
        spot, futures = FakeBybitLeg(events), FakeBybitLeg(events)
        path = tmp_path / f"close-{futures_uncertain}.json"
        opened = executor.open_cross_venue_position("BTC", "forward", "bybit", "bybit", 100,
                                                   dry_run=False, futures_venue=futures,
                                                   spot_venue=spot, positions_path=path)
        assert opened.ok
        events.clear()
        if futures_uncertain:
            futures = Uncertain(events)
        else:
            spot = Uncertain(events)
        result = executor.close_cross_venue_position(opened.position_id, futures_venue=futures,
                                                    spot_venue=spot, positions_path=path)
        assert result.state == "reconcile_required"
        assert [event[0] for event in events] == (["close_short"] if futures_uncertain else ["close_short", "sell"])
        assert executor.load_positions(path)[0]["status"] == "reconcile_required"


def test_futures_partial_filled_quantity_never_claims_success(monkeypatch):
    monkeypatch.setattr(bybit.BybitSpotVenue, "initialize_futures_symbol", lambda *_: None)
    monkeypatch.setattr(bybit.time, "sleep", lambda *_: None)
    def fake_api(method, path, params=None, body=None):
        if method == "POST":
            return {"result": {"orderId": "mock"}}
        return {"result": {"list": [{"orderStatus": "Filled", "cumExecQty": "0.5", "avgPrice": "100"}]}}
    monkeypatch.setattr(bybit, "_api_call", fake_api)
    ok, detail = bybit.BybitSpotVenue().place_futures_order("BTCUSDT", "open_short", 1, ref_price=100)
    assert not ok and detail["outcome"] == "reconcile_required"


def test_unknown_submission_is_not_a_rejection(monkeypatch):
    monkeypatch.setattr(bybit, "_api_call", lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("mock timeout")))
    ok, detail = bybit.BybitSpotVenue().place_buy("BTCUSDT", 100, ref_price=100)
    assert not ok and detail["outcome"] == "reconcile_required"
    assert detail["order_link_id"].startswith("qbuy")


def test_close_futures_order_is_reduce_only(monkeypatch):
    calls = []
    monkeypatch.setenv("BYBIT_REQUIRE_TESTNET", "1")
    monkeypatch.setattr(bybit.time, "sleep", lambda *_: None)
    def fake_api(method, path, params=None, body=None):
        if method == "POST":
            calls.append(body)
            return {"result": {"orderId": "mock"}}
        return {"result": {"list": [{"orderStatus": "Filled", "cumExecQty": "1", "avgPrice": "100"}]}}
    monkeypatch.setattr(bybit, "_api_call", fake_api)
    ok, _ = bybit.BybitSpotVenue().place_futures_order("BTCUSDT", "close_short", 1, ref_price=100)
    assert ok and calls[0]["reduceOnly"] is True


def test_public_market_funding_and_depth_stay_on_testnet(monkeypatch):
    from backtest.funding_providers import BybitFundingProvider
    from market.futures_depth import fetch_futures_depth
    monkeypatch.setenv("BYBIT_REQUIRE_TESTNET", "1")
    calls = []
    class Response:
        status_code = 200
        def json(self):
            return {"result": {"list": [{"symbol": "BTCUSDT", "lastPrice": "100", "fundingRate": "0.0001"}], "b": [["100", "1"]], "a": [["101", "1"]]}}
    def fake_get(url, timeout, allow_redirects):
        calls.append((url, allow_redirects))
        return Response()
    monkeypatch.setattr(bybit.requests, "get", fake_get)
    assert bybit.BybitSpotVenue().get_ticker("BTCUSDT") == 100
    assert BybitFundingProvider().fetch_current("BTCUSDT")["rate_pct"] == 0.01
    assert fetch_futures_depth("bybit", "BTC")["bids"] == [(100.0, 1.0)]
    assert all(url.startswith("https://api-testnet.bybit.com/v5/") and not redirect for url, redirect in calls)
    with pytest.raises(RuntimeError, match="public request rejected"):
        bybit._public_get("https://api.bybit.com/v5/market/tickers")
    class Redirect(Response):
        status_code = 302
    monkeypatch.setattr(bybit.requests, "get", lambda *args, **kwargs: Redirect())
    with pytest.raises(RuntimeError, match="Unexpected"):
        bybit._public_get("https://api-testnet.bybit.com/v5/market/tickers")


def test_mocked_testnet_two_leg_orders_use_testnet_only(monkeypatch, tmp_path):
    import json
    import urllib.parse
    monkeypatch.setenv("BYBIT_REQUIRE_TESTNET", "1")
    monkeypatch.setenv("BYBIT_TESTNET_ALLOW_ORDERS", "1")
    monkeypatch.setenv("BYBIT_TESTNET_API_KEY", "test-key")
    monkeypatch.setenv("BYBIT_TESTNET_SECRET_KEY", "test-secret")
    monkeypatch.setattr(bybit.time, "sleep", lambda *_: None)
    urls, orders = [], []
    class Response:
        def __init__(self, data): self.data = data
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self): return json.dumps(self.data).encode()
    class Opener:
        def open(self, request, timeout):
            urls.append(request.full_url)
            parsed = urllib.parse.urlsplit(request.full_url)
            assert parsed.hostname == "api-testnet.bybit.com"
            if request.method == "POST":
                orders.append(json.loads(request.data))
                return Response({"retCode": 0, "result": {"orderId": str(len(orders))}})
            return Response({"retCode": 0, "result": {"list": [{"orderStatus": "Filled", "cumExecQty": "1", "avgPrice": "100"}]}})
    monkeypatch.setattr(bybit.urllib.request, "build_opener", lambda handler: Opener())
    venue = bybit.BybitSpotVenue()
    rules = {"quantity_precision": 3, "quote_precision": 2, "min_trade_usdt": 5, "min_trade_base": 0}
    monkeypatch.setattr(venue, "fetch_asset_market", lambda *_: {"pair": "BTCUSDT", "price": 100, "symbol_rules": rules})
    monkeypatch.setattr(venue, "fetch_futures_symbol_rules", lambda *_: rules)
    monkeypatch.setattr(venue, "get_futures_ticker", lambda *_: 100)
    path = tmp_path / "testnet-positions.json"
    opened = executor.open_cross_venue_position("BTC", "forward", "bybit", "bybit", 100,
                                               dry_run=False, futures_venue=venue,
                                               spot_venue=venue, positions_path=path)
    assert opened.ok and opened.state == "filled"
    closed = executor.close_cross_venue_position(opened.position_id, futures_venue=venue,
                                                spot_venue=venue, positions_path=path)
    assert closed.ok and closed.state == "filled"
    assert [(order["category"], order["side"]) for order in orders] == [
        ("spot", "Buy"), ("linear", "Sell"), ("linear", "Buy"), ("spot", "Sell")]
    assert orders[2]["reduceOnly"] is True
    assert len(urls) == 8 and all(url.startswith("https://api-testnet.bybit.com/") for url in urls)


def test_dashboard_fails_closed_in_testnet_mode(monkeypatch):
    import runpy
    monkeypatch.setenv("BYBIT_REQUIRE_TESTNET", "1")
    with pytest.raises(RuntimeError, match="dashboard isolation is not implemented"):
        runpy.run_module("server.main", run_name="testnet_dashboard_check")


def test_authenticated_request_keeps_initial_testnet_mode(monkeypatch):
    import json
    monkeypatch.setenv("BYBIT_TESTNET_API_KEY", "test-key")
    monkeypatch.setenv("BYBIT_TESTNET_SECRET_KEY", "test-secret")
    calls = []
    modes = iter([True, False, False])
    monkeypatch.setattr(bybit, "_testnet_only", lambda: next(modes))
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self): return json.dumps({"retCode": 0, "result": {}}).encode()
    class Opener:
        def open(self, request, timeout):
            calls.append((request.full_url, request.get_header("X-bapi-api-key")))
            return Response()
    monkeypatch.setattr(bybit.urllib.request, "build_opener", lambda handler: Opener())
    bybit._api_call("GET", "/v5/account/info")
    assert calls == [("https://api-testnet.bybit.com/v5/account/info", "test-key")]
