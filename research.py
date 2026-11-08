#!/usr/bin/env python3
"""Daily research job, separate from the live Daybook runs: it never sends pushes and never touches
docs/status.json. Writes docs/research.json for the dashboard's Research page.

  1. Watchlist 15m check: for each scored watchlist pick, the 15m EMA50 / MACD / CCI reading at the push.
  2. 15m EMA50 + MACD + CCI strategy in shadow: every signal, scored on its option's real 1-minute prices.
  3. That strategy's 60-day backtest on estimated option prices, rerun once a week.

Catches up on any day it hasn't processed, so a late or skipped run loses nothing.

  python research.py
"""
import json
import warnings
from datetime import date, datetime, timedelta

warnings.filterwarnings("ignore")

import research_config as R
import scanner as S
import side_analysis as SA
import strategy_15m as ST

OUT = S.ROOT / "docs" / "research.json"
OPTION_HISTORY_DAYS = 29          # Yahoo keeps about 30 days of 1-minute option prices


def load():
    try:
        return json.loads(OUT.read_text())
    except Exception:
        return {}


def save(res):
    res["updated"] = datetime.now(S.CT).isoformat(timespec="minutes")
    OUT.write_text(json.dumps(res, indent=1, default=str))


def last_complete_day(now):
    """Today once its 2:50 CT exit has passed and the candles are in, else the previous day."""
    return now.date() if now.strftime("%H:%M") >= "15:05" else now.date() - timedelta(days=1)


def main():
    if not R.ENABLED:
        print("research disabled (research_config.ENABLED)")
        return
    now = datetime.now(S.CT)
    res = load()
    res["settings"] = {"ema": R.EMA_LEN, "macd": [R.MACD_FAST, R.MACD_SLOW, R.MACD_SIGNAL], "cci": R.CCI_LEN,
                       "first": R.FIRST_SIGNAL_CT, "last": R.LAST_SIGNAL_CT, "exit": R.EXIT_CT,
                       "take_profit": S.C.TAKE_PROFIT, "stop_loss": S.C.STOP_LOSS, "max_trade": S.C.MAX_TRADE_DOLLARS,
                       "tickers": ST.tickers(), "decision_days": R.DECISION_DAYS}
    scored = SA.scored_days()
    names = sorted(set(ST.tickers()) | {r["ticker"] for sc in scored.values() for r in sc})
    x15 = {t: ST.ind15(df) for t, df in ST.candles(names, "60d", "15m").items()}
    open_days = set(x15["SPY"].index.date) if "SPY" in x15 else set()

    # 1. watchlist 15m check
    checked = res.setdefault("watchlist_check", [])
    done = {r["date"] for r in checked}
    for d, sc in scored.items():
        if d not in done:
            rows = SA.analyze(d, sc, x15)
            checked.extend(rows)
            S.log.info("research: watchlist check %s, %d picks", d, len(rows))
    checked.sort(key=lambda r: (r["date"], r["alert"], r["ticker"]), reverse=True)
    save(res)

    # 2. shadow strategy, one trading day at a time
    daily = ST.candles(ST.tickers(), "1y", "1d")
    shadow = res.setdefault("shadow", [])
    logged = set(res.setdefault("shadow_days", []))
    start = max(date.fromisoformat(R.SHADOW_START), now.date() - timedelta(days=OPTION_HISTORY_DAYS))
    d, end = start, last_complete_day(now)
    while d <= end:
        if d.isoformat() not in logged and d in open_days:
            rows = ST.shadow_day(d, x15, daily)
            shadow.extend(rows)
            res["shadow_days"] = sorted(logged | {d.isoformat()})
            logged = set(res["shadow_days"])
            shadow.sort(key=lambda r: (r["date"], r["time"].zfill(5), r["ticker"]), reverse=True)
            save(res)
            S.log.info("research: shadow %s, %d signals", d, len(rows))
        d += timedelta(days=1)

    # 3. weekly backtest
    bt = res.get("backtest") or {}
    if not bt.get("ran") or now - datetime.fromisoformat(bt["ran"]) >= timedelta(days=R.BACKTEST_EVERY_DAYS):
        res["backtest"] = ST.backtest()
        save(res)
    print(f"research: {len(checked)} watchlist picks checked, {len(shadow)} shadow signals over "
          f"{len(res['shadow_days'])} days, backtest {res['backtest'].get('ran')}")


if __name__ == "__main__":
    main()
