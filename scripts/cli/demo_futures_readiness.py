#!/usr/bin/env python3
"""Offline readiness report; never sends an API request or places an order."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for directory in (ROOT, ROOT / 'scripts'):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from core.execution_mode import current_mode
from execution.demo_futures import DemoBlocked, FuturesPair, _preflight
from execution.demo_futures_recovery import EvidenceBlocked, inspect_journal


def main(*, show_journal: bool = False) -> int:
    if show_journal:
        try:
            journal = inspect_journal()
            print(f'Journal state: {journal.state}; venues: {", ".join(journal.venues) or "none"}; '
                  f'acknowledged: {", ".join("yes" if item else "no" for item in journal.acknowledged) or "none"}')
        except EvidenceBlocked:
            print('NOT READY: journal is busy, unavailable or invalid; manual review required')
            return 2
    try:
        mode = current_mode()
    except Exception:
        print('NOT READY: execution mode is unavailable; orders remain disabled')
        return 2
    print(f'Execution mode: {mode}')
    try:
        _preflight(FuturesPair('BTC', 'bybit', 'okx', '0.01', '0.1', '0.1'))
    except DemoBlocked:
        print('NOT READY: trusted risk policy, recovery and safe close are incomplete')
        return 2
    # Do not equate preflight alone with permission to trade.
    print('NOT READY: demo order execution requires a separate safety review')
    return 2


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal', action='store_true', help='show a sanitized local journal summary')
    raise SystemExit(main(show_journal=parser.parse_args().journal))
