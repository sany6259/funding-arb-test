"""Prevent external connections during CI tests; allow loopback test servers."""
import socket

_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
_original_getaddrinfo = socket.getaddrinfo


def _local(address):
    return not isinstance(address, tuple) or address[0] in ('127.0.0.1', '::1', 'localhost')


def _connect(self, address):
    if not _local(address):
        raise OSError('External network disabled for offline tests')
    return _original_connect(self, address)


def _connect_ex(self, address):
    if not _local(address):
        raise OSError('External network disabled for offline tests')
    return _original_connect_ex(self, address)


def _getaddrinfo(host, *args, **kwargs):
    if host not in ('localhost', '127.0.0.1', '::1', None):
        raise OSError('External DNS disabled for offline tests')
    return _original_getaddrinfo(host, *args, **kwargs)


socket.socket.connect = _connect
socket.socket.connect_ex = _connect_ex
socket.getaddrinfo = _getaddrinfo
