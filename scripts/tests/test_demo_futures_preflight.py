"""Offline checks of normalized futures evidence; no exchange I/O."""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / 'scripts') not in sys.path:
    sys.path.insert(0, str(ROOT / 'scripts'))
from execution.demo_futures_preflight import PreflightBlocked, RiskLimits, VenueSnapshot, evaluate

NOW = 1_800_000_000_000


def limits(**changes):
    return replace(RiskLimits(max_snapshot_age_ms=10_000, max_funding_age_ms=60_000,
        min_funding_lead_ms=30_000, max_funding_lead_ms=600_000, max_funding_gap_ms=5_000,
        max_notional_usd='10000', max_leverage='3', max_spread_pct='1',
        max_taker_fee_rate='0.001', max_slippage_rate='0.0005', margin_buffer_rate='0.1',
        min_net_funding_rate='0'), **changes)


def snapshot(venue, **changes):
    return replace(VenueSnapshot(venue=venue, environment='demo',
        instrument='BTCUSDT' if venue == 'bybit' else 'BTC-USDT-SWAP',
        product='linear_perpetual', status='trading', market_timestamp_ms=NOW-100,
        account_timestamp_ms=NOW-200, funding_timestamp_ms=NOW-300,
        next_funding_ms=NOW+300_000, funding_rate='-0.01' if venue == 'bybit' else '0.01',
        mark_price='100000' if venue == 'bybit' else '100050', tick_size='0.1',
        quantity_step='0.002' if venue == 'bybit' else '0.3', min_quantity='0.002' if venue == 'bybit' else '0.3',
        max_quantity='1' if venue == 'bybit' else '100', min_notional_usd='5',
        contract_size_base='1' if venue == 'bybit' else '0.01', leverage='2', available_margin_usd='10000',
        taker_fee_rate='0.0005', slippage_rate='0.0001', margin_mode='cross', position_mode='net',
        position_quantity='0', open_orders=0), **changes)


def run(long=None, short=None, risk=None, amount='0.023', now=NOW):
    return evaluate('BTC', amount, long or snapshot('bybit'), short or snapshot('okx'), risk or limits(), now_ms=now)


def test_floors_to_shared_step_and_converts_contracts_exactly():
    plan = run()
    assert plan.base_quantity == '0.018'  # lcm(0.002, 0.3 * 0.01) = 0.006
    assert plan.okx_contracts == '1.8' and plan.okx_contract_size == '0.01'
    assert plan.long_notional_usd == '1800' and plan.short_notional_usd == '1800.9'
    assert plan.net_funding_after_estimated_costs == '0.0176'
    assert plan.long_venue == 'bybit' and plan.short_venue == 'okx'


def test_reversed_legs_use_funding_direction_and_never_swap_spot_for_futures():
    bybit = snapshot('bybit', funding_rate='0.02')
    okx = snapshot('okx', funding_rate='-0.02')
    plan = run(long=okx, short=bybit)
    assert plan.long_venue == 'okx' and plan.short_venue == 'bybit'
    assert plan.base_quantity == '0.018'
    with pytest.raises(PreflightBlocked):
        run(long=replace(okx, product='spot'), short=bybit)


@pytest.mark.parametrize('change', [
    {'environment': 'production'}, {'environment': 'testnet'}, {'instrument': 'BTC-USDT'},
    {'venue': 'bitget'}, {'venue': None}, {'product': 'spot'}, {'status': 'halted'},
    {'market_timestamp_ms': NOW-10_001}, {'market_timestamp_ms': NOW+1},
    {'account_timestamp_ms': NOW-10_001}, {'funding_timestamp_ms': NOW-60_001},
    {'next_funding_ms': NOW+29_999}, {'next_funding_ms': NOW+600_001},
    {'margin_mode': 'isolated'}, {'position_mode': 'hedge'}, {'position_quantity': '0.001'},
    {'open_orders': 1}, {'open_orders': True}, {'mark_price': 'NaN'}, {'tick_size': '0'},
    {'quantity_step': '0'}, {'min_quantity': '0'}, {'max_quantity': '0'},
    {'min_notional_usd': '0'}, {'contract_size_base': '0'}, {'leverage': '4'},
    {'available_margin_usd': '0'}, {'taker_fee_rate': '0.01'}, {'slippage_rate': '0.01'},
    {'funding_rate': 'Inf'}, {'funding_rate': None}, {'quantity_step': '1e1000'},
])
def test_incomplete_or_unsafe_venue_evidence_is_blocked(change):
    with pytest.raises(PreflightBlocked):
        run(short=replace(snapshot('okx'), **change))


@pytest.mark.parametrize('change', [
    {'max_snapshot_age_ms': 0}, {'max_funding_age_ms': -1}, {'max_funding_gap_ms': -1},
    {'max_notional_usd': '0'}, {'max_notional_usd': '100'}, {'max_leverage': '1'},
    {'max_spread_pct': '-1'}, {'max_spread_pct': '0.01'}, {'max_taker_fee_rate': '0.0001'},
    {'max_slippage_rate': '0.00001'}, {'margin_buffer_rate': '-0.1'},
    {'min_net_funding_rate': '0.9'}, {'min_funding_lead_ms': True},
])
def test_risk_limits_fail_closed(change):
    with pytest.raises(PreflightBlocked):
        run(risk=limits(**change))


def test_funding_times_must_align_and_invalid_leverage_or_minimum_block():
    with pytest.raises(PreflightBlocked, match='funding times'):
        run(short=snapshot('okx', next_funding_ms=NOW+310_000))
    with pytest.raises(PreflightBlocked, match='notional'):
        run(short=snapshot('okx', min_notional_usd='2000'))
    with pytest.raises(PreflightBlocked, match='quantity'):
        run(amount='0.001')
    with pytest.raises(PreflightBlocked):
        run(long=snapshot('bybit', contract_size_base='0.01'))


def test_neither_explicit_invalid_limits_nor_bad_time_are_defaulted():
    with pytest.raises(PreflightBlocked):
        evaluate('BTC', '1', snapshot('bybit'), snapshot('okx'), None, now_ms=NOW)
    with pytest.raises(PreflightBlocked):
        run(now=True)
    with pytest.raises(PreflightBlocked):
        run(amount='NaN')
