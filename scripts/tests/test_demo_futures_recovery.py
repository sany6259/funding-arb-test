"""Offline recovery evidence and close-intent checks; no network or orders."""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / 'scripts') not in sys.path:
    sys.path.insert(0, str(ROOT / 'scripts'))
from execution.demo_futures_recovery import (CloseIntent, EvidenceBlocked, Execution, ExpectedLeg,
    OrderEvidence, PositionEvidence, assess_order, assess_pair, plan_close)

NOW = 1_800_000_000_000


def leg(venue, **changes):
    return replace(ExpectedLeg(venue, 'BTC', 'long' if venue == 'bybit' else 'short',
                               venue + 'client', venue + 'order', '0.01' if venue == 'bybit' else '0.1',
                               '1' if venue == 'bybit' else '0.1'), **changes)


def order(venue, **changes):
    expected = leg(venue)
    return replace(OrderEvidence(venue, 'demo', 'BTCUSDT' if venue == 'bybit' else 'BTC-USDT-SWAP',
        'buy' if venue == 'bybit' else 'sell', expected.client_id, expected.order_id,
        'filled', expected.quantity, expected.quantity, '100000',
        (Execution(venue + 'exec1', expected.quantity),), NOW-100), **changes)


def position(venue, **changes):
    return replace(PositionEvidence(venue, 'demo', 'BTCUSDT' if venue == 'bybit' else 'BTC-USDT-SWAP',
        'net', '0.01' if venue == 'bybit' else '-0.1', 0, NOW-100), **changes)


def check(legs=None, orders=None, positions=None, **kwargs):
    return assess_pair(legs or (leg('bybit'), leg('okx')), orders or (order('bybit'), order('okx')),
                       positions or (position('bybit'), position('okx')),
                       now_ms=NOW, max_age_ms=10_000, baseline_zero_confirmed=kwargs.get('baseline', True))


def test_two_complete_futures_evidence_never_authorizes_new_trade():
    result = check()
    assert result.status == 'consistent_offline' and result.order_states == ('filled', 'filled')
    assert result.may_open_new_position is False


def test_reduce_only_close_plan_requires_owner_and_never_sends():
    args = ((leg('bybit'), leg('okx')), (order('bybit'), order('okx')), (position('bybit'), position('okx')))
    with pytest.raises(EvidenceBlocked, match='confirmation'):
        plan_close(*args, now_ms=NOW, max_age_ms=10_000, baseline_zero_confirmed=True, owner_confirmed=False)
    result = plan_close(*args, now_ms=NOW, max_age_ms=10_000, baseline_zero_confirmed=True, owner_confirmed=True)
    assert result == (CloseIntent('bybit', 'BTCUSDT', 'sell', '0.01'), CloseIntent('okx', 'BTC-USDT-SWAP', 'buy', '0.1'))
    assert all(item.reduce_only for item in result)


@pytest.mark.parametrize('change', [
    {'environment': 'production'}, {'environment': 'testnet'}, {'venue': 'bitget'},
    {'instrument': 'BTC-USDT'}, {'side': 'sell'}, {'client_id': 'wrong'}, {'order_id': 'wrong'},
    {'requested_quantity': '0.02'}, {'filled_quantity': '0.02'}, {'average_price': '0'},
    {'timestamp_ms': NOW+1}, {'timestamp_ms': NOW-10_001}, {'executions': None},
    {'executions': (Execution('dup', '0.005'), Execution('dup', '0.005'))},
    {'executions': (Execution('a', '0.005'),)}, {'executions': (Execution('a', '0'),)},
    {'filled_quantity': 'NaN'}, {'filled_quantity': '0e-999999999'},
])
def test_order_evidence_mismatch_blocks(change):
    with pytest.raises(EvidenceBlocked):
        assess_order(leg('bybit'), replace(order('bybit'), **change), now_ms=NOW, max_age_ms=10_000)


@pytest.mark.parametrize('change, expected', [
    ({'status': 'new', 'filled_quantity': '0', 'average_price': '0', 'executions': ()}, 'pending_or_partial'),
    ({'status': 'partially_filled', 'filled_quantity': '0.005', 'executions': (Execution('part', '0.005'),)}, 'pending_or_partial'),
    ({'status': 'cancelled', 'filled_quantity': '0', 'average_price': '0', 'executions': ()}, 'terminal_zero'),
    ({'status': 'filled', 'filled_quantity': '0', 'average_price': '0', 'executions': ()}, 'unknown'),
    ({'status': 'unknown'}, 'unknown'),
])
def test_status_never_confuses_ack_or_partial_with_complete_fill(change, expected):
    assert assess_order(leg('bybit'), replace(order('bybit'), **change), now_ms=NOW, max_age_ms=10_000) == expected


def test_partial_fill_blocks_close_plan_even_if_position_matches_observed_fill():
    partial = order('bybit', status='partially_filled', filled_quantity='0.005', executions=(Execution('part', '0.005'),))
    args = ((leg('bybit'), leg('okx')), (partial, order('okx')), (position('bybit', signed_quantity='0.005'), position('okx')))
    result = assess_pair(*args, now_ms=NOW, max_age_ms=10_000, baseline_zero_confirmed=True)
    assert result.status == 'manual_reconciliation_required' and not result.may_open_new_position
    with pytest.raises(EvidenceBlocked, match='manual'):
        plan_close(*args, now_ms=NOW, max_age_ms=10_000, baseline_zero_confirmed=True, owner_confirmed=True)


@pytest.mark.parametrize('change', [
    {'environment': 'production'}, {'instrument': 'BTC-USDT'}, {'position_mode': 'hedge'},
    {'signed_quantity': '0.02'}, {'open_orders': 1}, {'open_orders': True},
    {'timestamp_ms': NOW-10_001}, {'timestamp_ms': NOW+1}, {'signed_quantity': '0e-999999999'},
])
def test_mismatched_positions_never_allow_recovery(change):
    with pytest.raises(EvidenceBlocked):
        check(positions=(position('bybit', **change), position('okx')))


def test_baseline_identity_and_contract_size_are_mandatory():
    with pytest.raises(EvidenceBlocked, match='baseline'):
        check(baseline=False)
    with pytest.raises(EvidenceBlocked, match='base quantities'):
        check(legs=(leg('bybit'), leg('okx', contract_size_base='1')))
    with pytest.raises(EvidenceBlocked, match='pair identity'):
        check(legs=(leg('bybit'), leg('okx', client_id='bybitclient')))
    with pytest.raises(EvidenceBlocked):
        check(legs=(leg('bybit'), replace(leg('okx'), venue='bitget')))
