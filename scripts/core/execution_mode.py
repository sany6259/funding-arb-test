"""Shared fail-closed execution mode. This stage has no order permission."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from core.file_lock import lock_exclusive, unlock

_MODES = ('simulation', 'demo')
_thread_lock = threading.RLock()


class LegacyAccessBlocked(RuntimeError):
    """A legacy network call was rejected before sending."""


def _directory() -> Path:
    return Path.home() / '.funding-arb'


@contextmanager
def _locked():
    with _thread_lock:
        directory = _directory()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / 'execution-mode.lock'
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if not lock_exclusive(fd):
                raise RuntimeError('mode lock unavailable')
            yield directory
        finally:
            unlock(fd)
            os.close(fd)


def _read_state(directory: Path) -> dict:
    path = directory / 'execution-mode.json'
    if not path.exists():
        return {'version': 1, 'mode': 'simulation', 'orders_enabled': False, 'openings_enabled': False}
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(state, dict) or set(state) not in (
                {'version', 'mode', 'orders_enabled'}, {'version', 'mode', 'orders_enabled', 'openings_enabled'}):
            raise ValueError('invalid state')
        if 'openings_enabled' not in state:
            if state['orders_enabled'] is not False:
                raise ValueError('invalid legacy state')
            state['openings_enabled'] = False
        if (type(state['version']) is not int or state['version'] != 1 or state['mode'] not in _MODES or
                type(state['orders_enabled']) is not bool or type(state['openings_enabled']) is not bool or
                (state['orders_enabled'] and state['mode'] != 'demo') or
                (state['openings_enabled'] and not state['orders_enabled'])):
            raise ValueError('invalid state')
        return state
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        raise RuntimeError('invalid execution mode state') from None


def _read(directory: Path) -> str:
    return _read_state(directory)['mode']


def _write_state(directory: Path, state: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix='.execution-mode-', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(state, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, directory / 'execution-mode.json')
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def current_mode() -> str:
    with _locked() as directory:
        return _read(directory)


def set_mode(mode: str) -> str:
    if mode not in _MODES:
        raise ValueError('unsupported execution mode')
    with _locked() as directory:
        _read(directory)  # Invalid state cannot be silently overwritten.
        _write_state(directory, {'version': 1, 'mode': mode, 'orders_enabled': False, 'openings_enabled': False})
    return mode


def set_demo_orders_enabled(enabled: bool, *, confirmed: bool = False) -> None:
    """Set shared demo permission; changing mode and server start revoke it."""
    if type(enabled) is not bool or (enabled and confirmed is not True):
        raise ValueError('Explicit demo order confirmation required')
    with _locked() as directory:
        state = _read_state(directory)
        if enabled and state['mode'] != 'demo':
            raise ValueError('Demo mode required')
        if enabled and not state['openings_enabled']:
            _require_strategy_stopped(directory)
        state['orders_enabled'] = enabled
        state['openings_enabled'] = enabled
        _write_state(directory, state)


def set_demo_openings_enabled(enabled: bool, *, confirmed: bool = False) -> None:
    if type(enabled) is not bool or (enabled and confirmed is not True):
        raise ValueError('Explicit confirmation required')
    with _locked() as directory:
        state = _read_state(directory)
        if enabled and (state['mode'] != 'demo' or not state['orders_enabled']):
            raise ValueError('Demo order permission required')
        if enabled and not state['openings_enabled']:
            _require_strategy_stopped(directory)
        state['openings_enabled'] = enabled
        _write_state(directory, state)


def _require_strategy_stopped(directory: Path) -> None:
    """Prevent a stopped worker in another process from resuming unexpectedly."""
    fd = os.open(directory / 'demo-strategy.lock', os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        if not lock_exclusive(fd, blocking=False):
            raise ValueError('Wait for demo strategy to stop')
        unlock(fd)
    finally:
        os.close(fd)


def current_state() -> dict:
    with _locked() as directory:
        return _read_state(directory)


def forbid_orders() -> None:
    """Check the shared state and fail closed for every trading request."""
    try:
        current_mode()
    except Exception:
        raise RuntimeError('Execution mode unavailable; orders disabled') from None
    raise RuntimeError('Orders disabled during demo account setup')


def require_simulation_api() -> None:
    """Legacy production/public routes are unavailable outside simulation."""
    try:
        if current_mode() == 'simulation':
            require_network_enabled()
            return
    except Exception:
        pass
    raise LegacyAccessBlocked('Legacy API access disabled in demo or unknown mode') from None


def require_network_enabled() -> None:
    if os.environ.get('FARB_DRY_RUN', '').strip().lower() not in ('', '0', 'false', 'no', 'off'):
        raise LegacyAccessBlocked('Network disabled by FARB_DRY_RUN')


def simulation_request(call, *args, **kwargs):
    """Hold the cross-process mode lock until a legacy request is sent.

    A switch to demo waits for any already-started request, so no new
    legacy request can cross the transition.
    """
    with _locked() as directory:
        try:
            mode = _read(directory)
        except Exception:
            raise LegacyAccessBlocked('Legacy API access disabled in demo or unknown mode') from None
        if mode != 'simulation':
            raise LegacyAccessBlocked('Legacy API access disabled in demo or unknown mode')
        require_network_enabled()
        return call(*args, **kwargs)
