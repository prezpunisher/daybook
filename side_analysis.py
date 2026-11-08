#!/usr/bin/env python3
"""Side analysis, separate from the live Daybook process: it reads docs/status.json and never sends pushes
or changes the watchlist.

  python side_analysis.py            # every scored day
  python side_analysis.py 2026-11-06 # one day

For every scored watchlist pick, looks at the underlying stock's 15-minute chart with
EMA50, MACD 12/26/9 and CCI 20 (HLC3) at the last candle that closed before the push, and while the
trade was open, then compares winners and losers. Writes data/side_analysis.csv. The daily research job
(research.py) runs the same check and puts it on the dashboard's Research page.
"""
import json
import sys
import warnings
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

import research_config as R
import scanner as S
from strategy_15m import BAR, candles, ind15

CROSS_LOOKBACK = R.CROSS_LOOKBACK   # a MACD cross within the last 4 candles (1 hour) counts as "fresh"


def scored_days(only=None):
    runs = json.loads(S.STATUS_FILE.read_text()).get("runs", [])
    days = {}
    for r in runs:
        if r.get("kind") in ("review", "reminder") and r.get("scorecard"):
            days.setdefault(r["time"][:10], r["scorecard"])
    return {d: sc for d, sc in sorted(days.items()) if not only or d == only}


def ct_on(day, hm):
    h, m = map(int, hm.split(":"))
    return pd.Timestamp(datetime.combine(pd.Timestamp(day).date(), datetime.min.time()), tz=S.CT) + timedelta(hours=h, minutes=m)


def analyze(day, rows, x15):
    """x15: {ticker: ind15 frame in CT}."""
    out = []
    for r in rows:
        x = x15.get(r["ticker"])
        if x is None:
            continue
        push = ct_on(day, r["alert"])
        closed = x[x.index + BAR <= push]                       # candles finished before the push
        if len(closed) < R.EMA_LEN:
            continue
        a = closed.iloc[-1]
        call = r["direction"] == "CALLS"
        sgn = 1 if call else -1
        recent = closed["cross"].iloc[-CROSS_LOOKBACK:]
        fresh = bool((recent == sgn).any())
        against = bool((recent == -sgn).any())
        # the full 3-indicator 15m signal (close past EMA50 + MACD cross + CCI past 0, same candle)
        sig = x[(x.index.date == push.date()) & (x.index.time >= pd.Timestamp("08:30").time())]
        fire = sig[(np.sign(sig["close"] - sig["ema"]) == sgn) & (sig["cross"] == sgn) & (np.sign(sig["cci"]) == sgn)]
        fire_before = fire[fire.index + BAR <= push]
        fire_after = fire[fire.index + BAR > push]
        # while the trade was open: first 15m candle (closed after entry) that turned against it
        end = ct_on(day, r["hit3"] if r["result"] > 0 and r.get("hit3") else (r.get("hit20") or "14:50"))
        during = x[(x.index + BAR > push) & (x.index + BAR <= end + BAR)]
        turn = during[(during["cross"] == -sgn) | (np.sign(during["cci"]) == -sgn)]
        out.append({
            "date": day, "ticker": r["ticker"], "dir": "C" if call else "P", "option": r.get("label"),
            "won": r["result"] > 0, "result": r["result"], "pnl": r["pnl"], "best": r.get("best"),
            "min20": r.get("min20"), "alert": r["alert"],
            "ema_ok": bool(np.sign(a["close"] - a["ema"]) == sgn),
            "ema_dist_%": round((a["close"] / a["ema"] - 1) * 100 * sgn, 2),
            "macd_ok": bool(np.sign(a["macd"] - a["sig"]) == sgn),
            "fresh_cross": fresh, "cross_against_1h": against,
            "cci": round(float(a["cci"]), 0), "cci_ok": bool(np.sign(a["cci"]) == sgn),
            "cci_strong": bool(a["cci"] * sgn > 100),
            "all3_ok": bool(np.sign(a["close"] - a["ema"]) == sgn and np.sign(a["macd"] - a["sig"]) == sgn
                            and np.sign(a["cci"]) == sgn),
            "signal_before_push": fire_before.index[-1].strftime("%H:%M") if len(fire_before) else "",
            "signal_after_push": fire_after.index[0].strftime("%H:%M") if len(fire_after) else "",
            "turned_against": (turn.index[0] + BAR).strftime("%H:%M") if len(turn) else "",
        })
    return out


def report(df):
    pd.set_option("display.width", 220)
    pd.set_option("display.max_rows", 200)
    cols = ["date", "ticker", "dir", "won", "pnl", "min20", "ema_ok", "ema_dist_%", "macd_ok", "fresh_cross",
            "cross_against_1h", "cci", "all3_ok", "signal_before_push", "turned_against"]
    print(df[cols].to_string(index=False))
    print(f"\n{len(df)} trades · {df.won.sum()} won · {(~df.won).sum()} lost · ${df.pnl.sum():+,.0f}\n")
    print("15m reading at the push, and how trades did with / without it:")
    for f, name in [("ema_ok", "close on the trade side of EMA50"), ("macd_ok", "MACD on the trade side of signal"),
                    ("cci_ok", "CCI on the trade side of 0"), ("cci_strong", "CCI past ±100"),
                    ("all3_ok", "all three agree"), ("fresh_cross", "MACD crossed our way in the last hour"),
                    ("cross_against_1h", "MACD crossed against us in the last hour")]:
        y, n = df[df[f]], df[~df[f]]
        line = lambda g: f"{g.won.sum()}/{len(g)} won, ${g.pnl.sum():+,.0f}" if len(g) else "none"
        print(f"  {name:42s} yes: {line(y):22s} no: {line(n)}")
    lost = df[~df.won]
    if len(lost):
        print("\nLosers, 15m reading at the push:")
        for _, r in lost.iterrows():
            why = []
            if not r.ema_ok: why.append("price on the wrong side of EMA50")
            if not r.macd_ok: why.append("MACD against")
            if not r.cci_ok: why.append(f"CCI {r.cci:+.0f} against")
            if r.cross_against_1h: why.append("MACD crossed against in the last hour")
            print(f"  {r.date} {r.ticker} {r.option}: {'; '.join(why) or 'all three agreed, the move reversed'}"
                  f"{f' · −20% after {int(r.min20)}m' if pd.notna(r.min20) else ''}")


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    days = scored_days(only)
    tickers = sorted({r["ticker"] for sc in days.values() for r in sc})
    x15 = {t: ind15(df) for t, df in candles(tickers, "60d", "15m").items()}
    rows = [row for d, sc in days.items() for row in analyze(d, sc, x15)]
    df = pd.DataFrame(rows)
    df.to_csv(S.DATA / "side_analysis.csv", index=False)
    report(df)


if __name__ == "__main__":
    main()
