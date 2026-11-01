#!/usr/bin/env python3
"""Read-only, explicit Bybit testnet credential check. Never submits orders."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from venues import bybit


def check() -> None:
    if os.environ.get("BYBIT_REQUIRE_TESTNET") != "1":
        raise RuntimeError("Set BYBIT_REQUIRE_TESTNET=1 in an isolated process")
    if os.environ.get("FARB_DRY_RUN") != "1":
        raise RuntimeError("Set FARB_DRY_RUN=1 for this read-only check")
    if "BYBIT_TESTNET_ALLOW_ORDERS" in os.environ:
        raise RuntimeError("Remove BYBIT_TESTNET_ALLOW_ORDERS for this read-only check")
    if any(name in os.environ for name in ("FARB_LIVE", "DCA_LIVE", "BYBIT_API_KEY", "BYBIT_SECRET_KEY")):
        raise RuntimeError("Remove production credentials and live flags from this process")
    if not os.environ.get("BYBIT_TESTNET_API_KEY") or not os.environ.get("BYBIT_TESTNET_SECRET_KEY"):
        raise RuntimeError("Testnet-only credentials are missing")
    if bybit._base_url() != bybit.TESTNET_BASE:
        raise RuntimeError("Testnet endpoint verification failed")
    bybit._api_call("GET", "/v5/account/info")


def main() -> int:
    try:
        check()
    except Exception:
        # Do not print the exception: remote messages can contain sensitive data.
        print("NO-GO: read-only testnet connection failed or prerequisites missing", file=sys.stderr)
        return 1
    print("PASS: Bybit testnet authenticated read-only request")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
