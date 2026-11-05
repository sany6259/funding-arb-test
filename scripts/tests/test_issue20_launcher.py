"""The VPS launcher loads configuration without shell execution or logging."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.launch_backend import load_project_env


def test_root_dotenv_is_loaded_without_overwriting_environment_or_executing_shell(tmp_path, monkeypatch, capsys):
    sentinel = tmp_path / 'must-not-exist'
    dummy_secret = 'dummy-secret-not-a-key'
    (tmp_path / '.env').write_text(
        'FARB_DEMO_CREDENTIAL_STORE=age\n'
        'FARB_DEMO_AGE_IDENTITY="/private/demo.key"\n'
        f'FARB_TEST_DUMMY_SECRET={dummy_secret}\n'
        f'FARB_TEST_LITERAL=$(touch {sentinel})\n', encoding='utf-8')
    monkeypatch.setenv('FARB_DEMO_CREDENTIAL_STORE', 'explicit-existing-value')
    for name in ('FARB_DEMO_AGE_IDENTITY', 'FARB_TEST_DUMMY_SECRET', 'FARB_TEST_LITERAL'):
        monkeypatch.delenv(name, raising=False)
    load_project_env(tmp_path)
    assert os.environ['FARB_DEMO_CREDENTIAL_STORE'] == 'explicit-existing-value'
    assert os.environ['FARB_DEMO_AGE_IDENTITY'] == '/private/demo.key'
    assert os.environ['FARB_TEST_DUMMY_SECRET'] == dummy_secret
    assert os.environ['FARB_TEST_LITERAL'].startswith('$(touch ')
    assert not sentinel.exists()
    captured = capsys.readouterr()
    assert dummy_secret not in captured.out + captured.err
    for name in ('FARB_DEMO_AGE_IDENTITY', 'FARB_TEST_DUMMY_SECRET', 'FARB_TEST_LITERAL'):
        monkeypatch.delenv(name, raising=False)


def test_missing_dotenv_is_allowed(tmp_path, monkeypatch):
    monkeypatch.delenv('FARB_DEMO_CREDENTIAL_STORE', raising=False)
    load_project_env(tmp_path)
    assert 'FARB_DEMO_CREDENTIAL_STORE' not in os.environ
