"""Local-only controls for the existing journalled demo futures pair executor."""
from __future__ import annotations

import asyncio
import json
import os
import re
import stat

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
    if (not isinstance(record, dict) or record.get('version') != 1 or
            not isinstance(record.get('state'), str) or not re.fullmatch(r'[a-z_0-9]{1,80}', record['state']) or
            not isinstance(record.get('base'), str) or not re.fullmatch(r'[A-Z][A-Z0-9]{1,14}', record['base'])):
        raise ValueError('Invalid journal')
    result = {'base': record['base'], 'state': record['state'], 'legs': [], 'closing': []}
    for group in ('legs', 'closing'):
        legs = record.get(group, [])
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
        result = await asyncio.to_thread(demo_futures.close_pair, confirmed=True, close_confirmed=True, risk_limits=_LIMITS)
        return {'result': result}
    except Exception:
        return _error('Закрытие не выполнено. Проверьте состояние позиций и журнал.', 409)


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


def _positions(base: str):
    result = {}
    for venue in ('bybit', 'okx'):
        position = demo_futures_data.read(venue, base, 'position').payload
        orders = demo_futures_data.read(venue, base, 'orders').payload
        if venue == 'bybit':
            positions, pending = position['result']['list'], orders['result']['list']
            pkeys = ('symbol', 'side', 'size', 'positionIdx', 'leverage', 'unrealisedPnl')
            okeys = ('symbol', 'orderId', 'orderLinkId', 'side', 'orderStatus', 'qty', 'cumExecQty')
        else:
            positions, pending = position['data'], orders['data']
            pkeys = ('instId', 'posSide', 'pos', 'mgnMode', 'lever', 'upl')
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
