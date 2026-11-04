"""Offline evidence checks and reduce-only close planning; never sends orders.

This does not verify the provenance of caller-supplied evidence or unlock the
persistent journal. Exchange-backed recovery and safe execution remain blocked.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext


class EvidenceBlocked(ValueError):
    """Evidence is missing, inconsistent, stale or cannot be trusted."""


@dataclass(frozen=True)
class Execution:
    execution_id: str
    quantity: str


@dataclass(frozen=True)
class ExpectedLeg:
    venue: str
    base: str
    direction: str
    client_id: str
    order_id: str
    quantity: str
    contract_size_base: str


@dataclass(frozen=True)
class OrderEvidence:
    venue: str
    environment: str
    instrument: str
    side: str
    client_id: str
    order_id: str
    status: str
    requested_quantity: str
    filled_quantity: str
    average_price: str
    executions: tuple[Execution, ...]
    timestamp_ms: int


@dataclass(frozen=True)
class PositionEvidence:
    venue: str
    environment: str
    instrument: str
    position_mode: str
    signed_quantity: str
    open_orders: int
    timestamp_ms: int


@dataclass(frozen=True)
class RecoveryAssessment:
    status: str
    order_states: tuple[str, str]
    # This is deliberately always false: caller-supplied evidence alone cannot
    # authorize a new entry, clear a journal, or prove a live exchange state.
    may_open_new_position: bool = False


@dataclass(frozen=True)
class CloseIntent:
    venue: str
    instrument: str
    side: str
    quantity: str
    reduce_only: bool = True


def _number(value: object, label: str, *, signed: bool = False) -> Decimal:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise EvidenceBlocked(f'{label}: invalid quantity')
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        raise EvidenceBlocked(f'{label}: invalid quantity') from None
    if (not number.is_finite() or number.adjusted() > 12 or number.adjusted() < -12 or
            len(number.as_tuple().digits) > 24 or abs(number.as_tuple().exponent) > 24 or
            (not signed and number < 0)):
        raise EvidenceBlocked(f'{label}: invalid quantity')
    return number


def _fresh(stamp: object, now: object, max_age_ms: object) -> None:
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (stamp, now, max_age_ms)):
        raise EvidenceBlocked('invalid observation time')
    if not 0 < stamp <= now <= 10**15 or not 0 < max_age_ms <= 10**12 or now - stamp > max_age_ms:
        raise EvidenceBlocked('stale or future observation')


def _expected(leg: ExpectedLeg) -> tuple[str, str, Decimal]:
    if not isinstance(leg, ExpectedLeg) or leg.venue not in ('bybit', 'okx') or leg.direction not in ('long', 'short'):
        raise EvidenceBlocked('unsupported futures leg')
    if not isinstance(leg.base, str) or not re.fullmatch(r'[A-Z][A-Z0-9]{1,14}', leg.base):
        raise EvidenceBlocked('invalid underlying')
    if not isinstance(leg.client_id, str) or not re.fullmatch(r'[A-Za-z0-9]{1,24}', leg.client_id):
        raise EvidenceBlocked('invalid client order id')
    if not isinstance(leg.order_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', leg.order_id):
        raise EvidenceBlocked('order id unavailable')
    qty = _number(leg.quantity, 'expected quantity')
    contract = _number(leg.contract_size_base, 'contract size')
    if qty <= 0 or contract <= 0 or (leg.venue == 'bybit' and contract != 1):
        raise EvidenceBlocked('invalid expected quantity or contract size')
    instrument = leg.base + ('USDT' if leg.venue == 'bybit' else '-USDT-SWAP')
    side = 'buy' if leg.direction == 'long' else 'sell'
    return instrument, side, qty


def assess_order(leg: ExpectedLeg, evidence: OrderEvidence, *, now_ms: int, max_age_ms: int) -> str:
    """Classify a single observed order, requiring distinct execution records."""
    instrument, side, qty = _expected(leg)
    if not isinstance(evidence, OrderEvidence):
        raise EvidenceBlocked('order evidence unavailable')
    _fresh(evidence.timestamp_ms, now_ms, max_age_ms)
    if (evidence.venue != leg.venue or evidence.environment != 'demo' or evidence.instrument != instrument or
            evidence.client_id != leg.client_id or evidence.order_id != leg.order_id or evidence.side != side):
        raise EvidenceBlocked('order identity or environment mismatch')
    requested = _number(evidence.requested_quantity, 'requested quantity')
    filled = _number(evidence.filled_quantity, 'filled quantity')
    price = _number(evidence.average_price, 'average price')
    if requested != qty or filled > requested or not isinstance(evidence.executions, tuple) or len(evidence.executions) > 1000:
        raise EvidenceBlocked('order quantity mismatch')
    ids: set[str] = set()
    with localcontext() as ctx:
        ctx.prec = 100
        total = Decimal(0)
        for execution in evidence.executions:
            if not isinstance(execution, Execution) or not isinstance(execution.execution_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', execution.execution_id):
                raise EvidenceBlocked('invalid execution evidence')
            if execution.execution_id in ids:
                raise EvidenceBlocked('duplicate execution identifier')
            ids.add(execution.execution_id)
            item_qty = _number(execution.quantity, 'execution quantity')
            if item_qty <= 0:
                raise EvidenceBlocked('execution quantity must be positive')
            total += item_qty
    if total != filled or (filled > 0 and price <= 0):
        raise EvidenceBlocked('fill and execution evidence disagree')
    if evidence.status == 'filled' and filled == qty and price > 0:
        return 'filled'
    if evidence.status in ('cancelled', 'rejected') and filled == 0:
        return 'terminal_zero'
    if evidence.status in ('new', 'partially_filled', 'cancelled') and filled < qty:
        return 'pending_or_partial'
    return 'unknown'


def _position(leg: ExpectedLeg, position: PositionEvidence, *, now_ms: int, max_age_ms: int) -> Decimal:
    instrument, _, _ = _expected(leg)
    if not isinstance(position, PositionEvidence):
        raise EvidenceBlocked('position evidence unavailable')
    _fresh(position.timestamp_ms, now_ms, max_age_ms)
    if (position.venue != leg.venue or position.environment != 'demo' or position.instrument != instrument or
            position.position_mode != 'net'):
        raise EvidenceBlocked('position identity or mode mismatch')
    if isinstance(position.open_orders, bool) or not isinstance(position.open_orders, int) or position.open_orders != 0:
        raise EvidenceBlocked('open or unknown orders')
    return _number(position.signed_quantity, 'signed position', signed=True)


def assess_pair(legs: tuple[ExpectedLeg, ExpectedLeg], orders: tuple[OrderEvidence, OrderEvidence],
                positions: tuple[PositionEvidence, PositionEvidence], *, now_ms: int, max_age_ms: int,
                baseline_zero_confirmed: bool) -> RecoveryAssessment:
    """Compare order/fill/position evidence. This never authorizes a trade."""
    if (not isinstance(legs, tuple) or not isinstance(orders, tuple) or not isinstance(positions, tuple) or
            len(legs) != 2 or len(orders) != 2 or len(positions) != 2 or baseline_zero_confirmed is not True):
        raise EvidenceBlocked('two legs and a confirmed zero baseline are required')
    first, second = legs
    _expected(first)
    _expected(second)
    if ({first.venue, second.venue} != {'bybit', 'okx'} or first.base != second.base or
            {first.direction, second.direction} != {'long', 'short'} or first.client_id == second.client_id):
        raise EvidenceBlocked('pair identity mismatch')
    with localcontext() as ctx:
        ctx.prec = 100
        if (_number(first.quantity, 'quantity') * _number(first.contract_size_base, 'contract size') !=
                _number(second.quantity, 'quantity') * _number(second.contract_size_base, 'contract size')):
            raise EvidenceBlocked('futures base quantities do not match')
    states = tuple(assess_order(leg, order, now_ms=now_ms, max_age_ms=max_age_ms)
                   for leg, order in zip(legs, orders))
    quantities = tuple(_position(leg, pos, now_ms=now_ms, max_age_ms=max_age_ms)
                       for leg, pos in zip(legs, positions))
    for leg, order, quantity in zip(legs, orders, quantities):
        filled = _number(order.filled_quantity, 'filled quantity')
        expected_position = filled if leg.direction == 'long' else -filled
        if quantity != expected_position:
            raise EvidenceBlocked('position does not match observed fills')
    if states == ('filled', 'filled'):
        return RecoveryAssessment('consistent_offline', states)
    return RecoveryAssessment('manual_reconciliation_required', states)


def plan_close(legs: tuple[ExpectedLeg, ExpectedLeg], orders: tuple[OrderEvidence, OrderEvidence],
               positions: tuple[PositionEvidence, PositionEvidence], *, now_ms: int, max_age_ms: int,
               baseline_zero_confirmed: bool, owner_confirmed: bool) -> tuple[CloseIntent, CloseIntent]:
    """Plan opposite reduce-only intents for a fully evidenced pair, offline.

    This function does not submit or sign anything and does not provide an
    emergency bypass. Partial, unknown or inconsistent evidence remains blocked.
    """
    if owner_confirmed is not True:
        raise EvidenceBlocked('explicit owner confirmation required')
    assessment = assess_pair(legs, orders, positions, now_ms=now_ms, max_age_ms=max_age_ms,
                             baseline_zero_confirmed=baseline_zero_confirmed)
    if assessment.status != 'consistent_offline':
        raise EvidenceBlocked('manual reconciliation required before close planning')
    output = []
    for leg, position in zip(legs, positions):
        quantity = _position(leg, position, now_ms=now_ms, max_age_ms=max_age_ms)
        if quantity == 0:
            raise EvidenceBlocked('position is already flat')
        instrument, _, _ = _expected(leg)
        output.append(CloseIntent(leg.venue, instrument, 'sell' if quantity > 0 else 'buy',
                                  format(quantity.copy_abs(), 'f')))
    return output[0], output[1]


@dataclass(frozen=True)
class JournalSummary:
    state: str
    venues: tuple[str, ...]
    acknowledged: tuple[bool, ...]
    may_open_new_position: bool = False


def inspect_journal() -> JournalSummary:
    """Read a bounded, sanitized journal summary under the existing pair lock.

    This is a local diagnostic only. It does not query exchanges, clear any
    intent, authorize a retry or expose stored request/credential values.
    """
    import json
    import os
    import stat
    from core import execution_mode
    from core.file_lock import lock_exclusive, unlock

    path = execution_mode._directory() / 'demo-futures-intent.json'
    if not path.parent.exists():
        return JournalSummary('no_journal', (), ())
    lock_fd = None
    locked = False
    try:
        lock_fd = os.open(path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        locked = lock_exclusive(lock_fd, blocking=False)
        if not locked:
            raise EvidenceBlocked('demo intent is in use')
        if path.is_symlink():
            raise EvidenceBlocked('journal symlink rejected')
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        except FileNotFoundError:
            return JournalSummary('no_journal', (), ())
        with os.fdopen(fd, 'rb') as file:
            info = os.fstat(file.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_size > 1_048_576 or
                    (os.name != 'nt' and (info.st_uid != os.geteuid() or info.st_mode & 0o077))):
                raise EvidenceBlocked('invalid journal permissions or size')
            raw = file.read(1_048_577)
            if len(raw) > 1_048_576:
                raise EvidenceBlocked('invalid journal size')
        data = json.loads(raw)
        states = {'prepared', 'leg_1_submitting', 'leg_1_acknowledged', 'leg_1_filled',
                  'leg_2_submitting', 'leg_2_acknowledged', 'leg_2_filled',
                  'reconciliation_required', 'position_reconciliation_required'}
        if (not isinstance(data, dict) or type(data.get('version')) is not int or data.get('version') != 1 or data.get('state') not in states or
                not isinstance(data.get('id'), str) or not re.fullmatch(r'[a-f0-9]{32}', data['id']) or
                not isinstance(data.get('legs'), list) or len(data['legs']) != 2):
            raise EvidenceBlocked('invalid journal')
        venues = []
        acknowledged = []
        for leg in data['legs']:
            if not isinstance(leg, dict) or leg.get('venue') not in ('bybit', 'okx') or not isinstance(leg.get('request'), dict):
                raise EvidenceBlocked('invalid journal leg')
            venue = leg['venue']
            client_id = leg['request'].get('orderLinkId' if venue == 'bybit' else 'clOrdId')
            if not isinstance(client_id, str) or not re.fullmatch(r'[A-Za-z0-9]{1,24}', client_id):
                raise EvidenceBlocked('invalid journal client id')
            exchange_id = leg.get('order_id')
            if exchange_id is not None and (not isinstance(exchange_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', exchange_id)):
                raise EvidenceBlocked('invalid journal order id')
            venues.append(venue)
            acknowledged.append(exchange_id is not None)
        if set(venues) != {'bybit', 'okx'}:
            raise EvidenceBlocked('invalid journal venues')
        return JournalSummary(data['state'], tuple(venues), tuple(acknowledged))
    except EvidenceBlocked:
        raise
    except Exception:
        raise EvidenceBlocked('journal unavailable or invalid') from None
    finally:
        if lock_fd is not None:
            if locked:
                unlock(lock_fd)
            os.close(lock_fd)
