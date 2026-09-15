"""One-shot: close all Onyxion magic positions on the connected MT5 terminal."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import MetaTrader5 as mt5  # noqa: E402
import bridge_trader as bt  # noqa: E402


def main() -> int:
    env = bt.load_env(bt.ENV_PATH)
    cfg = bt.build_cfg(env)
    bt.connect(cfg)
    symbol = bt.resolve_symbol(cfg["symbol"]) or cfg["symbol"]
    magic = int(cfg.get("magic") or os.environ.get("MT5_MAGIC_ASIM") or 126823)
    deviation = int(cfg.get("deviation") or 20)
    before = bt.positions_for_magic(symbol, magic)
    print(json.dumps({"before": len(before), "symbol": symbol, "magic": magic}))
    results = bt.close_all(symbol, magic, deviation)
    after = bt.positions_for_magic(symbol, magic)
    out = {
        "ok": len(after) == 0,
        "before": len(before),
        "after": len(after),
        "results": results,
        "remaining_tickets": [getattr(p, "ticket", None) for p in after],
    }
    print(json.dumps(out, default=str))
    try:
        mt5.shutdown()
    except Exception:
        pass
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
