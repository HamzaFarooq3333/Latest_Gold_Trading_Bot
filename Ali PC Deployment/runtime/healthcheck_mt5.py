"""No-trade MT5 connectivity check for the dedicated EC2 Exness terminal."""

from __future__ import annotations

import argparse
from pathlib import Path

import MetaTrader5 as mt5


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=r"C:\onyxion\.env")
    parser.add_argument("--model", choices=("ASIM", "DEMO"), default="ASIM")
    args = parser.parse_args()
    env = load_env(Path(args.env))

    prefix = args.model.upper()
    login = int(env[f"{prefix}_MT5_LOGIN"])
    server = env[f"{prefix}_MT5_SERVER"]
    symbol = env.get(f"{prefix}_MT5_SYMBOL", "XAUUSDm")
    terminal = env.get(f"{prefix}_MT5_TERMINAL_PATH") or env.get("MT5_TERMINAL_PATH")
    password = env[f"{prefix}_MT5_PASSWORD"]
    portable = (env.get(f"{prefix}_MT5_PORTABLE", "1").lower() in ("1", "true", "yes", "on"))
    if terminal and not Path(terminal).is_file():
        print(f"FAIL terminal_missing path={terminal}")
        return 1

    kwargs = {
        "login": login,
        "password": password,
        "server": server,
        "timeout": 60000,
        "portable": portable,
    }
    ok = mt5.initialize(terminal, **kwargs) if terminal else mt5.initialize(**kwargs)
    if not ok:
        print(f"FAIL initialize error={mt5.last_error()}")
        return 2
    try:
        info = mt5.account_info()
        if info is None:
            print("FAIL account_info is None")
            return 3
        terminal_info = mt5.terminal_info()
        if terminal_info is None:
            print(f"FAIL terminal_info is None error={mt5.last_error()}")
            return 3
        actual_server = str(getattr(info, "server", "") or "")
        print(
            f"ACCOUNT login={info.login} server={actual_server} "
            f"trade_mode={info.trade_mode} "
            f"terminal_trade_allowed={getattr(terminal_info, 'trade_allowed', None)}"
        )
        if int(info.login) != login or actual_server.casefold() != server.casefold():
            print(f"FAIL expected login={login} server={server}")
            return 4
        if int(getattr(info, "trade_mode", 2)) == 2:
            print(f"FAIL {args.model} account is real; demo-only runtime refuses to trade")
            return 4
        if not mt5.symbol_select(symbol, True):
            print(f"FAIL symbol_select symbol={symbol} error={mt5.last_error()}")
            return 5
        symbol_info = mt5.symbol_info(symbol)
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 161)
        if symbol_info is None or rates is None or len(rates) < 161:
            print(f"FAIL symbol_or_bars symbol={symbol} bars={0 if rates is None else len(rates)}")
            return 6
        if getattr(symbol_info, "trade_mode", None) == getattr(
            mt5, "SYMBOL_TRADE_MODE_DISABLED", 0
        ):
            print(f"FAIL symbol trading disabled symbol={symbol}")
            return 7
        print(
            f"PASS symbol={symbol} bars={len(rates)} "
            f"trade_allowed={getattr(info, 'trade_allowed', None)} "
            f"terminal_trade_allowed={getattr(terminal_info, 'trade_allowed', None)} "
            f"m15_last={int(rates[-1]['time'])}"
        )
        return 0
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
