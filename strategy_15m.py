#!/usr/bin/env python3
"""15-minute EMA50 + MACD 12/26/9 + CCI 20 strategy, run in shadow: signals are logged and scored on real
1-minute option prices, never pushed. Separate from the live watchlist so its results can be measured alone.

  CALL: a completed 15m candle closes above EMA50, MACD crosses above its signal on that candle, CCI > 0
  PUT:  a completed 15m candle closes below EMA50, MACD crosses below its signal on that candle, CCI < 0
CCI past +100 / -100 is recorded as "strong" but not required. Signals use the stock's candles, not the option's.

  python strategy_15m.py signals 2026-11-06   # print one day's signals
  python strategy_15m.py backtest             # 60-day backtest on estimated option prices
"""
import math
import sys
import warnings
from datetime import date, datetime, timedelta

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import yfinance as yf

import config as C
import research_config as R
import scanner as S

BAR = timedelta(minutes=15)


def ind15(df):
    """EMA, MACD and CCI (HLC3) with TradingView's math. df: OHLC candles."""
    c = df["Close"]
    x = pd.DataFrame(index=df.index)
    x["close"] = c
    x["ema"] = c.ewm(span=R.EMA_LEN, adjust=False).mean()
    macd = c.ewm(span=R.MACD_FAST, adjust=False).mean() - c.ewm(span=R.MACD_SLOW, adjust=False).mean()
    x["macd"], x["sig"] = macd, macd.ewm(span=R.MACD_SIGNAL, adjust=False).mean()
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    md = tp.rolling(R.CCI_LEN).apply(lambda v: np.abs(v - v.mean()).mean(), raw=True)
    x["cci"] = (tp - tp.rolling(R.CCI_LEN).mean()) / (0.015 * md)
    above = (x["macd"] > x["sig"]).astype(int)
    x["cross"] = above.diff().fillna(0)             # +1 crossed up on this candle, -1 crossed down
    return x


def tickers():
    return [t for t in R.TICKERS if t not in C.EXCLUDE]


def candles(ts, period, interval):
    """{ticker: OHLC in CT}, regular hours only."""
    out = {}
    for t in ts:
        try:
            df = yf.Ticker(t).history(period=period, interval=interval).dropna()
        except Exception as e:
            S.log.warning("research %s %s: %s", t, interval, e)
            continue
        if df.empty:
            continue
        if interval != "1d":                       # daily candles keep their exchange dates
            df.index = df.index.tz_convert(S.CT)
        out[t] = df
    return out


def signals(x, day, now=None):
    """Signals on candles of `day` that closed between FIRST_SIGNAL_CT and LAST_SIGNAL_CT (and before `now`).
    One row per candle at most, so a candle can't signal twice."""
    end = x.index + BAR
    hm = end.strftime("%H:%M")
    m = (x.index.date == day) & (hm >= R.FIRST_SIGNAL_CT) & (hm <= R.LAST_SIGNAL_CT) & x["ema"].notna() & x["cci"].notna()
    if now is not None:
        m &= end <= now
    rows = []
    for ts, r in x[m].iterrows():
        side = np.sign(r["close"] - r["ema"])
        if side != 0 and r["cross"] == side and np.sign(r["cci"]) == side:
            call = side > 0
            rows.append({"ticker": None, "direction": "CALLS" if call else "PUTS", "at": ts + BAR,
                         "time": (ts + BAR).strftime("%-I:%M"), "close": round(float(r["close"]), 2),
                         "ema": round(float(r["ema"]), 2), "macd": round(float(r["macd"]), 3),
                         "macd_signal": round(float(r["sig"]), 3), "cci": round(float(r["cci"]), 0),
                         "cci_strong": bool(r["cci"] > R.CCI_UPPER if call else r["cci"] < R.CCI_LOWER)})
    return rows


def occ(t, exp, call, strike):
    return f"{t}{exp:%y%m%d}{'C' if call else 'P'}{int(round(strike * 1000)):08d}"


def pick_contract(t, call, day, price):
    """This week's ATM weekly (next week's on Thu/Fri), like the watchlist. Returns (symbol, day volume) or
    (None, 0). Uses the listed strikes while the expiry is live, otherwise tries the usual strike steps."""
    exp = day + timedelta(days=S.weekly_dte(day))
    strikes = []
    if exp >= date.today():
        try:
            ch = yf.Ticker(t).option_chain(exp.isoformat())
            strikes = list((ch.calls if call else ch.puts)["strike"])
        except Exception:
            strikes = []
    if not strikes:
        strikes = {round(price / s) * s for s in (0.5, 1, 2.5, 5, 10)}
    for k in sorted(strikes, key=lambda k: abs(k - price))[:4]:
        sym = occ(t, exp, call, k)
        try:
            h = yf.Ticker(sym).history(start=day, end=day + timedelta(days=1), interval="1m")
        except Exception:
            continue
        if not h.empty:
            return sym, int(h["Volume"].sum())
    return None, 0


def shadow_day(day, x15, daily):
    """Every signal on `day`, scored on its option's real 1-minute prices from the signal candle's close."""
    out = []
    for t in tickers():
        if t not in x15:
            continue
        bias = None
        if t in daily:
            prior = S.daily_bias(daily[t])
            bias = prior["bias"].get(day)
        for s in signals(x15[t], day, now=pd.Timestamp.now(tz=S.CT)):
            call = s["direction"] == "CALLS"
            sym, vol = pick_contract(t, call, day, s["close"])
            row = {**s, "ticker": t, "date": day.isoformat(), "symbol": sym, "day_option_volume": vol,
                   "thin": vol < R.MIN_DAY_OPTION_VOLUME,
                   "daily_trend_agrees": None if bias is None or (isinstance(bias, float) and math.isnan(bias))
                   else bool(bias == (1 if call else -1))}
            row.pop("at")
            if sym:
                sc = S.score_picks([{"ticker": t, "direction": s["direction"], "symbol": sym}], s["at"])
                if sc:
                    k = sc[0]
                    row.update({key: k[key] for key in ("label", "entry", "entry_time", "qty", "hit3", "hit5",
                                                        "hit20", "min3", "min5", "min20", "best", "now",
                                                        "result", "pnl")})
            out.append(row)
    return out


# ---------------------------------------------------------------- backtest

def _trade(call, spot, vol, dte, t0, bars, costs=True):
    """Walk 5m candles after the signal. Buy at the estimated ask (+ slippage + commission), sell when the
    estimated bid gets +TAKE_PROFIT or -STOP_LOSS, else at EXIT_CT. Returns (net return, $ pnl, qty)."""
    years = lambda ts: max(dte - (ts - t0).total_seconds() / 86400, 1e-4) / 365
    half = R.SPREAD_PCT / 2 + R.SLIPPAGE_PCT if costs else 0.0
    fee = R.COMMISSION_PER_CONTRACT / 100 if costs else 0.0
    mid0 = S.bs_price(spot, spot, years(t0), vol, call)
    if mid0 <= 0.05:
        return None
    buy = mid0 * (1 + half) + fee
    bid = lambda px, ts: S.bs_price(px, spot, years(ts), vol, call) * (1 - half) - fee
    ret = None
    for ts, b in bars.iterrows():
        path = [b["Open"], b["Low"], b["High"]] if b["Close"] >= b["Open"] else [b["Open"], b["High"], b["Low"]]
        for px in path:
            r = bid(px, ts) / buy - 1
            if r <= -C.STOP_LOSS:
                ret = r if px == b["Open"] else -C.STOP_LOSS
                break
            if r >= C.TAKE_PROFIT:
                ret = C.TAKE_PROFIT
                break
        if ret is not None:
            break
        if (ts + timedelta(minutes=5)).strftime("%H:%M") >= R.EXIT_CT:
            ret = bid(b["Close"], ts) / buy - 1
            break
    if ret is None:
        ts, b = bars.index[-1], bars.iloc[-1]
        ret = bid(b["Close"], ts) / buy - 1
    qty = max(int(C.MAX_TRADE_DOLLARS // (buy * 100)), 1)
    return ret, ret * qty * buy * 100, qty


def metrics(tr):
    if not tr:
        return {"trades": 0}
    pnl = np.array([t["pnl"] for t in tr])
    ret = np.array([t["ret"] for t in tr])
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    eq = np.cumsum(pnl)
    dd = float((np.maximum.accumulate(np.concatenate([[0], eq]))[1:] - eq).max())
    return {"trades": len(tr), "win_rate": round(float((pnl > 0).mean()), 3), "avg_return": round(float(ret.mean()), 4),
            "total_pnl": round(float(pnl.sum())), "profit_factor": round(float(wins.sum() / -losses.sum()), 2) if losses.sum() < 0 else None,
            "max_drawdown": round(dd), "avg_win": round(float(wins.mean())) if len(wins) else 0,
            "avg_loss": round(float(losses.mean())) if len(losses) else 0,
            "risk_reward": round(float(wins.mean() / -losses.mean()), 2) if len(wins) and len(losses) and losses.mean() < 0 else None}


def backtest():
    ts = tickers()
    S.log.info("research backtest: %d tickers", len(ts))
    x15 = {t: ind15(df) for t, df in candles(ts, f"{R.BACKTEST_DAYS}d", "15m").items()}
    b5 = candles(ts, f"{R.BACKTEST_DAYS}d", "5m")
    daily = candles(ts, "1y", "1d")
    today = datetime.now(S.CT).date()
    trades = []
    for t, x in x15.items():
        if t not in b5 or t not in daily:
            continue
        prior = S.daily_bias(daily[t])
        for day in sorted(set(x.index.date)):
            if day >= today or day not in prior.index:
                continue
            p = prior.loc[day]
            vol = max(float(p["rv"]) * 1.1 if not math.isnan(p["rv"]) else R.MIN_VOL, R.MIN_VOL)
            bars_day = b5[t][b5[t].index.date == day]
            for s in signals(x, day):
                call = s["direction"] == "CALLS"
                after = bars_day[bars_day.index >= s["at"]]
                if after.empty:
                    continue
                res = _trade(call, s["close"], vol, S.weekly_dte(day), s["at"], after)
                if res is None:
                    continue
                free = _trade(call, s["close"], vol, S.weekly_dte(day), s["at"], after, costs=False)
                trades.append({"ret0": free[0], "pnl0": free[1], "ticker": t, "date": day.isoformat(), "time": s["time"], "direction": s["direction"],
                               "ret": res[0], "pnl": res[1], "cci_strong": s["cci_strong"],
                               "trend": bool(p["bias"] == (1 if call else -1))})
    trades.sort(key=lambda t: (t["date"], t["time"]))
    days = len({t["date"] for t in trades})
    variants = {"All signals": trades,
                "Daily trend agrees": [t for t in trades if t["trend"]],
                "CCI past ±100": [t for t in trades if t["cci_strong"]],
                "Daily trend agrees + CCI past ±100": [t for t in trades if t["trend"] and t["cci_strong"]],
                "Calls only": [t for t in trades if t["direction"] == "CALLS"],
                "Puts only": [t for t in trades if t["direction"] == "PUTS"],
                "All signals, no spread/slippage/commission": [{**t, "ret": t["ret0"], "pnl": t["pnl0"]} for t in trades]}
    return {"ran": datetime.now(S.CT).isoformat(timespec="minutes"), "days": days,
            "from": trades[0]["date"] if trades else None, "to": trades[-1]["date"] if trades else None,
            "costs": {"spread": R.SPREAD_PCT, "slippage": R.SLIPPAGE_PCT, "commission": R.COMMISSION_PER_CONTRACT},
            "variants": [{"name": k, **metrics(v)} for k, v in variants.items()],
            "by_ticker": sorted([{"ticker": t, **metrics([x for x in trades if x["ticker"] == t])} for t in {x["ticker"] for x in trades}],
                                key=lambda r: -r["total_pnl"])}


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "signals"
    if cmd == "backtest":
        import json
        print(json.dumps(backtest(), indent=1))
    else:
        day = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else datetime.now(S.CT).date()
        x15 = {t: ind15(df) for t, df in candles(tickers(), "60d", "15m").items()}
        for t, x in x15.items():
            for s in signals(x, day):
                print(t, s["direction"], s["time"], "close", s["close"], "ema", s["ema"], "macd", s["macd"],
                      "sig", s["macd_signal"], "cci", s["cci"])


if __name__ == "__main__":
    main()
