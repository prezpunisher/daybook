#!/usr/bin/env python3
"""Research backtest for the "trend day" strategy (trend-aligned same-day weeklies).

  python backtest_trend.py [--limit 400] [--interval 5m]

Daily bias at the PRIOR close: close vs EMA45, MACD vs signal, Supertrend must all agree.
Then, same day, in the bias direction only, one entry per ticker per day from one of these triggers:
  orb   - a candle closes beyond the 30-minute opening range
  vwap  - price dips through VWAP against the trend, then a candle closes back on the trend side
Buys a weekly ATM option (expiring the coming Friday, next Friday on Thu/Fri), exits on the
option target, the stop, or at 15:50. Option prices are Black-Scholes estimates.
"""
import argparse
import math
import os
import warnings
from concurrent.futures import ProcessPoolExecutor
from datetime import timedelta

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

import config as C
import scanner as S

TARGETS = [0.03, 0.05, 0.08, 0.15]
OPT_STOPS = [0.10, 0.20, None]          # None = structure stop only
SLIPPAGE = 0.01                          # per side, filling near the mid on liquid weeklies
MIN_VOL = 0.25
LAST_ENTRY = "14:30"
EXIT_AT = "15:50"


weekly_dte = S.weekly_dte
daily_bias = S.daily_bias


def simulate(call, entry_px, vol, dte, bars, stop_px, entry_ts):
    """bars: remaining candles of the day (after entry). Returns {variant: return}."""
    T0 = entry_ts
    price = lambda spot, ts: S.bs_price(spot, entry_px, max(dte - (ts - T0).total_seconds() / 86400, 1e-4) / 365,
                                        vol, call)
    paid = price(entry_px, T0) * (1 + SLIPPAGE)
    if paid <= 0.05:
        return None
    net = lambda gross: gross * (1 - SLIPPAGE) / paid - 1
    out = {}
    for tgt in TARGETS:
        for os_ in OPT_STOPS:
            ret = None
            for ts, b in bars.iterrows():
                # walk the candle: green = open, low, high, close; red = open, high, low, close
                path = [b["Open"], b["Low"], b["High"]] if b["Close"] >= b["Open"] else [b["Open"], b["High"], b["Low"]]
                for px in path:
                    r = net(price(px, ts))
                    if os_ is not None and r <= -os_:
                        ret = -os_ if px != b["Open"] else r   # stop fills at its price unless the candle opens past it
                        break
                    if r >= tgt:
                        ret = tgt
                        break
                if ret is not None:
                    break
                # structure stop: candle CLOSES back through the stop level
                if (call and b["Close"] < stop_px) or (not call and b["Close"] > stop_px) \
                        or ts.strftime("%H:%M") >= EXIT_AT:
                    ret = net(price(b["Close"], ts))
                    break
            if ret is None:
                ts, b = bars.index[-1], bars.iloc[-1]
                ret = net(price(b["Close"], ts))
            out[f"{tgt:.0%}|{'none' if os_ is None else f'{os_:.0%}'}"] = ret
    # does the trigger predict direction at all? (stock only, no option model)
    sign = 1 if call else -1
    close = bars[bars.index.strftime("%H:%M") <= EXIT_AT]["Close"]
    out["stock_to_close"] = sign * (float(close.iloc[-1]) / entry_px - 1) * 100 if len(close) else 0.0
    out["stock_mfe"] = (float(bars["High"].max()) / entry_px - 1) * 100 if call else (1 - float(bars["Low"].min()) / entry_px) * 100
    out["stock_mae"] = (1 - float(bars["Low"].min()) / entry_px) * 100 if call else (float(bars["High"].max()) / entry_px - 1) * 100
    return out


def ticker_job(job):
    t, daily, intra, spy_bias = job
    prior = daily_bias(daily)
    df = intra.copy()
    df.index = df.index.tz_convert(S.ET) if df.index.tz is not None else df.index.tz_localize(S.ET)
    df = df.between_time("09:30", "15:59")
    out = []
    for d, day in df.groupby(df.index.date):
        if d not in prior.index or len(day) < 20:
            continue
        p = prior.loc[d]
        if p["bias"] == 0 or math.isnan(p["bias"]):
            continue
        call = p["bias"] > 0
        vol = max(float(p["rv"]) * 1.1 if not math.isnan(p["rv"]) else MIN_VOL, MIN_VOL)
        dte = weekly_dte(d)
        o = day.between_time("09:30", "09:59")
        orh, orl = o["High"].max(), o["Low"].min()
        tp = (day["High"] + day["Low"] + day["Close"]) / 3
        vwap = (tp * day["Volume"]).cumsum() / day["Volume"].cumsum().replace(0, np.nan)
        hm = day.index.strftime("%H:%M")
        gap = float(day["Open"].iloc[0]) / float(p["prev_close"]) - 1
        base = {"ticker": t, "date": d.isoformat(), "direction": "CALLS" if call else "PUTS",
                "gap": gap * (1 if call else -1), "spy_agrees": spy_bias.get(d, 0) == p["bias"],
                "above_open": None}

        found = {}
        dipped = False
        for i in range(len(day)):
            if hm[i] < "10:00" or hm[i] > LAST_ENTRY:
                if hm[i] < "10:00":
                    dipped = dipped or (day["Close"].iloc[i] < vwap.iloc[i] if call else day["Close"].iloc[i] > vwap.iloc[i])
                continue
            c, v = float(day["Close"].iloc[i]), float(vwap.iloc[i])
            with_open = c > day["Open"].iloc[0] if call else c < day["Open"].iloc[0]
            if "orb" not in found and ((call and c > orh) or (not call and c < orl)):
                found["orb"] = (i, (orh + orl) / 2, with_open)
            if "vwap" not in found:
                if dipped and ((call and c > v) or (not call and c < v)):
                    found["vwap"] = (i, min(day["Low"].iloc[max(0, i - 3):i + 1]) if call
                                     else max(day["High"].iloc[max(0, i - 3):i + 1]), with_open)
                dipped = dipped or (c < v if call else c > v)
        for trig, (i, stop, with_open) in found.items():
            bars = day.iloc[i + 1:]
            if bars.empty:
                continue
            res = simulate(call, float(day["Close"].iloc[i]), vol, dte, bars, stop, day.index[i])
            if res:
                out.append({**base, "trigger": trig, "time": hm[i], "above_open": bool(with_open), **res})
    return out


def report(df, days):
    pd.set_option("display.width", 220)
    print("\nStock-only check (in the trade's direction, %): does the trigger predict the move?")
    print(df.groupby("trigger")[["stock_to_close", "stock_mfe", "stock_mae"]].agg(["mean", "median"]).round(2))
    print("share of trades where the stock finished the day in our favor:")
    print((df.groupby("trigger")["stock_to_close"].apply(lambda x: (x > 0).mean() * 100)).round(1))
    variants = [c for c in df.columns if "|" in c]
    rows = []
    for (trig, filt), g in [((tr, f), df[(df.trigger == tr) & m]) for tr in df.trigger.unique()
                            for f, m in [("all", df.trigger == df.trigger),
                                         ("with open", df.above_open),
                                         ("spy agrees", df.spy_agrees),
                                         ("open+spy", df.above_open & df.spy_agrees)]]:
        if g.empty:
            continue
        for v in variants:
            r = g[v]
            rows.append({"trigger": trig, "filter": filt, "target|opt stop": v, "trades": len(g),
                         "per day": len(g) / days, "win %": (r > 0).mean() * 100,
                         "avg win %": r[r > 0].mean() * 100, "avg loss %": r[r <= 0].mean() * 100,
                         "avg %": r.mean() * 100, "$ per $1k trade": r.mean() * 1000})
    res = pd.DataFrame(rows).round(1)
    print("\nTop 25 combinations by average return:")
    print(res.sort_values("avg %", ascending=False).head(25).to_string(index=False))
    print("\nEvery trigger/filter at +5% target, -20% option stop:")
    print(res[res["target|opt stop"] == "5%|20%"].to_string(index=False))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=C.INTRADAY_MAX_TICKERS)
    ap.add_argument("--interval", default="5m")
    a = ap.parse_args()
    tickers = [u["ticker"] for u in S.get_universe()][:a.limit]
    print(f"Downloading daily + 60d {a.interval} data for {len(tickers)} tickers...")
    daily = S.download(tickers + ["SPY"], period="1y", interval="1d")
    intra = S.download(tickers, period="60d", interval=a.interval)
    spy = daily_bias(daily["SPY"])["bias"].to_dict()
    jobs = [(t, daily[t], intra[t], spy) for t in tickers if t in daily and t in intra and len(daily[t]) > 120]
    print(f"Backtesting {len(jobs)} tickers...")
    rows = []
    with ProcessPoolExecutor(max(1, (os.cpu_count() or 2) - 1)) as ex:
        for r in ex.map(ticker_job, jobs, chunksize=4):
            rows.extend(r)
    df = pd.DataFrame(rows)
    df.to_csv(S.DATA / "backtest_trend.csv", index=False)
    days = df["date"].nunique()
    print(f"{len(df)} trades over {days} trading days")
    report(df, days)


if __name__ == "__main__":
    main()
