#!/usr/bin/env python3
"""Daily research job, separate from the live Daybook runs: it never sends pushes and never touches
docs/status.json. Writes docs/research.json for the dashboard's Research page.

  1. Watchlist 15m check: for each scored watchlist pick, the 15m EMA50 / MACD / CCI reading at the push.
  2. 15m EMA50 + MACD + CCI strategy in shadow: every signal, scored on its option's real 1-minute prices.
  3. That strategy's 60-day backtest on estimated option prices, rerun once a week.

Catches up on any day it hasn't processed, so a late or skipped run loses nothing. Also saves every traded option's
1-minute prices to research_data/minutes/ (Yahoo drops them once the option expires), so other exit rules can be
tested on past trades later; the -15% way-out comparison comes from these.

  python research.py
"""
import json
import warnings
from datetime import date, datetime, timedelta

warnings.filterwarnings("ignore")

import pandas as pd
import yfinance as yf

import research_config as R
import scanner as S
import side_analysis as SA
import strategy_15m as ST

OUT = S.ROOT / "docs" / "research.json"
OPTION_HISTORY_DAYS = 29          # Yahoo keeps about 30 days of 1-minute option prices
TARGETS = {"3": ("min3", 0.03), "5": ("min5", 0.05), "10": ("min10", 0.10)}
ALT_STOP = 0.15                   # compared next to the live -20% way out
MINUTES = S.ROOT / "research_data" / "minutes"


def exits(r):
    """What the trade would have made selling at +3%, +5% or +10% (still -20% way out, out by 2:50 CT), and at
    its best price of the day (hindsight). Uses the same 1-minute option trades as the +3% result."""
    if r.get("entry") is None or r.get("qty") is None:
        return None
    cost = r["entry"] * r["qty"] * 100
    out = {"cost": round(cost)}
    for k, (key, t) in TARGETS.items():
        hit, stop = r.get(key), r.get("min20")
        ret = t if hit is not None and (stop is None or hit < stop) else -S.C.STOP_LOSS if stop is not None else r.get("now")
        if ret is None:
            return None
        out[k] = {"ret": round(ret, 3), "pnl": round(ret * cost)}
    if r.get("best") is not None:
        out["best"] = {"ret": r["best"], "pnl": round(r["best"] * cost)}
    return out


def push_time(day, hm):
    """'8:47' / '1:15' (CT, no am/pm; the session is 8:30-3:00) -> timestamp."""
    h, m = map(int, hm.split(":"))
    return pd.Timestamp(f"{day} {h + 12 if h < 8 else h:02d}:{m:02d}", tz=S.CT)


def minute_bars(day, symbols):
    """{symbol: 1-minute OHLCV in CT} for one day, from research_data/minutes/<day>.json, fetching what's missing."""
    f = MINUTES / f"{day}.json"
    try:
        saved = json.loads(f.read_text())
    except Exception:
        saved = {}
    d = date.fromisoformat(day)
    changed = False
    for sym in symbols:
        if not sym or sym in saved:
            continue
        try:
            h = yf.Ticker(sym).history(start=d, end=d + timedelta(days=1), interval="1m")
        except Exception:
            continue
        if h.empty:
            continue
        h.index = h.index.tz_convert(S.CT)
        saved[sym] = {"t": list(h.index.strftime("%H:%M")), **{k: [round(float(v), 2) for v in h[c]] for k, c in
                      (("o", "Open"), ("h", "High"), ("l", "Low"), ("c", "Close"))}, "v": [int(v) for v in h["Volume"]]}
        changed = True
    if changed:
        MINUTES.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(saved, separators=(",", ":")))
    out = {}
    for sym, b in saved.items():
        idx = pd.DatetimeIndex([pd.Timestamp(f"{day} {t}", tz=S.CT) for t in b["t"]])
        out[sym] = pd.DataFrame({"Open": b["o"], "High": b["h"], "Low": b["l"], "Close": b["c"], "Volume": b["v"]}, index=idx)
    return out


def exits_from_minutes(bars, push, stop):
    """Same scoring as scanner.score_picks (buy the first trade after the push minute, out by 2:50 CT), with any way-out
    level: +3/+5/+10% if that prints before -stop, else -stop, else the 2:50 price; plus the best price."""
    h = bars[bars.index.strftime("%H:%M") <= R.EXIT_CT]
    after = h[h.index > push.floor("min")]
    if after.empty:
        return None
    entry = float(after["Open"].iloc[0])
    if entry <= 0:
        return None
    qty = max(int(S.C.MAX_TRADE_DOLLARS // (entry * 100)), 1)
    cost = entry * qty * 100
    first = lambda m: m.index[0] if len(m) else None
    t_out = first(after[after["Low"] <= entry * (1 - stop)])
    out = {"cost": round(cost)}
    for k, (_, t) in TARGETS.items():
        t_hit = first(after[after["High"] >= entry * (1 + t)])
        ret = t if t_hit is not None and (t_out is None or t_hit < t_out) else -stop if t_out is not None \
            else float(after["Close"].iloc[-1]) / entry - 1
        out[k] = {"ret": round(ret, 3), "pnl": round(ret * cost)}
    best = float(after["High"].max()) / entry - 1
    out["best"] = {"ret": round(best, 3), "pnl": round(best * cost)}
    return out


def add_alt_stop(rows, when):
    """rows of one kind; when(row) -> push time. Adds exits15 where the option's minute prices exist."""
    by_day = {}
    for r in rows:
        if "exits15" not in r and r.get("symbol"):
            by_day.setdefault(r["date"], []).append(r)
    for day, rs in by_day.items():
        bars = minute_bars(day, sorted({r["symbol"] for r in rs}))
        recent = date.fromisoformat(day) >= date.today() - timedelta(days=5)
        for r in rs:
            b = bars.get(r["symbol"])
            if b is not None:
                r["exits15"] = exits_from_minutes(b, when(r), ALT_STOP)
            elif not recent:                 # expired and gone from Yahoo; don't keep asking
                r["exits15"] = None


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
                       "tickers": ST.tickers(), "decision_days": R.DECISION_DAYS,
                       "cci_track_from": R.CCI_TRACK_FROM}
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
    for r in checked:                    # exit comparison from the day's scorecard
        k = next((x for x in scored.get(r["date"], []) if x["ticker"] == r["ticker"] and x.get("label") == r.get("option")), None)
        if k:
            r["exits"] = exits(k)
            r["symbol"] = k.get("symbol")
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
            for r in rows:
                r["exits"] = exits(r)
            shadow.extend(rows)
            res["shadow_days"] = sorted(logged | {d.isoformat()})
            logged = set(res["shadow_days"])
            shadow.sort(key=lambda r: (r["date"], r["time"].zfill(5), r["ticker"]), reverse=True)
            save(res)
            S.log.info("research: shadow %s, %d signals", d, len(rows))
        d += timedelta(days=1)

    # minute prices for every traded option, and the -15% way-out comparison
    add_alt_stop(checked, lambda r: push_time(r["date"], r["alert"]))
    add_alt_stop(shadow, lambda r: push_time(r["date"], r["time"]))
    save(res)

    # 3. weekly backtest
    bt = res.get("backtest") or {}
    if not bt.get("ran") or now - datetime.fromisoformat(bt["ran"]) >= timedelta(days=R.BACKTEST_EVERY_DAYS):
        res["backtest"] = ST.backtest()
        save(res)
    print(f"research: {len(checked)} watchlist picks checked, {len(shadow)} shadow signals over "
          f"{len(res['shadow_days'])} days, backtest {res['backtest'].get('ran')}")


if __name__ == "__main__":
    main()
