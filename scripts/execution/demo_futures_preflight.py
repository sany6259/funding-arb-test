"""Offline, fail-closed validation of normalized demo futures snapshots.

This optional evaluator does not obtain exchange data and is not an order
permission. A caller-provided ``environment`` label cannot prove provenance.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction


class PreflightBlocked(ValueError):
    """Required evidence is missing, stale, inconsistent or outside a limit."""


@dataclass(frozen=True)
class TechnicalLimits:
    max_snapshot_age_ms: int


@dataclass(frozen=True)
class RiskLimits:
    max_snapshot_age_ms: int
    max_funding_age_ms: int
    min_funding_lead_ms: int
    max_funding_lead_ms: int
    max_funding_gap_ms: int
    max_notional_usd: str
    max_leverage: str
    max_spread_pct: str
    max_taker_fee_rate: str
    max_slippage_rate: str
    margin_buffer_rate: str
    min_net_funding_rate: str


@dataclass(frozen=True)
class VenueSnapshot:
    venue: str
    environment: str
    instrument: str
    product: str
    status: str
    market_timestamp_ms: int
    account_timestamp_ms: int
    funding_timestamp_ms: int
    next_funding_ms: int
    funding_rate: str
    mark_price: str
    tick_size: str
    quantity_step: str
    min_quantity: str
    max_quantity: str
    min_notional_usd: str
    contract_size_base: str
    leverage: str
    available_margin_usd: str
    taker_fee_rate: str
    slippage_rate: str
    margin_mode: str
    position_mode: str
    position_quantity: str
    open_orders: int


@dataclass(frozen=True)
class CheckedPlan:
    base: str
    long_venue: str
    short_venue: str
    base_quantity: str
    okx_contracts: str
    okx_contract_size: str
    long_notional_usd: str
    short_notional_usd: str
    net_funding_after_estimated_costs: str


def _decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise PreflightBlocked(f'{name}: invalid decimal')
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        raise PreflightBlocked(f'{name}: invalid decimal') from None
    if (not number.is_finite() or number.adjusted() > 12 or number.adjusted() < -12 or
            len(number.as_tuple().digits) > 24 or abs(number.as_tuple().exponent) > 24):
        raise PreflightBlocked(f'{name}: invalid decimal')
    if positive and number <= 0:
        raise PreflightBlocked(f'{name}: must be positive')
    return number


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= 10**15:
        raise PreflightBlocked(f'{name}: invalid integer')
    return value


def _within(value: Decimal, lower: Decimal, upper: Decimal, name: str) -> None:
    if not lower <= value <= upper:
        raise PreflightBlocked(f'{name}: outside risk limit')


def _fresh(timestamp: object, now: int, maximum_age: int, name: str) -> None:
    stamp = _integer(timestamp, name, minimum=1)
    if stamp > now or now - stamp > maximum_age:
        raise PreflightBlocked(f'{name}: stale or future timestamp')


def _common_base_step(first: Decimal, second: Decimal) -> Decimal:
    a, b = Fraction(first), Fraction(second)
    result = Fraction(math.lcm(a.numerator, b.numerator), math.gcd(a.denominator, b.denominator))
    with localcontext() as ctx:
        ctx.prec = 80
        return Decimal(result.numerator) / Decimal(result.denominator)


def _plain(value: Decimal) -> str:
    return format(value.normalize(), 'f')


def evaluate(base: str, requested_base_quantity: str, long: VenueSnapshot, short: VenueSnapshot,
             limits: RiskLimits, *, now_ms: int) -> CheckedPlan:
    """Check one Bybit/OKX linear-perpetual pair without performing I/O.

    Contract quantities are floored to a shared base-asset step. No missing
    account, funding, instrument, order or risk input is assumed safe.
    """
    if not isinstance(base, str) or not re.fullmatch(r'[A-Z][A-Z0-9]{1,14}', base):
        raise PreflightBlocked('invalid base')
    if not isinstance(long, VenueSnapshot) or not isinstance(short, VenueSnapshot) or not isinstance(limits, RiskLimits):
        raise PreflightBlocked('missing normalized evidence')
    if (not isinstance(long.venue, str) or not isinstance(short.venue, str) or
            {long.venue, short.venue} != {'bybit', 'okx'}):
        raise PreflightBlocked('exactly Bybit and OKX are required')
    now = _integer(now_ms, 'now', minimum=1)
    age = _integer(limits.max_snapshot_age_ms, 'max_snapshot_age', minimum=1)
    funding_age = _integer(limits.max_funding_age_ms, 'max_funding_age', minimum=1)
    min_lead = _integer(limits.min_funding_lead_ms, 'min_funding_lead', minimum=1)
    max_lead = _integer(limits.max_funding_lead_ms, 'max_funding_lead', minimum=min_lead)
    max_gap = _integer(limits.max_funding_gap_ms, 'max_funding_gap')
    max_notional = _decimal(limits.max_notional_usd, 'max_notional', positive=True)
    max_leverage = _decimal(limits.max_leverage, 'max_leverage', positive=True)
    max_spread = _decimal(limits.max_spread_pct, 'max_spread')
    max_fee = _decimal(limits.max_taker_fee_rate, 'max_fee')
    max_slippage = _decimal(limits.max_slippage_rate, 'max_slippage')
    buffer = _decimal(limits.margin_buffer_rate, 'margin_buffer')
    min_net = _decimal(limits.min_net_funding_rate, 'min_net_funding')
    for value, name in ((max_spread, 'max_spread'), (max_fee, 'max_fee'),
                        (max_slippage, 'max_slippage'), (buffer, 'margin_buffer')):
        _within(value, Decimal('0'), Decimal('1'), name)
    _within(min_net, Decimal('-1'), Decimal('1'), 'min_net_funding')
    requested = _decimal(requested_base_quantity, 'requested quantity', positive=True)
    parsed: dict[str, dict[str, Decimal]] = {}
    next_times = []
    for snapshot in (long, short):
        venue = snapshot.venue
        expected = base + ('USDT' if venue == 'bybit' else '-USDT-SWAP')
        if (snapshot.environment != 'demo' or snapshot.instrument != expected or
                snapshot.product != 'linear_perpetual' or snapshot.status != 'trading'):
            raise PreflightBlocked('unsupported environment or instrument')
        if snapshot.margin_mode != 'cross' or snapshot.position_mode != 'net':
            raise PreflightBlocked('unsupported margin or position mode')
        if _decimal(snapshot.position_quantity, 'position quantity') != 0 or _integer(snapshot.open_orders, 'open orders') != 0:
            raise PreflightBlocked('existing position or open order')
        _fresh(snapshot.market_timestamp_ms, now, age, 'market timestamp')
        _fresh(snapshot.account_timestamp_ms, now, age, 'account timestamp')
        _fresh(snapshot.funding_timestamp_ms, now, funding_age, 'funding timestamp')
        funding_time = _integer(snapshot.next_funding_ms, 'next funding', minimum=1)
        if not min_lead <= funding_time - now <= max_lead:
            raise PreflightBlocked('funding time outside permitted window')
        next_times.append(funding_time)
        values = {
            'price': _decimal(snapshot.mark_price, 'mark price', positive=True),
            'tick': _decimal(snapshot.tick_size, 'tick size', positive=True),
            'step': _decimal(snapshot.quantity_step, 'quantity step', positive=True),
            'min': _decimal(snapshot.min_quantity, 'minimum quantity', positive=True),
            'max': _decimal(snapshot.max_quantity, 'maximum quantity', positive=True),
            'min_notional': _decimal(snapshot.min_notional_usd, 'minimum notional', positive=True),
            'contract': _decimal(snapshot.contract_size_base, 'contract size', positive=True),
            'leverage': _decimal(snapshot.leverage, 'leverage', positive=True),
            'margin': _decimal(snapshot.available_margin_usd, 'available margin'),
            'fee': _decimal(snapshot.taker_fee_rate, 'taker fee'),
            'slippage': _decimal(snapshot.slippage_rate, 'slippage'),
            'funding': _decimal(snapshot.funding_rate, 'funding rate'),
        }
        if values['min'] > values['max'] or values['leverage'] > max_leverage:
            raise PreflightBlocked('instrument limits or leverage invalid')
        if venue == 'bybit' and values['contract'] != 1:
            raise PreflightBlocked('Bybit linear quantity must be in base units')
        _within(values['margin'], Decimal('0'), Decimal('1e12'), 'available margin')
        _within(values['fee'], Decimal('0'), max_fee, 'taker fee')
        _within(values['slippage'], Decimal('0'), max_slippage, 'slippage')
        _within(values['funding'], Decimal('-1'), Decimal('1'), 'funding')
        parsed[venue] = values
    if abs(next_times[0] - next_times[1]) > max_gap:
        raise PreflightBlocked('funding times do not align')
    bybit, okx = parsed['bybit'], parsed['okx']
    with localcontext() as ctx:
        ctx.prec = 80
        step = _common_base_step(bybit['step'], okx['step'] * okx['contract'])
        quantity = (requested // step) * step
        contracts = quantity / okx['contract']
        if quantity <= 0 or contracts * okx['contract'] != quantity:
            raise PreflightBlocked('quantity cannot be represented on both venues')
        if not bybit['min'] <= quantity <= bybit['max'] or not okx['min'] <= contracts <= okx['max']:
            raise PreflightBlocked('quantity outside instrument limits')
        prices = (parsed[long.venue]['price'], parsed[short.venue]['price'])
        spread_pct = abs(prices[0] - prices[1]) / max(prices) * 100
        if spread_pct > max_spread:
            raise PreflightBlocked('mark spread exceeds limit')
        notionals: dict[str, Decimal] = {}
        for snapshot in (long, short):
            values = parsed[snapshot.venue]
            notional = quantity * values['price']
            if not values['min_notional'] <= notional <= max_notional:
                raise PreflightBlocked('notional outside permitted limits')
            required = notional / values['leverage'] * (1 + buffer) + notional * 2 * (values['fee'] + values['slippage'])
            if values['margin'] < required:
                raise PreflightBlocked('insufficient demo margin')
            notionals[snapshot.venue] = notional
        net = parsed[short.venue]['funding'] - parsed[long.venue]['funding'] - 2 * sum(
            (item['fee'] + item['slippage'] for item in parsed.values()), Decimal('0'))
        if net < min_net:
            raise PreflightBlocked('funding after estimated costs below limit')
        return CheckedPlan(base, long.venue, short.venue, _plain(quantity), _plain(contracts),
                           _plain(okx['contract']), _plain(notionals[long.venue]),
                           _plain(notionals[short.venue]), _plain(net))
