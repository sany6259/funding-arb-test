"""Local-only controls for the existing journalled demo futures pair executor."""
from __future__ import annotations

import asyncio
import json
import os
import re
import stat
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from core import execution_mode
from execution import demo_futures, demo_futures_data
from execution.demo_futures_preflight import TechnicalLimits
from server.routes.demo_connections import _local_request

router = APIRouter(tags=['demo-trading'])
_LIMITS = TechnicalLimits(30_000)


def _error(message: str, status: int = 400):
    return JSONResponse(status_code=status, content={'error': message})


def _journal_summary():
    demo_futures._permit(False, read_only=True)
    path = demo_futures._journal_path()
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as file:
        info = os.fstat(file.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_size > 1_048_576 or
                (os.name != 'nt' and (info.st_uid != os.geteuid() or info.st_mode & 0o077))):
            raise ValueError('Invalid journal')
        record = json.load(file)
    if (not isinstance(record, dict) or type(record.get('version')) is not int or record.get('version') != 1 or
            not isinstance(record.get('state'), str) or not re.fullmatch(r'[a-z_0-9]{1,80}', record['state']) or
            not isinstance(record.get('base'), str) or not re.fullmatch(r'[A-Z][A-Z0-9]{1,14}', record['base'])):
        raise ValueError('Invalid journal')
    remaining = record.get('remaining_qty')
    if remaining is not None:
        remaining = format(demo_futures._quantity(remaining), 'f')
    result = {'base': record['base'], 'state': record['state'],
              'kind': 'single' if record.get('kind') == 'single' else 'pair',
              'recovered_from_pair': record.get('recovered_from_pair') is True,
              'remaining_qty': remaining, 'legs': [], 'closing': [], 'pending_recovery': []}
    for group in ('legs', 'closing', 'pending_recovery'):
        legs = ([record['pending_recovery']] if record.get('pending_recovery') is not None else []) if group == 'pending_recovery' else record.get(group, [])
        if not isinstance(legs, list) or len(legs) > 2:
            raise ValueError('Invalid journal')
        for leg in legs:
            if not isinstance(leg, dict) or leg.get('venue') not in ('bybit', 'okx') or not isinstance(leg.get('request'), dict):
                raise ValueError('Invalid journal')
            venue = leg['venue']
            client_id = leg['request'].get('orderLinkId' if venue == 'bybit' else 'clOrdId')
            order_id = leg.get('order_id')
            if (not isinstance(client_id, str) or not re.fullmatch(r'[A-Za-z0-9]{1,24}', client_id) or
                    (order_id is not None and (not isinstance(order_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', order_id)))):
                raise ValueError('Invalid journal')
            result[group].append({'venue': venue, 'client_id': client_id, 'order_id': order_id,
                                  'confirmed': isinstance(leg.get('confirmation'), dict)})
    if execution_mode.current_mode() != 'demo':
        raise ValueError('Mode changed')
    return result


@router.get('/demo/trading/journal')
async def journal(request: Request):
    if not _local_request(request):
        return _error('Доступ только через локальный адрес.', 403)
    try:
        return {'result': await asyncio.to_thread(_journal_summary)}
    except Exception:
        return _error('Журнал недоступен или повреждён; требуется ручная проверка.', 409)


async def _payload(request: Request):
    if not _local_request(request):
        return None, _error('Доступ только через локальный адрес.', 403)
    if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
        return None, _error('Требуется JSON.')
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError()
        return data, None
    except Exception:
        return None, _error('Некорректный запрос.')


@router.post('/demo/trading/permission')
async def permission(request: Request):
    data, error = await _payload(request)
    if error:
        return error
    if set(data) != {'enabled', 'confirmed'} or type(data['enabled']) is not bool or data['confirmed'] is not True:
        return _error('Требуется явное подтверждение.')
    try:
        if data['enabled']:
            demo_futures._permit(False, read_only=True)
        execution_mode.set_demo_orders_enabled(data['enabled'], confirmed=True)
        return execution_mode.current_state()
    except Exception:
        return _error('Деморазрешение недоступно. Проверьте режим, dry-run и хранилище.', 409)


@router.post('/demo/trading/stop-openings')
async def stop_openings(request: Request):
    data, error = await _payload(request)
    if error:
        return error
    if set(data) != {'confirmed'} or data['confirmed'] is not True:
        return _error('Требуется явное подтверждение.')
    try:
        execution_mode.set_demo_openings_enabled(False)
        return execution_mode.current_state()
    except Exception:
        return _error('Состояние режима недоступно.', 409)


@router.post('/demo/trading/resume-openings')
async def resume_openings(request: Request):
    data, error = await _payload(request)
    if error:
        return error
    if set(data) != {'confirmed'} or data['confirmed'] is not True:
        return _error('Требуется явное подтверждение.')
    try:
        demo_futures._permit(False, read_only=True)
        execution_mode.set_demo_openings_enabled(True, confirmed=True)
        return execution_mode.current_state()
    except Exception:
        return _error('Открытия недоступны. Проверьте разрешение и режим.', 409)


def _open(base: str, long_venue: str, short_venue: str, amount: str):
    demo_futures._permit(True, opening=True)
    pair = demo_futures_data.pair_for_notional(base, long_venue, short_venue, amount,
                                                max_age_ms=_LIMITS.max_snapshot_age_ms)
    return demo_futures.execute_pair(pair, confirmed=True, risk_limits=_LIMITS)


@router.post('/demo/trading/open')
async def open_pair(request: Request):
    data, error = await _payload(request)
    if error:
        return error
    if (set(data) != {'base', 'long_venue', 'short_venue', 'amount_usd', 'confirmed'} or
            data['confirmed'] is not True or not isinstance(data['base'], str) or
            not re.fullmatch(r'[A-Z][A-Z0-9]{1,14}', data['base']) or
            not isinstance(data['long_venue'], str) or not isinstance(data['short_venue'], str) or
            {data['long_venue'], data['short_venue']} != {'bybit', 'okx'} or
            not isinstance(data['amount_usd'], str)):
        return _error('Проверьте инструмент, площадки и размер.')
    try:
        result = await asyncio.to_thread(_open, data['base'], data['long_venue'], data['short_venue'], data['amount_usd'])
        return {'result': result}
    except Exception:
        return _error('Открытие не выполнено. Проверьте разрешение, данные бирж и журнал.', 409)


@router.post('/demo/trading/close')
async def close_pair(request: Request):
    data, error = await _payload(request)
    if error:
        return error
    if set(data) != {'confirmed'} or data['confirmed'] is not True:
        return _error('Требуется явное подтверждение.')
    try:
        result = await asyncio.to_thread(_close_pair_or_recovery)
        return {'result': result}
    except Exception:
        return _error('Закрытие не выполнено. Проверьте состояние позиций и журнал.', 409)


def _close_pair_or_recovery():
    summary = _journal_summary()
    if summary and summary['kind'] == 'single' and summary['recovered_from_pair']:
        result = demo_futures.close_single(confirmed=True, risk_limits=_LIMITS)
        if result.get('state') == 'next_leg_pending':
            result = demo_futures.close_single(confirmed=True, risk_limits=_LIMITS)
        return result
    return demo_futures.close_pair(confirmed=True, close_confirmed=True, risk_limits=_LIMITS)


def _open_single(base: str, venue: str, side: str, amount: str):
    demo_futures._permit(True, opening=True)
    pair = demo_futures_data.single_for_notional(venue, base, side, amount, max_age_ms=_LIMITS.max_snapshot_age_ms)
    return demo_futures.execute_single(pair, venue, side, confirmed=True, risk_limits=_LIMITS)


@router.post('/demo/trading/single/open')
async def open_single(request: Request):
    data, error = await _payload(request)
    if error:
        return error
    if (set(data) != {'base', 'venue', 'side', 'amount_usd', 'confirmed'} or data['confirmed'] is not True or
            not isinstance(data['base'], str) or not re.fullmatch(r'[A-Z][A-Z0-9]{1,14}', data['base']) or
            data['venue'] not in ('bybit', 'okx') or data['side'] not in ('buy', 'sell') or
            not isinstance(data['amount_usd'], str)):
        return _error('Проверьте инструмент, площадку, сторону и размер.')
    try:
        result = await asyncio.to_thread(_open_single, data['base'], data['venue'], data['side'], data['amount_usd'])
        return {'result': result}
    except Exception:
        return _error('Открытие не выполнено. Проверьте разрешение, данные биржи и журнал.', 409)


@router.post('/demo/trading/single/close')
async def close_single(request: Request):
    data, error = await _payload(request)
    if error:
        return error
    if set(data) != {'confirmed'} or data['confirmed'] is not True:
        return _error('Требуется явное подтверждение.')
    try:
        result = await asyncio.to_thread(demo_futures.close_single, confirmed=True, risk_limits=_LIMITS)
        return {'result': result}
    except Exception:
        return _error('Закрытие не выполнено. Проверьте позицию и журнал.', 409)


@router.post('/demo/trading/cancel')
async def cancel(request: Request):
    data, error = await _payload(request)
    if error:
        return error
    if (set(data) != {'venue', 'client_id', 'confirmed'} or data['confirmed'] is not True or
            data['venue'] not in ('bybit', 'okx') or not isinstance(data['client_id'], str) or
            not re.fullmatch(r'[A-Za-z0-9]{1,24}', data['client_id'])):
        return _error('Некорректный идентификатор заявки.')
    try:
        result = await asyncio.to_thread(demo_futures.cancel_pending, data['venue'], data['client_id'], confirmed=True)
        return {'result': result}
    except Exception:
        return _error('Отмена не выполнена. Проверьте журнал и состояние заявки.', 409)


@router.post('/demo/trading/cancel-open')
async def cancel_open(request: Request):
    data, error = await _payload(request)
    if error:
        return error
    if (set(data) != {'venue', 'base', 'client_id', 'confirmed'} or data['confirmed'] is not True or
            data['venue'] not in ('bybit', 'okx') or not isinstance(data['base'], str) or
            not re.fullmatch(r'[A-Z][A-Z0-9]{1,14}', data['base']) or not isinstance(data['client_id'], str) or
            not re.fullmatch(r'[A-Za-z0-9]{1,24}', data['client_id'])):
        return _error('Некорректный идентификатор заявки.')
    try:
        return {'result': await asyncio.to_thread(demo_futures.cancel_open_order, data['venue'], data['base'],
                                                   data['client_id'], confirmed=True)}
    except Exception:
        return _error('Не удалось безопасно отменить открытую заявку.', 409)


@router.get('/demo/trading/cancel-intent')
async def cancel_intent(request: Request):
    if not _local_request(request):
        return _error('Доступ только через локальный адрес.', 403)
    try:
        path = execution_mode._directory() / 'demo-futures-cancel-intent.json'
        try: record = demo_futures._read_external_cancel(path)
        except FileNotFoundError: return {'result': None}
        client = 'orderLinkId' if record['venue'] == 'bybit' else 'clOrdId'
        return {'result': {'state': record['state'], 'venue': record['venue'], 'base': record['base'],
                           'client_id': record['fields'][client], 'order_id': record['order_id']}}
    except Exception:
        return _error('Журнал отмены недоступен; операции заблокированы.', 409)


def _reconcile():
    cancel_path = execution_mode._directory() / 'demo-futures-cancel-intent.json'
    if cancel_path.exists() or cancel_path.is_symlink():
        return demo_futures.reconcile_external_cancel(confirmed=True, max_age_ms=_LIMITS.max_snapshot_age_ms)
    summary = _journal_summary()
    if summary is None:
        raise ValueError('No journal')
    if summary['kind'] == 'single':
        return demo_futures.reconcile_single(confirmed=True, max_age_ms=_LIMITS.max_snapshot_age_ms)
    recovered = demo_futures.reconcile_pair_first_leg(confirmed=True, max_age_ms=_LIMITS.max_snapshot_age_ms)
    if recovered.get('reason') == 'not_first_leg':
        recovered = demo_futures.reconcile_pair_second_leg(confirmed=True, max_age_ms=_LIMITS.max_snapshot_age_ms)
        if recovered.get('reason') == 'not_second_leg':
            if summary['state'].startswith(('close_', 'cancel_')):
                recovered = demo_futures.reconcile_pair_closing(confirmed=True, max_age_ms=_LIMITS.max_snapshot_age_ms)
            else:
                return demo_futures.reconcile_pending(confirmed=True, risk_limits=_LIMITS)
    return recovered


@router.post('/demo/trading/reconcile')
async def reconcile(request: Request):
    data, error = await _payload(request)
    if error:
        return error
    if set(data) != {'confirmed'} or data['confirmed'] is not True:
        return _error('Требуется подтверждение сверки.')
    try:
        return {'result': await asyncio.to_thread(_reconcile)}
    except Exception:
        return _error('Не удалось достоверно сверить состояние. Повторы остаются заблокированы.', 409)


@router.post('/demo/trading/resolve-flat')
async def resolve_flat(request: Request):
    data, error = await _payload(request)
    if error:
        return error
    if set(data) != {'confirmed'} or data['confirmed'] is not True:
        return _error('Требуется подтверждение сверки.')
    try:
        return {'result': await asyncio.to_thread(demo_futures.resolve_pending_flat, confirmed=True,
                                                   max_age_ms=_LIMITS.max_snapshot_age_ms)}
    except Exception:
        return _error('Не удалось подтвердить отсутствие позиций и заявок.', 409)


def _positions(base: str):
    result = {}
    for venue in ('bybit', 'okx'):
        position = demo_futures_data.read(venue, base, 'position').payload
        orders = demo_futures_data.read(venue, base, 'orders').payload
        if venue == 'bybit':
            positions, pending = position['result']['list'], orders['result']['list']
            pkeys = ('symbol', 'side', 'size', 'positionIdx', 'leverage', 'unrealisedPnl', 'curRealisedPnl', 'cumRealisedPnl')
            okeys = ('symbol', 'orderId', 'orderLinkId', 'side', 'orderStatus', 'qty', 'cumExecQty')
        else:
            positions, pending = position['data'], orders['data']
            pkeys = ('instId', 'posSide', 'pos', 'mgnMode', 'lever', 'upl', 'realizedPnl', 'fee', 'fundingFee')
            okeys = ('instId', 'ordId', 'clOrdId', 'side', 'state', 'sz', 'accFillSz')
        if not isinstance(positions, list) or not isinstance(pending, list):
            raise ValueError('Invalid response')
        result[venue] = {'positions': [{k: row.get(k) for k in pkeys} for row in positions],
                         'orders': [{k: row.get(k) for k in okeys} for row in pending]}
    return result


@router.post('/demo/trading/positions')
async def positions(request: Request):
    data, error = await _payload(request)
    if error:
        return error
    if set(data) != {'base'} or not isinstance(data['base'], str) or not re.fullmatch(r'[A-Z][A-Z0-9]{1,14}', data['base']):
        return _error('Некорректный инструмент.')
    try:
        result = await asyncio.to_thread(_positions, data['base'])
        if execution_mode.current_mode() != 'demo':
            raise ValueError('Mode changed')
        return {'result': result}
    except Exception:
        return _error('Не удалось получить данные обеих демобирж.', 409)


def _history(base: str):
    result = {}
    for venue in ('bybit', 'okx'):
        order_rows = demo_futures_data.all_history(venue, base, 'history')
        pnl_rows = demo_futures_data.all_history(venue, base, 'closed_pnl')
        funding_rows = demo_futures_data.all_history(venue, base, 'funding_history')
        fills, _, _ = demo_futures_data._all_fills(venue, base)
        if venue == 'bybit':
            order_keys = ('symbol', 'orderId', 'orderLinkId', 'side', 'orderStatus', 'qty', 'cumExecQty', 'avgPrice', 'createdTime')
            pnl_keys = ('symbol', 'orderId', 'side', 'qty', 'closedPnl', 'avgEntryPrice', 'avgExitPrice', 'updatedTime')
            fill_keys = ('symbol', 'orderId', 'orderLinkId', 'execId', 'side', 'execQty', 'execPrice', 'execFee', 'execTime')
            funding_rows = [row for row in funding_rows if row.get('symbol') == base + 'USDT']
            funding_keys = ('id', 'symbol', 'currency', 'type', 'cashFlow', 'funding', 'fee', 'transactionTime')
        else:
            order_keys = ('instId', 'ordId', 'clOrdId', 'side', 'state', 'sz', 'accFillSz', 'avgPx', 'fee', 'cTime')
            pnl_keys = ('instId', 'posSide', 'realizedPnl', 'pnl', 'fee', 'fundingFee', 'openAvgPx', 'closeAvgPx', 'uTime')
            fill_keys = ('instId', 'ordId', 'clOrdId', 'tradeId', 'side', 'fillSz', 'fillPx', 'fee', 'ts')
            funding_keys = ('billId', 'instId', 'type', 'subType', 'ccy', 'pnl', 'fee', 'balChg', 'ts')
        if not isinstance(order_rows, list) or not isinstance(pnl_rows, list):
            raise ValueError('Invalid history')
        result[venue] = {'orders': [{k: row.get(k) for k in order_keys} for row in order_rows],
                         'closed_positions': [{k: row.get(k) for k in pnl_keys} for row in pnl_rows],
                         'fills': [{k: row.get(k) for k in fill_keys} for row in fills],
                         'funding': [{k: row.get(k) for k in funding_keys} for row in funding_rows]}
    return result


@router.post('/demo/trading/history')
async def history(request: Request):
    data, error = await _payload(request)
    if error:
        return error
    if set(data) != {'base'} or not isinstance(data['base'], str) or not re.fullmatch(r'[A-Z][A-Z0-9]{1,14}', data['base']):
        return _error('Некорректный инструмент.')
    try:
        result = await asyncio.to_thread(_history, data['base'])
        if execution_mode.current_mode() != 'demo':
            raise ValueError('Mode changed')
        return {'result': result}
    except Exception:
        return _error('История недоступна или неполна.', 409)


# One worker across all backend processes. Restart never re-enables trading or
# restarts the strategy; durable pair intents remain in the existing journal.
import threading
from core.file_lock import lock_exclusive, unlock
from execution.run_pure_futures_spread import (run_demo_strategy_once, validate_demo_strategy,
                                                save_demo_strategy_settings, load_demo_strategy_settings)

_auto_guard = threading.Lock()
_auto_stop = threading.Event()
_auto_thread = None
_auto_state = {'running': False, 'last': None, 'error': None}


def _log_auto_event(result: dict):
    action = result.get('action')
    if action not in ('started', 'stopped', 'stopped_error', 'open', 'close', 'hold', 'none', 'wait'):
        raise ValueError('Invalid strategy event')
    event = {'time': datetime.now(timezone.utc).isoformat(), 'action': action}
    base = result.get('base')
    if isinstance(base, str) and re.fullmatch(r'[A-Z][A-Z0-9]{1,14}', base):
        event['base'] = base
    outcome = result.get('result')
    if isinstance(outcome, dict) and isinstance(outcome.get('state'), str) and re.fullmatch(r'[a-z_0-9]{1,80}', outcome['state']):
        event['state'] = outcome['state']
    payload = (json.dumps(event, separators=(',', ':')) + '\n').encode()
    path = execution_mode._directory() / 'demo-strategy-events.jsonl'
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or
                (os.name != 'nt' and (info.st_uid != os.geteuid() or info.st_mode & 0o077))):
            raise ValueError('Unsafe event journal')
        if os.write(fd, payload) != len(payload):
            raise OSError('Incomplete event write')
        os.fsync(fd)
    finally:
        os.close(fd)


def _auto_loop(settings, fd):
    token = demo_futures._CANCELLED.set(_auto_stop.is_set)
    strategy_token = demo_futures._STRATEGY_ACTIVE.set(True)
    try:
        while not _auto_stop.is_set():
            try:
                result = run_demo_strategy_once(settings)
                _log_auto_event(result)
                with _auto_guard:
                    _auto_state['last'] = result
                    _auto_state['error'] = None
                if isinstance(result.get('result'), dict) and result['result'].get('state') in (
                        'reconciliation_required', 'close_reconciliation_required'):
                    with _auto_guard:
                        _auto_state['error'] = 'Требуется ручная сверка; стратегия остановлена.'
                    break
            except Exception:
                # Do not repeatedly attempt trades after unknown or inconsistent state.
                try: _log_auto_event({'action': 'stopped_error'})
                except Exception: pass
                with _auto_guard:
                    _auto_state['error'] = 'Стратегия остановлена: данные, разрешение или журнал недоступны.'
                break
            for _ in range(settings['interval_sec']):
                if _auto_stop.wait(1):
                    break
                try:
                    if not execution_mode.current_state()['openings_enabled']:
                        _auto_stop.set()
                        break
                except Exception:
                    _auto_stop.set()
                    break
    finally:
        try: _log_auto_event({'action': 'stopped'})
        except Exception: pass
        demo_futures._STRATEGY_ACTIVE.reset(strategy_token)
        demo_futures._CANCELLED.reset(token)
        unlock(fd)
        os.close(fd)
        with _auto_guard:
            _auto_state['running'] = False


@router.post('/demo/trading/strategy/start')
async def start_strategy(request: Request):
    global _auto_thread
    data, error = await _payload(request)
    if error:
        return error
    if set(data) != {'confirmed', 'settings'} or data['confirmed'] is not True:
        return _error('Требуется подтверждение и настройки стратегии.')
    try:
        settings = validate_demo_strategy(data['settings'])
        demo_futures._permit(True, opening=True)
        directory = execution_mode._directory()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(directory / 'demo-strategy.lock', os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        if not lock_exclusive(fd, blocking=False):
            os.close(fd)
            return _error('Демостратегия уже запущена в другом процессе.', 409)
        try:
            settings = save_demo_strategy_settings(settings)
            _log_auto_event({'action': 'started'})
        except Exception:
            unlock(fd)
            os.close(fd)
            raise
        with _auto_guard:
            if _auto_state['running']:
                unlock(fd)
                os.close(fd)
                return _error('Демостратегия уже запущена.', 409)
            _auto_stop.clear()
            _auto_state.update(running=True, last=None, error=None)
            _auto_thread = threading.Thread(target=_auto_loop, args=(settings, fd), daemon=True)
            try:
                _auto_thread.start()
            except Exception:
                _auto_state['running'] = False
                unlock(fd)
                os.close(fd)
                raise
        return {'running': True}
    except Exception:
        return _error('Проверьте настройки и разрешение демоторговли.', 409)


@router.post('/demo/trading/strategy/stop')
async def stop_strategy(request: Request):
    data, error = await _payload(request)
    if error:
        return error
    if set(data) != {'confirmed'} or data['confirmed'] is not True:
        return _error('Требуется подтверждение.')
    _auto_stop.set()
    try:
        # A concurrent cycle must not send another opening after this returns.
        execution_mode.set_demo_openings_enabled(False)
    except Exception:
        return _error('Состояние режима недоступно.', 409)
    with _auto_guard:
        return {'running': _auto_state['running'], 'stopping': True}


@router.get('/demo/trading/strategy')
async def strategy_status(request: Request):
    if not _local_request(request):
        return _error('Доступ только через локальный адрес.', 403)
    with _auto_guard:
        result = dict(_auto_state)
    if not result['running']:
        try:
            directory = execution_mode._directory()
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(directory / 'demo-strategy.lock', os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            try:
                acquired = lock_exclusive(fd, blocking=False)
                if acquired:
                    unlock(fd)
                else:
                    result['running'] = True
                    result['last'] = None
                    result['error'] = 'Стратегия запущена в другом процессе; подробности состояния недоступны.'
            finally:
                os.close(fd)
        except Exception:
            return _error('Состояние стратегии недоступно.', 503)
    return result


@router.get('/demo/trading/strategy/settings')
async def strategy_settings(request: Request):
    if not _local_request(request):
        return _error('Доступ только через локальный адрес.', 403)
    try:
        return {'settings': load_demo_strategy_settings()}
    except Exception:
        return _error('Сохранённые настройки стратегии недоступны.', 409)


@router.get('/demo/trading/strategy/events')
async def strategy_events(request: Request):
    if not _local_request(request):
        return _error('Доступ только через локальный адрес.', 403)
    try:
        path = execution_mode._directory() / 'demo-strategy-events.jsonl'
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        except FileNotFoundError:
            return {'events': []}
        with os.fdopen(fd, 'rb') as file:
            info = os.fstat(file.fileno())
            if (not stat.S_ISREG(info.st_mode) or
                    (os.name != 'nt' and (info.st_uid != os.geteuid() or info.st_mode & 0o077))):
                raise ValueError('Unsafe event journal')
            file.seek(max(0, info.st_size - 65536))
            lines = file.read(65536).splitlines()[-50:]
        events = []
        for line in lines:
            try:
                item = json.loads(line)
                if (not isinstance(item, dict) or set(item) - {'time', 'action', 'base', 'state'} or
                        not isinstance(item.get('time'), str) or not re.fullmatch(r'[0-9T:.,+\-]{1,40}', item['time']) or
                        item.get('action') not in ('started', 'stopped', 'stopped_error', 'open', 'close', 'hold', 'none', 'wait') or
                        ('base' in item and (not isinstance(item['base'], str) or not re.fullmatch(r'[A-Z][A-Z0-9]{1,14}', item['base']))) or
                        ('state' in item and (not isinstance(item['state'], str) or not re.fullmatch(r'[a-z_0-9]{1,80}', item['state'])))):
                    raise ValueError('Invalid event')
                events.append(item)
            except Exception:
                continue
        return {'events': events}
    except Exception:
        return _error('Журнал стратегии недоступен.', 409)


@router.post('/demo/trading/strategy/settings')
async def save_strategy_settings(request: Request):
    data, error = await _payload(request)
    if error:
        return error
    if set(data) != {'settings'}:
        return _error('Некорректные настройки.')
    try:
        if execution_mode.current_mode() != 'demo':
            raise ValueError('Demo mode required')
        return {'settings': save_demo_strategy_settings(data['settings'])}
    except Exception:
        return _error('Не удалось сохранить настройки стратегии.', 409)
