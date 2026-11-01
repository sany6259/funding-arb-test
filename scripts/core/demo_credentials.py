"""Isolated demo credentials: OS keyring or explicitly configured age encryption."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from core.file_lock import lock_exclusive, unlock

_SERVICE = 'funding-arb-demo-v1'
_VENUES = {'bybit', 'okx', 'bitget'}
_ALLOWED_BACKENDS = {'keyring.backends.SecretService', 'keyring.backends.macOS', 'keyring.backends.Windows'}
_age_thread_lock = threading.RLock()


def _keyring():
    import keyring
    backend = keyring.get_keyring()
    if type(backend).__module__ not in _ALLOWED_BACKENDS:
        raise RuntimeError('Secure system keyring unavailable')
    return keyring


def _name(venue: str, environment: str) -> str:
    if venue not in _VENUES or environment != 'demo':
        raise ValueError('Unsupported credential environment')
    return f'{environment}:{venue}'


def _backend() -> str:
    backend = os.environ.get('FARB_DEMO_CREDENTIAL_STORE', 'keyring')
    if backend not in ('keyring', 'age'):
        raise RuntimeError('Unsupported demo credential store')
    return backend


def _age_identity() -> Path:
    path = Path(os.environ.get('FARB_DEMO_AGE_IDENTITY', str(Path.home() / '.funding-arb' / 'demo-age.key')))
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or (os.name != 'nt' and (info.st_uid != os.geteuid() or info.st_mode & 0o077)):
        raise RuntimeError('Insecure age identity permissions')
    return path


def _age_program(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError('age tools unavailable')
    return executable


def _age_dir() -> Path:
    return Path.home() / '.funding-arb' / 'demo-credentials'


@contextmanager
def _age_locked():
    with _age_thread_lock:
        _age_identity()
        _age_program('age')
        _age_program('age-keygen')
        directory = _age_dir()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or (os.name != 'nt' and (info.st_uid != os.geteuid() or info.st_mode & 0o077)):
            raise RuntimeError('Insecure credential directory permissions')
        fd = os.open(directory / '.lock', os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            if not lock_exclusive(fd):
                raise RuntimeError('credential store lock unavailable')
            yield directory
        finally:
            unlock(fd)
            os.close(fd)


def _run(command: list[str], content: bytes = b'') -> bytes:
    try:
        result = subprocess.run(command, input=content, capture_output=True, check=True, timeout=15)
        return result.stdout
    except Exception:
        # Never expose tool stderr, command output or credentials to clients/logs.
        raise RuntimeError('Encrypted credential store unavailable') from None


def _age_save(venue: str, value: str) -> None:
    with _age_locked() as directory:
        recipient = _run([_age_program('age-keygen'), '-y', str(_age_identity())]).decode('ascii').strip()
        if not recipient.startswith('age1') or '\n' in recipient:
            raise RuntimeError('Invalid age identity')
        encrypted = _run([_age_program('age'), '-r', recipient], value.encode('utf-8'))
        if not encrypted.startswith(b'age-encryption.org/v1\n'):
            raise RuntimeError('Invalid age ciphertext')
        fd, temporary = tempfile.mkstemp(prefix='.demo-', dir=directory)
        try:
            with os.fdopen(fd, 'wb') as file:
                file.write(encrypted)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, directory / f'demo-{venue}.age')
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def _age_load(venue: str) -> str | None:
    with _age_locked() as directory:
        path = directory / f'demo-{venue}.age'
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536 or (os.name != 'nt' and (info.st_uid != os.geteuid() or info.st_mode & 0o077)):
            raise RuntimeError('Insecure encrypted credential file')
        ciphertext = path.read_bytes()
        return _run([_age_program('age'), '-d', '-i', str(_age_identity())], ciphertext).decode('utf-8')


def _age_delete(venue: str) -> None:
    with _age_locked() as directory:
        path = directory / f'demo-{venue}.age'
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError('Invalid encrypted credential file')
        path.unlink()


def save(venue: str, environment: str, key: str, secret: str, passphrase: str) -> None:
    name = _name(venue, environment)
    data = {'key': key, 'secret': secret, 'passphrase': passphrase}
    if _backend() == 'age':
        value = json.dumps({'version': 1, 'venue': venue, 'environment': environment, 'credentials': data})
        _age_save(venue, value)
    else:
        _keyring().set_password(_SERVICE, name, json.dumps(data))


def load(venue: str, environment: str) -> dict[str, str] | None:
    name = _name(venue, environment)
    is_age = _backend() == 'age'
    value = _age_load(venue) if is_age else _keyring().get_password(_SERVICE, name)
    if value is None:
        return None
    data = json.loads(value)
    if is_age:
        if (not isinstance(data, dict) or set(data) != {'version', 'venue', 'environment', 'credentials'} or
                data['version'] != 1 or data['venue'] != venue or data['environment'] != environment):
            raise RuntimeError('Credential environment mismatch')
        data = data['credentials']
    if (not isinstance(data, dict) or set(data) != {'key', 'secret', 'passphrase'} or
            not all(isinstance(data[k], str) for k in data)):
        raise RuntimeError('Invalid saved credentials')
    return data


def delete(venue: str, environment: str) -> None:
    name = _name(venue, environment)
    if _backend() == 'age':
        _age_delete(venue)
    else:
        ring = _keyring()
        if ring.get_password(_SERVICE, name) is not None:
            ring.delete_password(_SERVICE, name)
