#!/usr/bin/env python3
"""Backtest the strategy using the same indicator/setup code as the live scanner.

  python backtest.py swing    [--hold 5]   # ~2 years of daily candles
  python backtest.py intraday              # last ~60 days of 15m candles (Yahoo's limit)
  python backtest.py both

Each callout buys an at-the-money option at the close of the candle that completed the setup, then:
  - takes profit when the option is up TAKE_PROFIT (default +3%, after spread costs), or
  - stops out when the stock crosses the Supertrend line from the entry candle, or
  - exits at the close after `hold` days (swing) / at the market close (day trade).
If the target and stop are both touched in the same candle, it assumes the stop hit first.

Option prices are ESTIMATES (Black-Scholes, implied vol approximated by realized vol, plus a
spread cost). There is no free historical options data to test against.
"""
import argparse
import math
import os
import warnings
from concurrent.futures import ProcessPoolExecutor

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

import config as C
import scanner as S

SLIPPAGE = 0.015         # 1.5% of the option price lost on each side (bid/ask spread on liquid names)
MIN_VOL = 0.20
OPTION_STOPS = [None, 0.05, 0.10, 0.15, 0.20, 0.30]   # option stop-loss levels to compare


def trade(direction, entry, stop, future, dte, vol, days_per_bar, opt_stop=None):
    """Simulate one option trade bar by bar. Returns (return, exit_reason, bars_held).
    opt_stop: also exit if the option is down this fraction (e.g. 0.10 = -10%)."""
    call = direction == "CALLS"
    vol = max(vol, MIN_VOL)
    value = lambda spot, bars: S.bs_price(spot, entry, max(dte - bars * days_per_bar, 0) / 365, vol, call)
    paid = value(entry, 0) * (1 + SLIPPAGE)
    if paid <= 0:
        return 0.0, "none", 0
    target_gross = paid * (1 + C.TAKE_PROFIT) / (1 - SLIPPAGE)
    net = lambda gross: gross * (1 - SLIPPAGE) / paid - 1
    for k, (_, bar) in enumerate(future.iterrows(), start=1):
        adverse = bar["Low"] if call else bar["High"]
        favorable = bar["High"] if call else bar["Low"]
        if (call and adverse <= stop) or (not call and adverse >= stop):
            fill = min(bar["Open"], stop) if call else max(bar["Open"], stop)
            return net(value(fill, k)), "stop", k
        if opt_stop is not None and net(value(adverse, k)) <= -opt_stop:
            # gapped through the stop at the open -> fill at the open, else at the stop
            return min(net(value(bar["Open"], k)), -opt_stop), "stop", k
        if value(favorable, k) >= target_gross:
            return C.TAKE_PROFIT, "target", k
    k = len(future)
    return net(value(float(future["Close"].iloc[-1]), k)), "time", k


def run_variants(direction, entry, stop, future, dte, vol, days_per_bar):
    out = {}
    for v in OPTION_STOPS:
        ret, reason, held = trade(direction, entry, stop, future, dte, vol, days_per_bar, v)
        tag = "none" if v is None else f"{v:.0%}"
        out[f"ret|{tag}"], out[f"reason|{tag}"], out[f"held|{tag}"] = ret, reason, held
    return out


def record(t, mode, direction, ts, entry, stop, variants, vix):
    return {"ticker": t, "mode": mode, "direction": direction, "time": ts, "entry": round(entry, 2),
            "stop": round(stop, 2), "stop_dist_pct": abs(entry / stop - 1) * 100, "vix": vix, **variants}


def swing_ticker(job):
    t, df, hold, vix = job
    ind = S.indicators(df)
    signal, _ = S.setups(ind)
    rv = np.log(df["Close"]).diff().rolling(20).std() * math.sqrt(252)
    out = []
    for i in np.flatnonzero(signal):
        if i < C.EMA_LEN + 30 or i >= len(df) - 1:
            continue
        direction = "CALLS" if signal[i] > 0 else "PUTS"
        entry, stop = float(ind["close"].iloc[i]), float(ind["st_line"].iloc[i])
        vol = float(rv.iloc[i]) if not math.isnan(rv.iloc[i]) else MIN_VOL
        variants = run_variants(direction, entry, stop, df.iloc[i + 1:i + 1 + hold], C.SWING_TARGET_DTE, vol, 7 / 5)
        d = df.index[i].date()
        out.append(record(t, "swing", direction, d.isoformat(), entry, stop, variants, vix.get(d)))
    return out


def intraday_ticker(job):
    t, df, vix = job
    df = df.copy()
    df.index = df.index.tz_convert(S.ET) if df.index.tz is not None else df.index.tz_localize(S.ET)
    ind = S.indicators(df)
    signal, _ = S.setups(ind)
    dates, times = df.index.date, df.index.strftime("%H:%M")
    logret = np.log(df["Close"]).diff()
    out = []
    for i in np.flatnonzero(signal):
        # warm-up, and only candles the live scanner evaluates (closing 9:45-15:45)
        if i < C.EMA_LEN + 30 or not ("09:30" <= times[i] <= "15:30"):
            continue
        future = df.iloc[i + 1:]
        future = future[future.index.date == dates[i]]
        if future.empty:
            continue
        direction = "CALLS" if signal[i] > 0 else "PUTS"
        entry, stop = float(ind["close"].iloc[i]), float(ind["st_line"].iloc[i])
        vol = float(logret.iloc[max(0, i - 130):i + 1].std() * math.sqrt(26 * 252))
        variants = run_variants(direction, entry, stop, future, C.INTRADAY_TARGET_DTE, vol, 1 / 26)
        prior = [d for d in vix if d < dates[i]]
        out.append(record(t, "intraday", direction, df.index[i].strftime("%Y-%m-%d %H:%M"), entry, stop,
                          variants, vix[max(prior)] if prior else None))
    return out


def stats(g, tag, days):
    ret, reason = g[f"ret|{tag}"], g[f"reason|{tag}"]
    losses = ret[ret <= 0]
    return pd.Series({
        "trades": len(g),
        "per day": len(g) / days,
        f"hit +{C.TAKE_PROFIT:.0%} %": (reason == "target").mean() * 100,
        "stopped %": (reason == "stop").mean() * 100,
        "timed out %": (reason == "time").mean() * 100,
        "avg loss when wrong %": losses.mean() * 100 if len(losses) else 0.0,
        "avg return %": ret.mean() * 100,
    })


def summarize(trades, title, days):
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 20)
    fmt = lambda df: df.round(1).astype({"trades": int})
    print(f"\n{'=' * 100}\n{title}\n{'=' * 100}")
    print(f"Avg distance from entry to Supertrend stop: {trades['stop_dist_pct'].mean():.1f}% of stock price")
    tags = ["none" if v is None else f"{v:.0%}" for v in OPTION_STOPS]
    print("\nBy option stop-loss (none = Supertrend stop only):")
    print(fmt(pd.DataFrame({f"opt stop {t}": stats(trades, t, days) for t in tags}).T))
    best = max(tags, key=lambda t: trades[f"ret|{t}"].mean())
    print(f"\nBest option stop: {best}. Breakdown at that level:")
    print("\nBy direction:")
    print(fmt(trades.groupby("direction").apply(lambda g: stats(g, best, days))))
    bins = pd.cut(trades["vix"], [0, 15, 20, 30, 100], labels=["VIX <15", "15-20", "20-30", "30+"])
    print("\nBy VIX level:")
    by_vix = trades.groupby(bins).apply(lambda g: stats(g, best, days) if len(g) else None)
    print(fmt(by_vix.dropna()))


def run(mode, hold, limit):
    universe = S.get_universe()
    workers = max(1, (os.cpu_count() or 2) - 1)
    vix_df = S.download(["^VIX"], period="2y", interval="1d")["^VIX"]
    vix = {d.date(): float(v) for d, v in vix_df["Close"].items()}
    if mode == "swing":
        tickers = [u["ticker"] for u in universe][:limit]
        print(f"Downloading 2y daily data for {len(tickers)} tickers...")
        data = S.download(tickers, period="2y", interval="1d")
        jobs = [(t, df, hold, vix) for t, df in data.items() if len(df) > 100]
        fn = swing_ticker
    else:
        tickers = [u["ticker"] for u in universe[:C.INTRADAY_MAX_TICKERS]][:limit]
        print(f"Downloading 60d of {C.INTRADAY_INTERVAL} data for {len(tickers)} tickers...")
        data = S.download(tickers, period="60d", interval=C.INTRADAY_INTERVAL)
        jobs = [(t, df, vix) for t, df in data.items() if len(df) > 100]
        fn = intraday_ticker
    print(f"Backtesting {len(jobs)} tickers on {workers} cores...")
    trades = []
    with ProcessPoolExecutor(workers) as ex:
        for res in ex.map(fn, jobs, chunksize=8):
            trades.extend(res)
    trades = pd.DataFrame(trades)
    out = S.DATA / f"backtest_{mode}.csv"
    trades.to_csv(out, index=False)
    if trades.empty:
        print(f"\n{mode}: no trades")
        return
    dates = pd.to_datetime(trades["time"].str[:10])
    days = max(1, np.busday_count(dates.min().date(), dates.max().date()) + 1)
    if mode == "swing":
        label = (f"SWING (daily candles): +{C.TAKE_PROFIT:.0%} target / Supertrend stop / max {hold} days, "
                 f"{C.SWING_TARGET_DTE} DTE ATM")
    else:
        label = (f"DAY TRADE (15m candles): +{C.TAKE_PROFIT:.0%} target / Supertrend stop / exit at close, "
                 f"{C.INTRADAY_TARGET_DTE} DTE ATM")
    summarize(trades, label, days)
    print(f"\nAll trades saved to {out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["swing", "intraday", "both"])
    ap.add_argument("--hold", type=int, default=5, help="swing: max trading days to hold")
    ap.add_argument("--limit", type=int, default=None, help="only test the N most liquid tickers")
    a = ap.parse_args()
    for m in (["swing", "intraday"] if a.mode == "both" else [a.mode]):
        run(m, a.hold, a.limit)
    print("\nNotes: uses today's universe (survivorship bias); option returns are model estimates "
          "(no earnings IV crush, real spreads vary). Past results don't guarantee future ones.")


if __name__ == "__main__":
    main()
