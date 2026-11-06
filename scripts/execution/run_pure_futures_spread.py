#!/usr/bin/env python3
"""Pure Futures Spread runner — scan -> decide -> execute -> journal.

MVP runner, defaults to dry-run:
  python3 scripts/execution/run_pure_futures_spread.py \
    --config templates/config.pure_futures.spread.json --once --verbose

Live requires dry_run=false or FARB_LIVE=1 (legacy DCA_LIVE still honored). Recommend using scanner/watch + report to confirm spread persistence first.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if __name__ == '__main__':
    from launch_backend import load_project_env
    load_project_env(SCRIPTS_DIR.parent)

from cli.scan_pure_futures_spreads import scan_pure_futures_spreads  # noqa: E402
from core.strategy_config import (  # noqa: E402
    apply_strategy_to_pure_futures_cfg,
    load_strategy_config,
    min_edge_for_row_factory,
    strategy_edge_thresholds,
    strategy_fee_policy,
)
from execution.pure_futures_executor import (  # noqa: E402
    close_pure_futures_pair,
    load_pure_futures_positions,
    open_pure_futures_pair,
)
from execution.settle_mismatch_planner import (  # noqa: E402
    effective_trade_usd,
    filter_candidates_with_mismatch,
)

DATA_DIR = SCRIPTS_DIR / "data" / "pure-futures"
JOURNAL_PATH = DATA_DIR / "journal.jsonl"


def _load_json(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _dry_run(cfg: dict[str, Any]) -> bool:
    if os.environ.get("FARB_LIVE") == "1" or os.environ.get("DCA_LIVE") == "1":
        return False
    if os.environ.get("FARB_DRY_RUN") == "1" or os.environ.get("DCA_DRY_RUN") == "1":
        return True
    return bool(cfg.get("dry_run", True))


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _candidate_key(row: dict[str, Any]) -> str:
    return ":".join(
        [
            str(row.get("base", "")).upper(),
            str(row.get("direction", "")),
            str(row.get("long_venue", "")),
            str(row.get("short_venue", "")),
        ]
    )


def _position_key(pos: dict[str, Any]) -> str:
    return ":".join(
        [
            str(pos.get("base", "")).upper(),
            str(pos.get("direction", "forward")),
            str(pos.get("long_venue", "")),
            str(pos.get("short_venue", "")),
        ]
    )


def _open_positions() -> list[dict[str, Any]]:
    return [p for p in load_pure_futures_positions() if p.get("status") == "open"]


def _run_demo_once(cfg: dict[str, Any]) -> dict[str, Any]:
    """Apply the same explicit settings and checks as the dashboard runner."""
    from execution import demo_futures
    if cfg.get('dry_run') is not False or not isinstance(cfg.get('demoStrategy'), dict):
        raise demo_futures.DemoBlocked('Explicit demo strategy settings required')
    cycle = run_demo_strategy_once(cfg['demoStrategy'])
    return {'strategy': 'pure_futures_spread', 'mode': 'demo', 'cycle': cycle,
            'actions': [cycle] if cycle['action'] in ('open', 'close') else [],
            'open_positions': int(cycle['action'] == 'hold' or
                                  cycle.get('result', {}).get('state') == 'open_confirmed')}


def run_once(cfg: dict[str, Any], *, verbose: bool = False) -> dict[str, Any]:
    from core import execution_mode
    if execution_mode.current_mode() == 'demo':
        return _run_demo_once(cfg)
    cfg = apply_strategy_to_pure_futures_cfg(cfg)
    strat = load_strategy_config()
    pfa = cfg.get("pureFuturesArbitrage") or {}
    venues = [
        str(v).lower() for v in pfa.get("venues", ["binance", "bitget", "bybit", "okx"])
    ]
    min_spread = float(pfa.get("minSpreadPct", 0.05))
    min_edge = float(pfa.get("minNetEdgePct", 0.01))
    exit_edge = float(pfa.get("exitThresholdPct", 0.01))
    max_pairs = int(pfa.get("maxConcurrentPairs", 3))
    trade_usd = float(pfa.get("tradeUsdPerPair", 500.0))
    max_mark_spread = float(pfa.get("maxMarkSpreadPct", 1.0))
    allow_mismatch = bool(pfa.get("allowSettleMismatch", False))
    workers = int(pfa.get("workers", 4))
    dry_run = _dry_run(cfg)
    min_edge, min_edge_1h, min_edge_mismatch = strategy_edge_thresholds(strat)
    row_min_edge = min_edge_for_row_factory(min_edge, min_edge_1h, min_edge_mismatch)
    fee_policy = strategy_fee_policy(strat)

    # Use relaxed thresholds to ensure held positions are visible even if below entry threshold, for exit decisions.
    scan = scan_pure_futures_spreads(
        venues=venues,
        min_spread=0.0,
        min_edge=-999.0,
        max_mark_spread_pct=max_mark_spread,
        workers=workers,
        fee_policy=fee_policy,
    )
    all_rows = list(scan.get("forward", [])) + list(scan.get("reverse", []))
    row_by_key = {_candidate_key(r): r for r in all_rows}

    actions: list[dict[str, Any]] = []

    # 1) Exit first.
    for pos in _open_positions():
        key = _position_key(pos)
        row = row_by_key.get(key)
        edge = float(row.get("net_edge_pct", -999.0)) if row else -999.0
        should_close = row is None or edge <= exit_edge
        if should_close:
            res = close_pure_futures_pair(str(pos["id"]), dry_run=dry_run, config=cfg)
            actions.append(
                {
                    "action": "close",
                    "position_id": pos.get("id"),
                    "edge": edge,
                    "result": res.to_dict(),
                }
            )
            if verbose:
                print(f"close {pos.get('id')} edge={edge:.4f}% state={res.state}")

    # 2) Open new slots.
    active = _open_positions()
    active_keys = {_position_key(p) for p in active}
    slots = max(0, max_pairs - len(active))

    # Pre-filter by basic thresholds
    candidates = []
    for row in all_rows:
        if float(row.get("spread_pct", 0.0) or 0.0) < min_spread:
            continue
        if float(row.get("net_edge_pct", 0.0) or 0.0) < row_min_edge(row):
            continue
        key = _candidate_key(row)
        if key in active_keys:
            continue
        candidates.append(row)

    # Apply settle_mismatch planner
    candidates = filter_candidates_with_mismatch(
        candidates,
        allow_mismatch=allow_mismatch,
        max_cumulative_outflow_pct=0.5,
        min_adjusted_edge_pct=min_edge,
        min_edge_for_row=row_min_edge,
    )

    candidates.sort(
        key=lambda x: (
            -float(x.get("adjusted_net_edge_pct", 0.0) or x.get("net_edge_pct", 0.0))
        )
    )

    for row in candidates[:slots]:
        row_trade_usd = effective_trade_usd(trade_usd, row)
        res = open_pure_futures_pair(
            str(row["base"]),
            str(row["long_venue"]),
            str(row["short_venue"]),
            row_trade_usd,
            dry_run=dry_run,
            direction=str(row.get("direction", "forward")),
            max_mark_spread_pct=max_mark_spread,
            config=cfg,
            capital_buffer_pct=float(row.get("capital_buffer_pct", 0) or 0),
        )
        actions.append({"action": "open", "candidate": row, "result": res.to_dict()})
        if verbose:
            print(
                f"open {row['base']} long@{row['long_venue']} short@{row['short_venue']} "
                f"edge={row.get('adjusted_net_edge_pct', row.get('net_edge_pct', 0)):.4f}% state={res.state}"
            )

    summary = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "strategy": "pure_futures_spread",
        "dry_run": dry_run,
        "venues": venues,
        "scan_total": len(all_rows),
        "candidates_after_filter": len(candidates),
        "actions": actions,
        "open_positions": len(_open_positions()),
        "thresholds": {
            "minSpreadPct": min_spread,
            "minNetEdgePct": min_edge,
            "minEdge1h": min_edge_1h,
            "minEdgeMismatch": min_edge_mismatch,
            "exitThresholdPct": exit_edge,
            "allowSettleMismatch": allow_mismatch,
            "feePolicy": fee_policy,
        },
    }
    _append_jsonl(JOURNAL_PATH, summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Pure futures spread runner")
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument(
        "--watch",
        type=float,
        metavar="MINUTES",
        help="run continuously every N minutes",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cfg = _load_json(args.config)
    from core import execution_mode
    if execution_mode.current_mode() == 'demo':
        from core.file_lock import lock_exclusive, unlock
        from execution import demo_futures
        directory = execution_mode._directory()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(directory / 'demo-strategy.lock', os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        if not lock_exclusive(fd, blocking=False):
            os.close(fd)
            raise demo_futures.DemoBlocked('Another demo strategy is already running')
        token = demo_futures._STRATEGY_ACTIVE.set(True)
        try:
            return _run_cli(cfg, args)
        finally:
            demo_futures._STRATEGY_ACTIVE.reset(token)
            unlock(fd)
            os.close(fd)
    return _run_cli(cfg, args)


def _run_cli(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    if not args.once and not args.watch:
        args.once = True

    if args.once:
        out = run_once(cfg, verbose=args.verbose)
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        elif args.verbose:
            print(
                f"done actions={len(out['actions'])} open_positions={out['open_positions']}"
            )
        return 0

    interval = float(args.watch) * 60.0
    while True:
        t0 = time.time()
        out = run_once(cfg, verbose=args.verbose)
        if args.json:
            print(json.dumps(out, ensure_ascii=False))
        elapsed = time.time() - t0
        time.sleep(max(0.0, interval - elapsed))


def validate_demo_strategy(settings: dict[str, Any]) -> dict[str, str | int]:
    """Validate explicit demo strategy parameters; never infer trading limits."""
    from decimal import Decimal, InvalidOperation
    from execution.demo_futures import DemoBlocked
    required = {'trade_usd', 'min_spread_pct', 'min_net_pct', 'exit_spread_pct',
                'max_mark_spread_pct', 'max_leverage', 'max_fee_pct', 'max_slippage_pct',
                'max_funding_gap_sec', 'interval_sec'}
    if not isinstance(settings, dict) or set(settings) != required:
        raise DemoBlocked('Explicit demo strategy settings required')
    result: dict[str, str | int] = {}
    for key in required - {'max_funding_gap_sec', 'interval_sec'}:
        value = settings[key]
        if not isinstance(value, str) or len(value) > 32:
            raise DemoBlocked('Invalid strategy setting')
        try:
            number = Decimal(value)
        except InvalidOperation:
            raise DemoBlocked('Invalid strategy setting') from None
        upper = Decimal('1000000000') if key == 'trade_usd' else Decimal('1') if key == 'max_mark_spread_pct' else Decimal('100')
        if not number.is_finite() or number < 0 or number > upper:
            raise DemoBlocked('Invalid strategy setting')
        if key in ('trade_usd', 'max_leverage') and number <= 0:
            raise DemoBlocked('Invalid strategy setting')
        result[key] = format(number, 'f')
    for key, low, high in (('interval_sec', 30, 86400), ('max_funding_gap_sec', 0, 604800)):
        value = settings[key]
        if type(value) is not int or not low <= value <= high:
            raise DemoBlocked('Invalid strategy setting')
        result[key] = value
    return result


def load_demo_strategy_settings() -> dict[str, str | int] | None:
    """Load validated non-secret settings without enabling the strategy."""
    import stat
    from core import execution_mode
    path = execution_mode._directory() / 'demo-strategy-settings.json'
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return None
    try:
        with os.fdopen(fd, 'rb') as file:
            info = os.fstat(file.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_size > 4096 or
                    (os.name != 'nt' and (info.st_uid != os.geteuid() or info.st_mode & 0o077))):
                raise ValueError('Invalid settings file')
            return validate_demo_strategy(json.loads(file.read(4097)))
    except Exception:
        from execution.demo_futures import DemoBlocked
        raise DemoBlocked('Stored demo strategy settings are invalid') from None


def save_demo_strategy_settings(settings: dict[str, Any]) -> dict[str, str | int]:
    from core import execution_mode
    from execution import demo_futures
    checked = validate_demo_strategy(settings)
    directory = execution_mode._directory()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    demo_futures._write_journal(directory / 'demo-strategy-settings.json', checked)
    return checked


def run_demo_strategy_once(settings: dict[str, Any]) -> dict[str, Any]:
    """One bounded cycle using the existing scanner and journalled demo executor.

    Both venues and every candidate are checked again using authenticated demo
    funding, order book, instrument, fee, margin and account data before entry.
    """
    from decimal import Decimal
    from execution import demo_futures, demo_futures_data
    from execution.demo_futures_preflight import RiskLimits, evaluate
    settings = validate_demo_strategy(settings)
    demo_futures._permit(True)
    max_age = 30_000
    path = demo_futures._journal_path()
    if path.exists() or path.is_symlink():
        try:
            pair, record = demo_futures._read_open_journal_for_close(
                path, allowed_states=frozenset({'open_confirmed', 'closed_confirmed'}))
        except demo_futures.DemoBlocked:
            # A verified closed single can be archived by execute_pair; any
            # open or uncertain single must continue blocking automation.
            demo_futures._read_single_journal(path, states=frozenset({'closed_confirmed'}))
            record = {'state': 'closed_confirmed'}
        if record['state'] == 'open_confirmed':
            try:
                demo_futures_data.verify_step(pair, record['legs'], 2, max_age_ms=max_age, require_fills=False)
            except demo_futures_data.DemoDataBlocked:
                raise demo_futures.DemoBlocked('Open pair requires reconciliation') from None
            try:
                signal = demo_futures_data.pure_futures_signal(pair.base, max_age_ms=max_age)
            except demo_futures_data.DemoDataBlocked:
                return {'action': 'wait', 'base': pair.base, 'reason': 'demo_market_data_unavailable'}
            spread = (Decimal(signal['short_funding_rate']) - Decimal(signal['long_funding_rate'])) * 100
            if signal['long_venue'] != pair.long_venue:
                spread = -spread
            if spread <= Decimal(settings['exit_spread_pct']):
                result = demo_futures.close_pair(confirmed=True, close_confirmed=True,
                                                  risk_limits=demo_futures_data_limit(max_age))
                return {'action': 'close', 'base': pair.base, 'spread_pct': str(spread), 'result': result}
            return {'action': 'hold', 'base': pair.base, 'spread_pct': str(spread)}
    demo_futures._permit(True, opening=True)
    scan = scan_pure_futures_spreads(
        venues=['bybit', 'okx'], min_spread=0, min_edge=-999,
        max_mark_spread_pct=float(settings['max_mark_spread_pct']), fee_policy={'mode': 'vip_tier'},
        with_depth=False, cache_ttl_sec=0.0)
    if not isinstance(scan, dict) or set(scan.get('data_venues', [])) != {'bybit', 'okx'}:
        return {'action': 'wait', 'reason': 'demo_market_data_unavailable'}
    rows = scan.get('forward', []) + scan.get('reverse', [])
    if not isinstance(rows, list):
        raise demo_futures.DemoBlocked('Invalid demo scan')
    limits = RiskLimits(max_age, max_age, 1, 604_800_000,
                        int(settings['max_funding_gap_sec']) * 1000,
                        str(settings['trade_usd']), str(settings['max_leverage']),
                        str(settings['max_mark_spread_pct']),
                        str(Decimal(settings['max_fee_pct']) / 100),
                        str(Decimal(settings['max_slippage_pct']) / 100), '0',
                        str(Decimal(settings['min_net_pct']) / 100))
    for row in rows[:50]:
        if not isinstance(row, dict) or {row.get('long_venue'), row.get('short_venue')} != {'bybit', 'okx'}:
            continue
        try:
            pair = demo_futures_data.pair_for_notional(row['base'], row['long_venue'], row['short_venue'],
                                                       str(settings['trade_usd']), max_age_ms=max_age)
            long = demo_futures_data.collect(pair.long_venue, pair.base, 'buy', pair.base_quantity)
            short = demo_futures_data.collect(pair.short_venue, pair.base, 'sell', pair.base_quantity)
            spread = (Decimal(short.funding_rate) - Decimal(long.funding_rate)) * 100
            if spread < Decimal(settings['min_spread_pct']):
                continue
            plan = evaluate(pair.base, pair.base_quantity, long, short, limits, now_ms=int(time.time() * 1000))
            if (Decimal(plan.base_quantity) != Decimal(pair.base_quantity) or
                    Decimal(plan.okx_contracts) != Decimal(pair.okx_contracts)):
                continue
        except Exception:
            continue  # Invalid or unavailable candidate evidence cannot authorize an order.
        result = demo_futures.execute_pair(pair, confirmed=True, risk_limits=limits)
        return {'action': 'open', 'base': pair.base, 'spread_pct': str(spread),
                'net_rate': plan.net_funding_after_estimated_costs, 'result': result}
    return {'action': 'none', 'reason': 'no_valid_candidate'}


def demo_futures_data_limit(max_age_ms: int):
    from execution.demo_futures_preflight import TechnicalLimits
    return TechnicalLimits(max_age_ms)


if __name__ == "__main__":
    raise SystemExit(main())
