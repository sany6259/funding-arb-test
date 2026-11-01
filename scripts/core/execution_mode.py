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


def _read(directory: Path) -> str:
    path = directory / 'execution-mode.json'
    if not path.exists():
        return 'simulation'
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(state, dict) or set(state) != {'version', 'mode', 'orders_enabled'}:
            raise ValueError('invalid state')
        if state['version'] != 1 or state['mode'] not in _MODES or state['orders_enabled'] is not False:
            raise ValueError('invalid state')
        return state['mode']
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        raise RuntimeError('invalid execution mode state') from None


def current_mode() -> str:
    with _locked() as directory:
        return _read(directory)


def set_mode(mode: str) -> str:
    if mode not in _MODES:
        raise ValueError('unsupported execution mode')
    with _locked() as directory:
        _read(directory)  # Invalid state cannot be silently overwritten.
        fd, tmp = tempfile.mkstemp(prefix='.execution-mode-', dir=directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as file:
                json.dump({'version': 1, 'mode': mode, 'orders_enabled': False}, file)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, directory / 'execution-mode.json')
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return mode


def forbid_orders() -> None:
    """Check the shared state and fail closed for every trading request."""
    try:
        current_mode()
    except Exception:
        raise RuntimeError('Execution mode unavailable; orders disabled') from None
    raise RuntimeError('Orders disabled during demo account setup')
