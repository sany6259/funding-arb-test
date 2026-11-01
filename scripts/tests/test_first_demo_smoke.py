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
    monkeypatch.setattr(bybit, "_get_key", blocked)
    monkeypatch.setattr(bybit, "_get_secret", blocked)
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


@pytest.mark.parametrize("enabled", ["1", "true", "yes", "on", "", "0", "typo"])
def test_require_testnet_blocks_authenticated_bybit_before_credentials(monkeypatch, enabled):
    monkeypatch.setenv("BYBIT_REQUIRE_TESTNET", enabled)
    for method, path in [("POST", "/v5/order/create"), ("GET", "/v5/account/wallet-balance")]:
        with pytest.raises(RuntimeError, match="testnet is not supported"):
            bybit._api_call(method, path)


@pytest.mark.xfail(strict=True, reason="Bybit adapter has no testnet-only routing; BASE is production")
def test_testnet_only_routing_is_not_yet_supported():
    assert bybit.BASE == "https://api-testnet.bybit.com"


@pytest.mark.xfail(strict=True, reason="unconfirmed Bybit order is currently reported successful")
def test_unfilled_order_must_not_be_reported_as_filled(monkeypatch):
    def fake_api(method, path, params=None, body=None):
        if path == "/v5/order/create":
            return {"result": {"orderId": "mock"}}
        return {"result": {"list": [{"orderStatus": "New", "cumExecQty": "0"}]}}
    monkeypatch.setattr(bybit, "_api_call", fake_api)
    monkeypatch.setattr(bybit.time, "sleep", lambda *_: None)
    ok, _ = bybit.BybitSpotVenue().place_buy("BTCUSDT", 100, ref_price=100)
    assert not ok
