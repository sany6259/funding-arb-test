#!/usr/bin/env python3
"""Offline age-store diagnostic using disposable, fake credentials only.

Run as the intended service account. The directory must already exist,
be empty, private, and created specifically for this diagnostic under /var/tmp
or /tmp. Prefer /var/tmp when checking persistence after a reboot.
"""
from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

MARKER = 'funding-arb-pr16-demo-probe-v1'
FIRST = 'dummy-key-before-update'
UPDATED = 'dummy-key-after-update'
SECRET = 'dummy-secret-not-a-real-key'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('create', 'verify', 'delete'))
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    directory = args.directory
    info = directory.lstat()
    if (directory.parent.resolve() not in (Path('/var/tmp'), Path('/tmp')) or not directory.name.startswith('funding-arb-pr16-probe-')
            or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise RuntimeError('Use a private, owner-only funding-arb-pr16-probe-* temporary directory')
    marker = directory / '.probe-marker'
    if args.phase == 'create':
        if any(directory.iterdir()):
            raise RuntimeError('The diagnostic directory must be empty')
        home = directory / 'home'
        home.mkdir(mode=0o700)
        keygen = shutil.which('age-keygen')
        if not keygen or not shutil.which('age'):
            raise RuntimeError('age and age-keygen must be installed')
        result = subprocess.run([keygen, '-o', str(home / 'demo.key')], capture_output=True, timeout=15)
        if result.returncode:
            raise RuntimeError('Failed to create diagnostic identity')
        os.chmod(home / 'demo.key', 0o600)
        marker.write_text(MARKER, encoding='utf-8')
    else:
        if marker.is_symlink() or not marker.is_file() or marker.read_text(encoding='utf-8') != MARKER:
            raise RuntimeError('Diagnostic marker missing')
        home = directory / 'home'
    # Only this child process changes HOME and the demo backend; the running
    # bot, service environment and existing credential store are untouched.
    os.environ['HOME'] = str(home)
    os.environ['FARB_DEMO_CREDENTIAL_STORE'] = 'age'
    os.environ['FARB_DEMO_AGE_IDENTITY'] = str(home / 'demo.key')
    from core import demo_credentials

    if args.phase == 'create':
        demo_credentials.save('bybit', 'demo', FIRST, SECRET, '')
        if demo_credentials.load('bybit', 'demo')['key'] != FIRST:
            raise RuntimeError('Initial read failed')
        demo_credentials.save('bybit', 'demo', UPDATED, SECRET, '')
        if demo_credentials.load('bybit', 'demo')['key'] != UPDATED:
            raise RuntimeError('Updated read failed')
        ciphertext = (home / '.funding-arb' / 'demo-credentials' / 'demo-bybit.age').read_bytes()
        if not ciphertext.startswith(b'age-encryption.org/v1\n') or any(
            text.encode() in ciphertext for text in (FIRST, UPDATED, SECRET)
        ):
            raise RuntimeError('Ciphertext validation failed')
        print('PASS: create, read, update and ciphertext checks; only dummy credentials used')
    elif args.phase == 'verify':
        if demo_credentials.load('bybit', 'demo') != {'key': UPDATED, 'secret': SECRET, 'passphrase': ''}:
            raise RuntimeError('Restored value mismatch')
        print('PASS: dummy record persisted across processes')
    else:
        demo_credentials.delete('bybit', 'demo')
        if demo_credentials.load('bybit', 'demo') is not None:
            raise RuntimeError('Deletion failed')
        print('PASS: dummy record deleted; remove the diagnostic directory separately')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('FAIL: diagnostic could not complete; check age availability and private-directory permissions', file=sys.stderr)
        raise SystemExit(1)
