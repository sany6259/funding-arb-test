#!/usr/bin/env python3
"""Manual CLI for pure futures spread pair open/close/list.

Examples:
  python3 scripts/cli/pure_futures_trade.py open BTC --long-venue okx --short-venue bybit --trade-usd 500 --dry-run
  python3 scripts/cli/pure_futures_trade.py list
  python3 scripts/cli/pure_futures_trade.py close pf-BTC-okx-bybit-... --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if __name__ == '__main__':
    from launch_backend import load_project_env
    load_project_env(ROOT.parent)

from execution.pure_futures_executor import (  # noqa: E402
    close_pure_futures_pair,
    load_pure_futures_positions,
    open_pure_futures_pair,
)


def _print_result(result) -> None:
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


def main() -> int:
    p = argparse.ArgumentParser(description="Pure futures spread manual trade CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    op = sub.add_parser("open", help="open long-perp + short-perp pair")
    op.add_argument("base", help="Base asset, e.g. BTC")
    op.add_argument("--long-venue", required=True, help="Venue for open_long")
    op.add_argument("--short-venue", required=True, help="Venue for open_short")
    op.add_argument("--trade-usd", type=float, required=True)
    op.add_argument("--quote", default="USDT")
    op.add_argument(
        "--direction",
        choices=("forward", "reverse"),
        default="forward",
        help="metadata from scanner classification",
    )
    op.add_argument("--max-mark-spread-pct", type=float, default=1.0)
    op.add_argument("--dry-run", action="store_true", help="simulate only")
    op.add_argument("--live", action="store_true", help="submit live orders")

    cl = sub.add_parser("close", help="close an open pure futures pair")
    cl.add_argument("position_id")
    cl.add_argument("--quote", default="USDT")
    cl.add_argument("--dry-run", action="store_true", help="simulate close")
    cl.add_argument("--live", action="store_true", help="submit live orders")

    ls = sub.add_parser("list", help="list pure futures positions")
    ls.add_argument("--all", action="store_true", help="include closed positions")
    ls.add_argument("--json", action="store_true")

    sub.add_parser("demo-mode", help="select demo mode and disable demo orders")
    enable = sub.add_parser("demo-enable", help="enable demo orders explicitly")
    enable.add_argument("--confirm", action="store_true", help="confirm demo order permission")
    sub.add_parser("demo-stop", help="stop new demo openings while keeping closes available")
    sub.add_parser("demo-disable", help="disable all demo orders")
    sub.add_parser("demo-state", help="show the shared execution mode and demo permissions")
    dop = sub.add_parser("demo-open", help="open a Bybit/OKX demo futures pair")
    dop.add_argument('base')
    dop.add_argument('--long-venue', required=True, choices=('bybit', 'okx'))
    dop.add_argument('--short-venue', required=True, choices=('bybit', 'okx'))
    dop.add_argument('--trade-usd', required=True)
    dop.add_argument('--confirm', action='store_true')
    dcl = sub.add_parser('demo-close', help='close the journalled demo pair reduce-only')
    dcl.add_argument('--confirm', action='store_true')
    dps = sub.add_parser('demo-positions', help='show demo positions and pending orders')
    dps.add_argument('base')
    dc = sub.add_parser('demo-cancel', help='cancel one uncertain journalled order')
    dc.add_argument('--venue', required=True, choices=('bybit', 'okx'))
    dc.add_argument('--client-id', required=True)
    dc.add_argument('--confirm', action='store_true')
    ds = sub.add_parser('demo-order', help='look up a demo order by client ID')
    ds.add_argument('base')
    ds.add_argument('--venue', required=True, choices=('bybit', 'okx'))
    ds.add_argument('--client-id', required=True)
    dr = sub.add_parser('demo-resolve', help='resolve a journal after terminal statuses and flat accounts')
    dr.add_argument('--confirm', action='store_true')

    args = p.parse_args()
    if args.cmd.startswith('demo-'):
        from core import execution_mode
        if args.cmd in ('demo-open', 'demo-close', 'demo-positions', 'demo-cancel', 'demo-order', 'demo-resolve'):
            from execution import demo_futures, demo_futures_data
            from execution.demo_futures_preflight import TechnicalLimits
            limits = TechnicalLimits(30_000)
            try:
                if args.cmd == 'demo-open':
                    if not args.confirm:
                        p.error('demo-open requires --confirm')
                    pair = demo_futures_data.pair_for_notional(args.base.upper(), args.long_venue,
                                                               args.short_venue, args.trade_usd,
                                                               max_age_ms=limits.max_snapshot_age_ms)
                    result = demo_futures.execute_pair(pair, confirmed=True, risk_limits=limits)
                elif args.cmd == 'demo-close':
                    if not args.confirm:
                        p.error('demo-close requires --confirm')
                    result = demo_futures.close_pair(confirmed=True, close_confirmed=True, risk_limits=limits)
                elif args.cmd == 'demo-cancel':
                    if not args.confirm:
                        p.error('demo-cancel requires --confirm')
                    result = demo_futures.cancel_pending(args.venue, args.client_id, confirmed=True)
                elif args.cmd == 'demo-resolve':
                    if not args.confirm:
                        p.error('demo-resolve requires --confirm')
                    result = demo_futures.resolve_pending_flat(confirmed=True, max_age_ms=limits.max_snapshot_age_ms)
                elif args.cmd == 'demo-order':
                    result = []
                    resources = ('lookup_current', 'lookup_history') if args.venue == 'bybit' else ('lookup_order',)
                    for resource in resources:
                        response = demo_futures_data.read(args.venue, args.base.upper(), resource,
                                                          client_id=args.client_id).payload
                        if args.venue == 'bybit':
                            rows = response['result']['list']
                            keys = ('symbol', 'orderId', 'orderLinkId', 'side', 'orderStatus', 'qty', 'cumExecQty')
                            cursor = response['result']['nextPageCursor']
                        else:
                            rows = response['data']
                            keys = ('instId', 'ordId', 'clOrdId', 'side', 'state', 'sz', 'accFillSz')
                            cursor = ''
                        result.append({'source': resource, 'orders': [{key: row.get(key) for key in keys} for row in rows],
                                       'more_history': bool(cursor)})
                else:
                    result = {}
                    for venue in ('bybit', 'okx'):
                        position = demo_futures_data.read(venue, args.base.upper(), 'position').payload
                        orders = demo_futures_data.read(venue, args.base.upper(), 'orders').payload
                        if venue == 'bybit':
                            positions, pending = position['result']['list'], orders['result']['list']
                            position_keys = ('symbol', 'side', 'size', 'positionIdx')
                            order_keys = ('symbol', 'orderId', 'orderLinkId', 'side', 'orderStatus', 'qty', 'cumExecQty')
                        else:
                            positions, pending = position['data'], orders['data']
                            position_keys = ('instId', 'posSide', 'pos', 'mgnMode')
                            order_keys = ('instId', 'ordId', 'clOrdId', 'side', 'state', 'sz', 'accFillSz')
                        result[venue] = {'positions': [{key: row.get(key) for key in position_keys} for row in positions],
                                         'orders': [{key: row.get(key) for key in order_keys} for row in pending]}
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 2 if isinstance(result, dict) and result.get('state') in (
                    'reconciliation_required', 'close_reconciliation_required', 'cancel_reconciliation_required') else 0
            except Exception:
                print('Demo operation blocked; review the journal and account state.', file=sys.stderr)
                return 2
        if args.cmd == 'demo-mode':
            execution_mode.set_mode('demo')
        elif args.cmd == 'demo-enable':
            if not args.confirm:
                p.error('demo-enable requires --confirm')
            execution_mode.set_demo_orders_enabled(True, confirmed=True)
        elif args.cmd == 'demo-stop':
            execution_mode.set_demo_openings_enabled(False)
        elif args.cmd == 'demo-disable':
            execution_mode.set_demo_orders_enabled(False)
        print(json.dumps(execution_mode.current_state(), ensure_ascii=False))
        return 0
    if args.cmd == "open":
        if args.live and args.dry_run:
            p.error("--live and --dry-run are mutually exclusive")
        dry_run = not args.live
        res = open_pure_futures_pair(
            args.base.upper(),
            args.long_venue.lower(),
            args.short_venue.lower(),
            args.trade_usd,
            dry_run=dry_run,
            quote=args.quote.upper(),
            direction=args.direction,
            max_mark_spread_pct=args.max_mark_spread_pct,
        )
        _print_result(res)
        return 0 if res.ok else 2

    if args.cmd == "close":
        if args.live and args.dry_run:
            p.error("--live and --dry-run are mutually exclusive")
        dry_run = None if not args.live and not args.dry_run else bool(args.dry_run)
        if args.live:
            dry_run = False
        res = close_pure_futures_pair(
            args.position_id, dry_run=dry_run, quote=args.quote.upper()
        )
        _print_result(res)
        return 0 if res.ok else 2

    if args.cmd == "list":
        rows = load_pure_futures_positions()
        if not args.all:
            rows = [r for r in rows if r.get("status") == "open"]
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            if not rows:
                print("No positions.")
            for r in rows:
                print(
                    f"{r.get('id')} {r.get('status')} {r.get('base')} "
                    f"long@{r.get('long_venue')} short@{r.get('short_venue')} "
                    f"qty={r.get('qty')} dry={r.get('dry_run')}"
                )
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
