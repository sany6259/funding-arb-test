"""Separated demo credentials in an OS keychain; never fall back to plaintext."""
from __future__ import annotations

import json

_SERVICE = 'funding-arb-demo-v1'
_VENUES = {'bybit', 'okx', 'bitget'}
# Do not accept chainer or third-party file-based backends: those may be plaintext.
_ALLOWED_BACKENDS = {'keyring.backends.SecretService', 'keyring.backends.macOS', 'keyring.backends.Windows'}


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


def save(venue: str, environment: str, key: str, secret: str, passphrase: str) -> None:
    name = _name(venue, environment)
    _keyring().set_password(_SERVICE, name, json.dumps({'key': key, 'secret': secret, 'passphrase': passphrase}))


def load(venue: str, environment: str) -> dict[str, str] | None:
    name = _name(venue, environment)
    value = _keyring().get_password(_SERVICE, name)
    if value is None:
        return None
    data = json.loads(value)
    if (not isinstance(data, dict) or set(data) != {'key', 'secret', 'passphrase'} or
            not all(isinstance(data[k], str) for k in data)):
        raise RuntimeError('Invalid saved credentials')
    return data


def delete(venue: str, environment: str) -> None:
    name = _name(venue, environment)
    ring = _keyring()
    if ring.get_password(_SERVICE, name) is not None:
        ring.delete_password(_SERVICE, name)
