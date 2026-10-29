#!/usr/bin/env python3
"""Options callout scanner. Pushes to phone (ntfy) and Mac. Callouts only, it never places trades.

Default (STRATEGY = "trend"): a morning watchlist of names whose daily trend is aligned (EMA45, MACD,
Supertrend) with a weekly contract sized under MAX_TRADE_DOLLARS, plus an afternoon reminder to close
same-day positions. Legacy (STRATEGY = "rsi"): RSI-triggered callouts confirmed by the same indicators.

  python scanner.py run                 # scheduler tick (launchd calls this every 5 min)
  python scanner.py trend [--dry-run]   # build the trend watchlist now
  python scanner.py swing [--dry-run]   # legacy RSI scan on daily candles
  python scanner.py intraday [--dry-run]
  python scanner.py universe            # rebuild the ticker universe
  python scanner.py test-notify
"""
import argparse
import csv
import fcntl
import json
import logging
import math
import os
import re
import subprocess
import sys
import time
import warnings
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import requests
import yfinance as yf

import config as C

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
UNIVERSE_FILE = DATA / "universe.json"
STATE_FILE = DATA / "state.json"
ALERTS_CSV = DATA / "alerts.csv"
STATUS_FILE = ROOT / "docs" / "status.json"      # read by the dashboard (docs/index.html)
STATUS_KEEP = 120
ET = ZoneInfo("America/New_York")    # market clock: all scheduling logic runs on Eastern time
CT = ZoneInfo("America/Chicago")     # what the user reads: every push and the dashboard show Central


def ct(hm_et):
    """"09:45" (Eastern) -> "8:45 CT"."""
    t = datetime.combine(datetime.now(ET).date(), datetime.strptime(hm_et, "%H:%M").time(), ET).astimezone(CT)
    return f"{t.hour % 12 or 12}:{t.minute:02d} CT"
RATE = 0.04

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(DATA / "scanner.log"), logging.StreamHandler()],
)
log = logging.getLogger("scanner")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


# ---------------------------------------------------------------- state

def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"sent": {}, "runs": {}}


def save_state(state):
    # Keep only the last 7 days of dedup keys
    cutoff = (date.today() - timedelta(days=7)).isoformat()
    state["sent"] = {k: v for k, v in state["sent"].items() if v >= cutoff}
    state["runs"] = {k: v for k, v in state["runs"].items() if k[:10] >= cutoff}
    STATE_FILE.write_text(json.dumps(state, indent=1))


# ---------------------------------------------------------------- data

def download(tickers, period, interval):
    """Batch-download OHLCV. Returns {ticker: DataFrame}."""
    out = {}
    for i in range(0, len(tickers), 200):
        chunk = tickers[i:i + 200]
        try:
            df = yf.download(chunk, period=period, interval=interval, group_by="ticker",
                             auto_adjust=False, threads=True, progress=False)
        except Exception as e:
            log.warning("download failed for chunk %d: %s", i, e)
            continue
        if df is None or df.empty:
            continue
        for t in chunk:
            try:
                sub = df[t] if isinstance(df.columns, pd.MultiIndex) else df
            except KeyError:
                continue
            sub = sub.dropna(subset=["Close"])
            if len(sub):
                out[t] = sub
    return out


def build_universe():
    log.info("Building universe (market cap >= $%s, avg volume >= %s)...",
             f"{C.MIN_MARKET_CAP:,}", f"{C.MIN_AVG_VOLUME:,}")
    r = requests.get("https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true",
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    rows = r.json()["data"]["rows"]
    caps = {}
    for row in rows:
        sym = row["symbol"].strip()
        name = row.get("name", "")
        if "^" in sym or any(w in name for w in ("Warrant", " Unit", " Right", "Preferred", "Depositary Shares")):
            continue
        try:
            cap = float(row["marketCap"] or 0)
        except ValueError:
            continue
        if cap >= C.MIN_MARKET_CAP:
            caps[sym.replace("/", "-")] = cap
    log.info("%d tickers pass market cap; checking average volume...", len(caps))

    data = download(list(caps), period="3mo", interval="1d")
    universe = []
    for t, df in data.items():
        tail = df.tail(C.AVG_VOLUME_DAYS)
        avg_vol = float(tail["Volume"].mean())
        price = float(df["Close"].iloc[-1])
        if avg_vol >= C.MIN_AVG_VOLUME and price >= C.MIN_PRICE:
            universe.append({"ticker": t, "market_cap": caps[t], "avg_volume": round(avg_vol),
                             "price": round(price, 2), "dollar_volume": round(avg_vol * price)})
    universe.sort(key=lambda u: -u["dollar_volume"])
    UNIVERSE_FILE.write_text(json.dumps({"built": datetime.now(ET).isoformat(), "tickers": universe}, indent=1))
    log.info("Universe: %d tickers", len(universe))
    return universe


def get_universe(max_age_hours=float(os.environ.get("UNIVERSE_MAX_AGE_HOURS", 20))):
    if UNIVERSE_FILE.exists():
        u = json.loads(UNIVERSE_FILE.read_text())
        age = datetime.now(ET) - datetime.fromisoformat(u["built"])
        if age < timedelta(hours=max_age_hours):
            return with_etfs(u["tickers"])
    return with_etfs(build_universe())


def with_etfs(universe):
    return [{"ticker": t} for t in C.ETFS] + [u for u in universe if u["ticker"] not in C.ETFS and u["ticker"] not in C.EXCLUDE]


def get_vix():
    """Latest VIX close and a plain-English regime label."""
    try:
        v = yf.download("^VIX", period="5d", interval="1d", progress=False, auto_adjust=False)
        level = float(v["Close"].dropna().iloc[-1].squeeze())
    except Exception:
        return None, "VIX unavailable"
    return level, f"VIX {level:.1f} ({vix_regime(level)})"


def vix_regime(level):
    if level < 15:
        return "low vol, options cheap"
    if level < 20:
        return "normal"
    if level < 30:
        return "elevated, options pricier"
    return "high fear, options expensive"


# ---------------------------------------------------------------- indicators

def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(s, n):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def supertrend(h, l, c, n, factor):
    """TradingView-style Supertrend. Returns (line, is_bullish) as arrays."""
    h, l, c = h.to_numpy(float), l.to_numpy(float), c.to_numpy(float)
    pc = np.roll(c, 1)
    pc[0] = c[0]
    tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
    atr = pd.Series(tr).ewm(alpha=1 / n, adjust=False).mean().to_numpy()
    hl2 = (h + l) / 2
    upper, lower = hl2 + factor * atr, hl2 - factor * atr
    fu, fl = upper.copy(), lower.copy()
    bull = np.ones(len(c), dtype=bool)
    for i in range(1, len(c)):
        fl[i] = lower[i] if lower[i] > fl[i - 1] or c[i - 1] < fl[i - 1] else fl[i - 1]
        fu[i] = upper[i] if upper[i] < fu[i - 1] or c[i - 1] > fu[i - 1] else fu[i - 1]
        if bull[i - 1]:
            bull[i] = c[i] >= fl[i]
        else:
            bull[i] = c[i] > fu[i]
    return np.where(bull, fl, fu), bull


def indicators(df):
    c = df["Close"]
    out = pd.DataFrame(index=df.index)
    out["close"] = c
    out["ema"] = ema(c, C.EMA_LEN)
    out["rsi"] = rsi(c, C.RSI_LEN)
    macd = ema(c, C.MACD_FAST) - ema(c, C.MACD_SLOW)
    out["macd"], out["macd_sig"] = macd, ema(macd, C.MACD_SIGNAL)
    out["st_line"], out["st_bull"] = supertrend(df["High"], df["Low"], c, C.SUPERTREND_LEN, C.SUPERTREND_FACTOR)
    return out


def setups(ind):
    """Mark the candle where each setup completes.

    Returns (signal, cross_idx): signal is +1 (call) / -1 (put) / 0 per candle; cross_idx is the
    index of the RSI cross that triggered it. Each RSI cross produces at most one callout.
    """
    r, prev = ind["rsi"].to_numpy(), ind["rsi"].shift(1).to_numpy()
    cross_up = (r > C.RSI_LOW) & (prev <= C.RSI_LOW)
    cross_dn = (r < C.RSI_HIGH) & (prev >= C.RSI_HIGH)
    c = ind["close"].to_numpy()
    bull = (c > ind["ema"].to_numpy()) & (ind["macd"].to_numpy() > ind["macd_sig"].to_numpy()) & ind["st_bull"].to_numpy()
    bear = (c < ind["ema"].to_numpy()) & (ind["macd"].to_numpy() < ind["macd_sig"].to_numpy()) & ~ind["st_bull"].to_numpy()
    n = len(ind)
    signal = np.zeros(n, dtype=int)
    cross_idx = np.full(n, -1)
    last_up = last_dn = fired_up = fired_dn = -10 ** 9
    for i in range(n):
        if cross_up[i]:
            last_up = i
        if cross_dn[i]:
            last_dn = i
        if i - last_up < C.CONFIRM_WINDOW and bull[i] and fired_up != last_up:
            signal[i], cross_idx[i], fired_up = 1, last_up, last_up
        elif i - last_dn < C.CONFIRM_WINDOW and bear[i] and fired_dn != last_dn:
            signal[i], cross_idx[i], fired_dn = -1, last_dn, last_dn
    return signal, cross_idx


def daily_bias(df):
    """Per date: bias (+1 calls / -1 puts / 0) from the PRIOR close, plus prior realized vol and close."""
    ind = indicators(df)
    c = ind["close"]
    bull = (c > ind["ema"]) & (ind["macd"] > ind["macd_sig"]) & ind["st_bull"]
    bear = (c < ind["ema"]) & (ind["macd"] < ind["macd_sig"]) & ~ind["st_bull"]
    bias = pd.Series(np.where(bull, 1, np.where(bear, -1, 0)), index=ind.index)
    rv = np.log(c).diff().rolling(20).std() * math.sqrt(252)
    prior = pd.DataFrame({"bias": bias.shift(1), "rv": rv.shift(1), "prev_close": c.shift(1),
                          "st_line": ind["st_line"].shift(1), "ema": ind["ema"].shift(1)})
    prior.index = prior.index.date
    return prior


def weekly_dte(d):
    """Calendar days to this Friday, or next Friday when this one is under WEEKLY_MIN_DTE away."""
    days = 4 - d.weekday()
    return days + 7 if days < C.WEEKLY_MIN_DTE else days


# ---------------------------------------------------------------- options

def bs_price(spot, strike, years, vol, call):
    if years <= 0 or vol <= 0:
        return max(0.0, spot - strike) if call else max(0.0, strike - spot)
    d1 = (math.log(spot / strike) + (RATE + vol * vol / 2) * years) / (vol * math.sqrt(years))
    d2 = d1 - vol * math.sqrt(years)
    n = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
    if call:
        return spot * n(d1) - strike * math.exp(-RATE * years) * n(d2)
    return strike * math.exp(-RATE * years) * n(-d2) - spot * n(-d1)


def stock_price_for_target(spot, strike, years, vol, call, target):
    """Stock price at which the option gains `target` (e.g. 0.03), via bisection."""
    p0 = bs_price(spot, strike, years, vol, call)
    want = p0 * (1 + target)
    lo, hi = (spot, spot * 1.5) if call else (spot * 0.5, spot)
    for _ in range(50):
        mid = (lo + hi) / 2
        val = bs_price(mid, strike, years, vol, call)
        if (val < want) == call:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def suggest_contract(ticker, direction, price, target_dte, min_dte):
    for attempt in range(4):
        try:
            return _suggest_contract(ticker, direction, price, target_dte, min_dte)
        except yf.exceptions.YFRateLimitError:
            time.sleep(5 * 2 ** attempt)
        except Exception as e:
            return f"chain unavailable ({e.__class__.__name__})", None, None, None
    return "chain unavailable (rate limited)", None, None, None


def _suggest_contract(ticker, direction, price, target_dte, min_dte):
    tk = yf.Ticker(ticker)
    exps = tk.options
    if not exps:
        return "no listed options found", None, None, None
    today = date.today()
    cands = [(e, (date.fromisoformat(e) - today).days) for e in exps]
    cands = [x for x in cands if x[1] >= min_dte] or cands
    exp, dte = min(cands, key=lambda x: abs(x[1] - target_dte))
    chain = tk.option_chain(exp)
    call = direction == "CALLS"
    tbl = chain.calls if call else chain.puts
    tbl = tbl.iloc[(tbl["strike"] - price).abs().argsort()[:1]]
    if tbl.empty:
        return "no strikes near price", None, None, None
    row = tbl.iloc[0]
    bid, ask = float(row["bid"] or 0), float(row["ask"] or 0)
    oi = int(row["openInterest"]) if not math.isnan(row["openInterest"]) else 0
    iv = float(row.get("impliedVolatility") or 0)
    mid = (bid + ask) / 2
    warn = []
    if oi < C.MIN_OPEN_INTEREST:
        warn.append("low OI")
    if mid and (ask - bid) / mid > C.MAX_SPREAD_PCT:
        warn.append("wide spread")
    exp_s = datetime.strptime(exp, "%Y-%m-%d").strftime("%b %d")
    s = f"{exp_s} ${row['strike']:g}{'C' if call else 'P'} ({dte} DTE) {bid:.2f}/{ask:.2f} at {datetime.now(CT):%-I:%M} CT, OI {oi:,}"
    if iv > 0.01:
        s += f", IV {iv:.0%}"
    if warn:
        s += " ⚠️ " + ", ".join(warn)
    target = None
    if mid > 0 and iv > 0.01:
        target_stock = stock_price_for_target(price, float(row["strike"]), dte / 365, iv, call, C.TAKE_PROFIT)
        target = f"+{C.TAKE_PROFIT:.0%} target: option ~${mid * (1 + C.TAKE_PROFIT):.2f} ≈ stock ${target_stock:.2f}"
    return s, target, ask or None, row.get("contractSymbol")


def earnings_soon(ticker):
    """Next earnings date if within EARNINGS_BUFFER_DAYS, None if not, "unknown" if Yahoo won't say.
    A throttled Yahoo can return an empty calendar instead of an error, so empty counts as a miss."""
    if ticker in C.ETFS:
        return None
    for attempt in range(5):
        try:
            cal = yf.Ticker(ticker).calendar or {}
        except yf.exceptions.YFRateLimitError:
            cal = None
        except Exception:
            cal = None
        if cal and "Earnings Date" in cal:
            break
        if cal is not None and cal:          # real calendar without earnings (e.g. some ETFs)
            return None
        time.sleep(3 * 2 ** attempt)
    else:
        return "unknown"
    today = date.today()
    soon = [d for d in cal["Earnings Date"] or [] if 0 <= (d - today).days <= C.EARNINGS_BUFFER_DAYS]
    return min(soon) if soon else None


def option_volume(symbol):
    """Average daily contracts traded over the last 5 completed sessions (None if Yahoo won't say).
    Days Yahoo reports as 0 are gaps in its data, not real zero days, so they're left out of the average."""
    for attempt in range(4):
        try:
            h = yf.Ticker(symbol).history(period="10d", interval="1d")
            break
        except yf.exceptions.YFRateLimitError:
            time.sleep(5 * 2 ** attempt)
        except Exception:
            return None
    else:
        return None
    done = h[h.index.date < datetime.now(ET).date()]["Volume"] if not h.empty else h
    done = done[done > 0].tail(5) if len(done) else done
    return int(done.mean()) if len(done) else 0


def est_contract_cost(ticker_df, call, dte):
    """Rough ATM weekly cost per contract from realized vol (premarket, before real quotes)."""
    c = ticker_df["Close"]
    vol = max(float(np.log(c).diff().tail(20).std() * math.sqrt(252)) * 1.1, 0.25)
    return bs_price(float(c.iloc[-1]), float(c.iloc[-1]), dte / 365, vol, call) * 100


# ---------------------------------------------------------------- notify

def notify(title, message, ticker=None, priority=3, tags=None, click=None):
    """Returns True if the phone push was accepted by ntfy."""
    sent = False
    if C.NTFY_TOPIC:
        payload = {"topic": C.NTFY_TOPIC, "title": title, "message": message, "priority": priority,
                   "tags": tags or []}
        if ticker:
            payload["click"] = f"https://www.tradingview.com/chart/?symbol={ticker}"
        if click:
            payload["click"] = click
        try:
            sent = requests.post(C.NTFY_SERVER, json=payload, timeout=10).ok
        except Exception as e:
            log.warning("ntfy failed: %s", e)
    if C.MAC_NOTIFICATIONS and sys.platform == "darwin":
        esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(["osascript", "-e",
                        f'display notification "{esc(message)}" with title "{esc(title)}" sound name "Glass"'],
                       capture_output=True)
    return sent


# ---------------------------------------------------------------- scans

def check_ticker(df, mode):
    """Return a callout dict if the latest completed candle completes a setup, else None."""
    if mode == "intraday":
        df = df.copy()
        df.index = df.index.tz_convert(ET) if df.index.tz is not None else df.index.tz_localize(ET)
        bar_len = pd.Timedelta(C.INTRADAY_INTERVAL.replace("m", "min"))
        if df.index[-1] + bar_len > datetime.now(ET):
            df = df.iloc[:-1]           # drop the still-forming candle
        if not ("09:30" <= df.index[-1].strftime("%H:%M") <= "15:30"):
            return None
    if len(df) < C.EMA_LEN + 30:
        return None
    ind = indicators(df)
    signal, cross_idx = setups(ind)
    if signal[-1] == 0:
        return None
    last = ind.iloc[-1]
    direction = "CALLS" if signal[-1] > 0 else "PUTS"
    return {
        "direction": direction,
        "price": float(last["close"]),
        "rsi": float(last["rsi"]),
        "ema": float(last["ema"]),
        "macd": float(last["macd"]), "macd_sig": float(last["macd_sig"]),
        "stop": float(last["st_line"]),
        "bars_since_cross": len(ind) - 1 - int(cross_idx[-1]),
        "cross_time": str(ind.index[int(cross_idx[-1])]),
    }


def run_scan(mode, dry_run=False):
    universe = get_universe()
    state = load_state()
    vix, vix_text = get_vix()
    if C.VIX_MAX and vix and vix > C.VIX_MAX:
        log.info("%s scan skipped: VIX %.1f above VIX_MAX %s", mode, vix, C.VIX_MAX)
        return []
    if mode == "swing":
        tickers = [u["ticker"] for u in universe]
        data = download(tickers, period="1y", interval="1d")
        dte = (C.SWING_TARGET_DTE, C.SWING_MIN_DTE)
        label, unit = "Swing", "day"
    else:
        tickers = [u["ticker"] for u in universe[:C.INTRADAY_MAX_TICKERS]]
        data = download(tickers, period="1mo", interval=C.INTRADAY_INTERVAL)
        dte = (C.INTRADAY_TARGET_DTE, C.INTRADAY_MIN_DTE)
        label, unit = "Day trade", "candle"
    log.info("%s scan: %d/%d tickers downloaded", mode, len(data), len(tickers))

    hits = []
    for t, df in data.items():
        try:
            h = check_ticker(df, mode)
        except Exception as e:
            log.debug("%s %s: %s", mode, t, e)
            continue
        if not h:
            continue
        h["ticker"] = t
        h["key"] = f"{mode}|{t}|{h['direction']}|{h['cross_time']}"
        if h["key"] not in state["sent"]:
            hits.append(h)

    log.info("%s scan: %d callouts", mode, len(hits))
    if hits:
        time.sleep(20)  # let Yahoo's rate limiter cool off after the bulk download
    for i, h in enumerate(hits):
        call = h["direction"] == "CALLS"
        ago = h["bars_since_cross"]
        when = "this " + unit if ago == 0 else f"{ago} {unit}{'s' if ago > 1 else ''} ago"
        lines = [
            f"• RSI crossed {'above ' + str(C.RSI_LOW) if call else 'below ' + str(C.RSI_HIGH)} {when} (now {h['rsi']:.0f})",
            f"• {'Above' if call else 'Below'} EMA{C.EMA_LEN} ${h['ema']:.2f} | MACD {'>' if call else '<'} signal | "
            f"Supertrend {'bullish' if call else 'bearish'}",
            vix_text,
        ]
        h["contract"] = ""
        if i < C.MAX_INDIVIDUAL_ALERTS:
            h["contract"], target, _, _ = suggest_contract(h["ticker"], h["direction"], h["price"], *dte)
            lines.append(f"Idea: {h['contract']}")
            if target:
                lines.append(target)
        lines.append(f"Stop: stock {'below' if call else 'above'} Supertrend ${h['stop']:.2f}")
        h["title"] = f"{'🟢' if call else '🔴'} {h['direction']}: {h['ticker']} ${h['price']:.2f} ({label})"
        h["body"] = "\n".join(lines)

    if dry_run:
        for h in hits:
            print(f"\n{h['title']}\n{h['body']}")
        return hits

    for h in hits[:C.MAX_INDIVIDUAL_ALERTS]:
        notify(h["title"], h["body"], ticker=h["ticker"], priority=4,
               tags=["chart_with_upwards_trend" if h["direction"] == "CALLS" else "chart_with_downwards_trend"])
    rest = hits[C.MAX_INDIVIDUAL_ALERTS:]
    if rest:
        notify(f"{label}: {len(rest)} more callouts",
               "\n".join(f"{'🟢' if h['direction'] == 'CALLS' else '🔴'} {h['ticker']} {h['direction']} "
                         f"${h['price']:.2f}, stop ${h['stop']:.2f}" for h in rest[:25]),
               priority=3)

    new_file = not ALERTS_CSV.exists()
    with ALERTS_CSV.open("a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["time", "mode", "ticker", "direction", "price", "rsi", "ema", "stop", "vix", "contract"])
        for h in hits:
            w.writerow([datetime.now(ET).strftime("%Y-%m-%d %H:%M"), mode, h["ticker"], h["direction"],
                        round(h["price"], 2), round(h["rsi"], 1), round(h["ema"], 2), round(h["stop"], 2),
                        round(vix, 1) if vix else "", h["contract"]])
            state["sent"][h["key"]] = date.today().isoformat()
    save_state(state)
    return hits


def run_trend_scan(dry_run=False, info=None):
    """Morning watchlist: names still trading with their daily trend, earnings-week names skipped.
    `info`, if given, is filled with what happened (for the dashboard)."""
    info = {} if info is None else info
    universe = get_universe()
    state = load_state()
    today = datetime.now(ET).date()
    vix, vix_text = get_vix()
    tickers = [u["ticker"] for u in universe]
    data = download(tickers, period="1y", interval="1d")
    log.info("trend scan: %d/%d tickers downloaded", len(data), len(tickers))
    session = max(df.index[-1].date() for df in data.values())   # today, or the last session on weekends
    info.update(universe=len(tickers), downloaded=len(data), session=session.isoformat(),
                vix=round(vix, 1) if vix else None, vix_text=vix_text)
    if not dry_run and session != today:
        log.info("trend scan: market closed today (last session %s); nothing sent", session)
        info.update(result="market closed", picks=[])
        return []

    cands = []
    for t in tickers:                       # universe is sorted by $ volume: most liquid first
        df = data.get(t)
        if df is None or len(df) < C.EMA_LEN + 30 or df.index[-1].date() != session:
            continue                        # need the current session's candle
        try:
            p = daily_bias(df).iloc[-1]     # bias from yesterday's close
        except Exception as e:
            log.debug("trend %s: %s", t, e)
            continue
        if p["bias"] == 0 or math.isnan(p["bias"]):
            continue
        call = p["bias"] > 0
        price = float(df["Close"].iloc[-1])
        # still on the trend side of yesterday's Supertrend and EMA45 this morning
        if (call and (price <= p["st_line"] or price <= p["ema"])) or \
                (not call and (price >= p["st_line"] or price >= p["ema"])):
            continue
        cands.append({"ticker": t, "direction": "CALLS" if call else "PUTS", "price": price,
                      "stop": float(p["st_line"]), "ema": float(p["ema"]),
                      "day_chg": (price / float(p["prev_close"]) - 1) * 100})
    log.info("trend scan: %d aligned names", len(cands))

    time.sleep(20)  # let Yahoo's rate limiter cool off after the bulk download
    picks, skipped, pricey, thin = [], [], [], []
    for h in cands[:C.MAX_NAMES_CHECKED]:   # cap the option lookups so the push doesn't go out late
        if len(picks) >= C.WATCHLIST_SIZE:
            break
        h["contract"], h["target"], ask, h["symbol"] = suggest_contract(h["ticker"], h["direction"], h["price"],
                                                           weekly_dte(today), C.WEEKLY_MIN_DTE)
        if not ask:
            continue
        h["qty"] = int(C.MAX_TRADE_DOLLARS // (ask * 100))
        if not h["qty"]:                    # one contract already breaks the size limit
            pricey.append(h["ticker"])
            continue
        # enough option trading to get in and out near the quote
        avg_vol = option_volume(h["symbol"]) if h["symbol"] else None
        if avg_vol is None or avg_vol < C.MIN_OPTION_AVG_VOLUME:
            thin.append(h["ticker"] if avg_vol is None else f"{h['ticker']} ({avg_vol:,}/day)")
            continue
        h["opt_avg_vol"] = avg_vol
        er = earnings_soon(h["ticker"])
        if er and er != "unknown":
            skipped.append(f"{h['ticker']} ({er:%b %d})")
            continue
        h["earnings_unknown"] = er == "unknown"
        h["size"] = f"max {h['qty']} contract{'s' if h['qty'] != 1 else ''} (${h['qty'] * ask * 100:,.0f})"
        h["mid"] = round((ask + float(h["contract"].split(" at ")[0].split()[-1].split("/")[0])) / 2, 2)
        h["ask"] = ask
        picks.append(h)

    info.update(aligned=len(cands), skipped_earnings=skipped, skipped_pricey=pricey, skipped_thin=thin,
                picks=[{k: (round(v, 2) if isinstance(v, float) else v) for k, v in h.items()
                        if k in ("ticker", "direction", "price", "stop", "day_chg", "contract", "target", "qty", "size", "symbol", "mid",
                                 "opt_avg_vol")}
                       for h in picks])
    lines = [trade_line(h) for h in picks]
    n_put = sum(h["direction"] == "PUTS" for h in picks)
    kinds = " · ".join(x for x in [f"{n_put} put{'s' * (n_put != 1)}" if n_put else "",
                                   f"{len(picks) - n_put} call{'s' * (len(picks) - n_put != 1)}" if len(picks) - n_put else ""] if x)
    title = f"📋 {len(picks)} trades · {kinds}" if picks else f"📋 No trades today ({session:%a})"
    how = (f"Buy at or under the first price, right away: the move is usually in the first 10 min.\n"
           f"Then set a sell at the 2nd price (+{C.TAKE_PROFIT:.0%}). Out at the 3rd (−{C.STOP_LOSS:.0%}). "
           f"All closed by {ct('15:50')}. {vix_text}.")
    body = "\n".join(lines + ["", how]) if picks else "Nothing lines up with its trend. Sitting out is a trade too.\n" + vix_text

    if dry_run:
        print(f"\n{title}\n{body}")
        return picks
    info["notified"] = notify(title, body, priority=4, tags=["clipboard"], click=C.DASHBOARD_URL or None)
    info["result"] = "watchlist sent"
    new_file = not ALERTS_CSV.exists()
    with ALERTS_CSV.open("a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["time", "mode", "ticker", "direction", "price", "rsi", "ema", "stop", "vix", "contract"])
        for h in picks:
            w.writerow([datetime.now(ET).strftime("%Y-%m-%d %H:%M"), "trend", h["ticker"], h["direction"],
                        round(h["price"], 2), "", round(h["ema"], 2), round(h["stop"], 2),
                        round(vix, 1) if vix else "", h["contract"]])
    save_state(state)
    return picks


def trade_line(h):
    """'🔴 NVDA 215 put · Fri · ×2' / 'buy ≤3.85 → sell 3.97 · out 3.08'."""
    call = h["direction"] == "CALLS"
    m = re.match(r"^[A-Z.]+?(\d{6})([CP])(\d{8})$", h.get("symbol") or "")
    if m:
        exp = datetime.strptime(m.group(1), "%y%m%d")
        name = f"{float(m.group(3)) / 1000:g} {'call' if call else 'put'} · exp {exp:%a %-m/%-d}"
    else:
        name = h["contract"].split(" (")[0]
    ask = h["ask"]
    return (f"{'🟢' if call else '🔴'} {h['ticker']} {name} · ×{h['qty']}\n"
            f"    buy ≤{ask:.2f} → sell {ask * (1 + C.TAKE_PROFIT):.2f} · out {ask * (1 - C.STOP_LOSS):.2f}"
            + ("  ⚠️ check earnings date" if h.get("earnings_unknown") else ""))


def run_premarket(dry_run=False, info=None):
    """~8:15 ET (7:15 CT): system check + the likely watchlist (trend aligned at the last close) with premarket moves.
    The real list still comes at 9:45, after the open, with contracts and sizes."""
    info = {} if info is None else info
    universe = get_universe()
    tickers = [u["ticker"] for u in universe]
    data = download(tickers, period="1y", interval="1d")
    today = datetime.now(ET).date()
    info.update(universe=len(tickers), downloaded=len(data))

    # trend at the most recent completed close (premarket, today's candle hasn't started)
    cands = []
    for t in tickers:
        df = data.get(t)
        if df is None or len(df) < C.EMA_LEN + 30:
            continue
        if df.index[-1].date() >= today:
            df = df[df.index.date < today]
        try:
            last = indicators(df).iloc[-1]
        except Exception:
            continue
        c = last["close"]
        if c > last["ema"] and last["macd"] > last["macd_sig"] and last["st_bull"]:
            d = "CALLS"
        elif c < last["ema"] and last["macd"] < last["macd_sig"] and not last["st_bull"]:
            d = "PUTS"
        else:
            continue
        cands.append({"ticker": t, "direction": d, "close": float(c), "stop": float(last["st_line"]),
                      "ema": float(last["ema"])})
    info["aligned"] = len(cands)

    time.sleep(20)  # let Yahoo's rate limiter cool off after the bulk download
    picks, skipped, pricey = [], [], []
    dte = weekly_dte(today)
    for h in cands:
        if len(picks) >= C.WATCHLIST_SIZE:
            break
        df = data[h["ticker"]]
        if est_contract_cost(df[df.index.date < today], h["direction"] == "CALLS", dte) > C.MAX_TRADE_DOLLARS:
            pricey.append(h["ticker"])
            continue
        er = earnings_soon(h["ticker"])
        if er and er != "unknown":
            skipped.append(f"{h['ticker']} ({er:%b %d})")
            continue
        h["earnings_unknown"] = er == "unknown"
        picks.append(h)

    # premarket prices for the picks + index futures
    pre = {}
    try:
        pm = yf.download([h["ticker"] for h in picks] + ["SPY", "QQQ"], period="1d", interval="5m",
                         prepost=True, group_by="ticker", progress=False, auto_adjust=False)
        for t in [h["ticker"] for h in picks] + ["SPY", "QQQ"]:
            try:
                pre[t] = float(pm[t]["Close"].dropna().iloc[-1])
            except Exception:
                pass
    except Exception as e:
        log.warning("premarket prices unavailable: %s", e)
    futs = []
    for sym, name in [("ES=F", "S&P fut"), ("NQ=F", "Nasdaq fut")]:
        try:
            fi = yf.Ticker(sym).fast_info
            futs.append(f"{name} {(fi['last_price'] / fi['previous_close'] - 1) * 100:+.1f}%")
        except Exception:
            pass
    vix, vix_text = get_vix()

    dropping = []
    for h in picks:
        call = h["direction"] == "CALLS"
        p = pre.get(h["ticker"])
        h["premarket"] = p
        h["gap"] = (p / h["close"] - 1) * 100 if p else None
        if p and ((call and (p <= h["stop"] or p <= h["ema"])) or (not call and (p >= h["stop"] or p >= h["ema"]))):
            dropping.append(h["ticker"])
    mkt = " · ".join(futs + [vix_text])
    puts = [h["ticker"] for h in picks if h["direction"] == "PUTS" and h["ticker"] not in dropping]
    calls = [h["ticker"] for h in picks if h["direction"] == "CALLS" and h["ticker"] not in dropping]
    title = f"☀️ Ready for {today:%a}: {len(puts)} puts · {len(calls)} calls likely"
    body = "\n".join([mkt] + ([f"🔴 Puts: {' '.join(puts)}"] if puts else []) + ([f"🟢 Calls: {' '.join(calls)}"] if calls else [])
                      + ([f"Likely to drop off (gapping the wrong way): {' '.join(dropping)}"] if dropping else [])
                      + [f"Exact trades with prices at {ct(C.TREND_SCAN_TIME_ET)}."])
    info.update(market=mkt, skipped_earnings=skipped, skipped_pricey=pricey,
                picks=[{"ticker": h["ticker"], "direction": h["direction"], "price": round(h["close"], 2),
                        "stop": round(h["stop"], 2), "premarket": round(h["premarket"], 2) if h["premarket"] else None,
                        "day_chg": round(h["gap"], 2) if h["gap"] is not None else None} for h in picks])
    if dry_run:
        print(f"\n{title}\n{body}")
        info["result"] = "dry run"
        return picks
    info["notified"] = notify(title, body, priority=4, tags=["sunrise"], click=C.DASHBOARD_URL or None)
    info["result"] = "premarket sent"
    return picks


def close_reminder():
    return notify("⏰ Close today's trades",
                  f"Sell every option you opened today before {ct('15:50')}. Holding overnight is where the big losses came from.",
                  priority=4, tags=["alarm_clock"])


WINDOWS = (5, 15, 30, 60)          # minutes after the push, for "best gain within"


def day_review(info=None):
    """After the close: how each of today's watchlist options did on the 1-minute chart, pushed and saved."""
    info = {} if info is None else info
    sc = info["scorecard"] = score_today()
    if not sc:
        info["result"] = "no watchlist today"
        return None
    info["result"] = "review sent"
    n = len(sc)
    wins = [r for r in sc if r["result"] > 0]
    pnl = sum(r["pnl"] for r in sc)
    by = {w: sum(r["best_in"][str(w)] >= C.TAKE_PROFIT for r in sc) for w in WINDOWS}
    title = f"📊 Day review: {len(wins)}/{n} winners · {'+' if pnl >= 0 else '−'}${abs(pnl):,.0f}"
    lines = []
    for r in sc:
        mins = lambda k: f"{r[k]}m" if r[k] is not None else "—"
        mark = "✅" if r["result"] > 0 else "❌"
        lines.append(f"{mark} {r['ticker']} {r['label']}: +3% in {mins('min3')} · +5% in {mins('min5')} · "
                     f"best {r['best']:+.0%} · {'+' if r['pnl'] >= 0 else '−'}${abs(r['pnl']):,.0f}")
    reach = " · ".join(f"{by[w]}/{n} by {w}m" for w in WINDOWS)
    body = "\n".join(lines + ["", f"Up {C.TAKE_PROFIT:.0%}+: {reach}",
                              f"Bought at the first trade after the {sc[0]['alert']} CT push, ${C.MAX_TRADE_DOLLARS:,} max each. "
                              f"✅ = +{C.TAKE_PROFIT:.0%} came before −{C.STOP_LOSS:.0%}."])
    return notify(title, body, priority=3, tags=["bar_chart"], click=C.DASHBOARD_URL or None)


def score_today():
    try:
        runs = json.loads(STATUS_FILE.read_text()).get("runs", [])
    except Exception:
        return []
    today = datetime.now(ET).date().isoformat()
    w = next((r for r in runs if r.get("kind") == "watchlist" and r.get("picks") and r["time"][:10] == today), None)
    if not w:
        return []
    push = datetime.fromisoformat(w["time"]) + timedelta(seconds=w.get("seconds", 0))
    return score_picks(w["picks"], push)


def score_picks(picks, push):
    """For each pick: option volume when the push went out, and from the first trade after the push, how
    many minutes until the option first traded +3% / +5% / +10% / -20%, the best gain within 5/15/30/60
    minutes, and the result under the rules (+3% before -20%, else -20%, else sold at 3:50 ET).
    Also scored from the push minute's last print ("at_alert"). Times in CT."""
    push = pd.Timestamp(push).tz_convert(CT)
    push_min = push.floor("min")
    day = push.date()
    out = []
    for p in picks:
        sym = p.get("symbol")
        if not sym:
            continue
        try:
            h = yf.Ticker(sym).history(start=day, end=day + timedelta(days=1), interval="1m")
        except Exception as e:
            log.warning("score %s: %s", sym, e)
            continue
        if h.empty:
            continue
        h.index = h.index.tz_convert(CT)
        h = h[h.index.strftime("%H:%M") <= "14:50"]
        sofar, after = h[h.index <= push_min], h[h.index > push_min]
        if after.empty:
            continue
        entry = float(after["Open"].iloc[0])
        t0 = after.index[0]
        first = lambda m: m.index[0] if len(m) else None
        clock = lambda t: t.strftime("%-I:%M") if t is not None else None
        mins = lambda t: int((t - t0).total_seconds() // 60) if t is not None else None
        t3, t5 = first(after[after["High"] >= entry * 1.03]), first(after[after["High"] >= entry * 1.05])
        t10, t20 = first(after[after["High"] >= entry * 1.10]), first(after[after["Low"] <= entry * 0.80])
        tp, sl = C.TAKE_PROFIT, C.STOP_LOSS
        ttp = first(after[after["High"] >= entry * (1 + tp)])
        if ttp is not None and (t20 is None or ttp < t20):
            ret = tp
        elif t20 is not None:
            ret = -sl
        else:
            ret = float(after["Close"].iloc[-1]) / entry - 1
        qty = max(int(C.MAX_TRADE_DOLLARS // (entry * 100)), 1)
        at = h[h.index == push_min]
        at_entry = float(at["Close"].iloc[0]) if len(at) else entry
        m = re.match(r"^[A-Z.]+?\d{6}([CP])(\d{8})$", sym)
        out.append({"ticker": p["ticker"], "direction": p["direction"], "symbol": sym,
                    "label": f"{float(m.group(2)) / 1000:g}{m.group(1)}" if m else "",
                    "alert": push.strftime("%-I:%M"), "quoted_mid": p.get("mid"),
                    "entry": round(entry, 2), "entry_time": clock(t0), "qty": qty,
                    "vol_alert_min": int(sofar["Volume"].iloc[-1]) if len(sofar) else 0,
                    "vol_before_alert": int(sofar["Volume"].sum()),
                    "hit3": clock(t3), "hit5": clock(t5), "hit10": clock(t10), "hit20": clock(t20),
                    "min3": mins(t3), "min5": mins(t5), "min10": mins(t10), "min20": mins(t20),
                    "best_in": {str(w): round(float(after[after.index <= t0 + timedelta(minutes=w)]["High"].max()) / entry - 1, 3)
                                for w in WINDOWS},
                    "at_alert_entry": round(at_entry, 2),
                    "at_alert_hit3": clock(first(after[after["High"] >= at_entry * 1.03])),
                    "at_alert_hit20": clock(first(after[after["Low"] <= at_entry * 0.80])),
                    "best": round(float(after["High"].max()) / entry - 1, 3),
                    "now": round(float(after["Close"].iloc[-1]) / entry - 1, 3),
                    "result": round(ret, 3), "pnl": round(ret * qty * entry * 100)})
        time.sleep(1)
    return out


def cloud():
    """Entry point for GitHub Actions. Cron runs in UTC at both the EDT and EST times; only the run
    that lands in the right Eastern-time window does anything, so DST never shifts or doubles alerts."""
    now = datetime.now(ET)
    hm = now.strftime("%H:%M")
    entry = {"time": now.isoformat(timespec="seconds"), "trigger": os.environ.get("GITHUB_EVENT_NAME", "manual"),
             "run_url": run_url()}
    t0 = time.time()
    forced = os.environ.get("DAYBOOK_SLOT")       # set by the keeper loop, which already waited for the right time
    try:
        if forced:
            entry["trigger"] = "keeper"
            if already_ran(forced, now.date()):
                entry.update(kind="skip", result=f"{forced} already sent today")
                return
            entry["kind"] = forced
            if forced == "premarket":
                run_premarket(info=entry)
            elif forced == "watchlist":
                run_trend_scan(info=entry)
            elif forced == "review":
                entry["notified"] = day_review(info=entry)
            else:
                entry.update(result="reminder sent", notified=close_reminder())
            return
        slot = ("premarket" if "07:50" <= hm <= "08:35" else "watchlist" if "09:40" <= hm <= "10:30"
                else "reminder" if "15:20" <= hm <= "15:55" else None)
        if entry["trigger"] == "schedule" and slot and already_ran(slot, now.date()):
            # a manual run (or an earlier late cron) already covered this slot today
            entry.update(kind="skip", result=f"{slot} already sent today")
            return
        if now.weekday() >= 5 and entry["trigger"] == "schedule":
            entry.update(kind="skip", result="weekend")
        elif "07:50" <= hm <= "08:35" and C.PREMARKET_SCAN:   # 8:40 EST is the summer 9:40 cron
            entry["kind"] = "premarket"
            run_premarket(info=entry)
        elif "09:40" <= hm <= "10:30" or entry["trigger"] == "workflow_dispatch":
            entry["kind"] = "watchlist"
            run_trend_scan(info=entry)
            if entry.get("result") == "market closed" and entry["trigger"] == "workflow_dispatch":
                entry["notified"] = notify("✅ Daybook test run OK",
                                           "Cloud run worked. Market is closed, so no watchlist this time.", priority=3)
        elif "15:20" <= hm <= "15:55" and C.CLOSE_REMINDER_ET:
            entry.update(kind="reminder", result="reminder sent", notified=close_reminder())
        else:
            # the duplicate DST cron: expected, harmless
            entry.update(kind="skip", result=f"{ct(hm)} is outside the run windows")
    except Exception as e:
        log.exception("cloud run failed")
        entry.update(result="failed", error=f"{e.__class__.__name__}: {e}"[:300])
        entry["notified"] = notify("⚠️ Scanner failed", f"The {ct(hm)} run hit an error: {entry['error']}", priority=4)
        raise
    finally:
        entry["seconds"] = round(time.time() - t0)
        record_run(entry)


def wait_for_slot(max_wait=5 * 3600 + 15 * 60, grace=20 * 60):
    """Keeper loop: sleep until the next run time (weekdays, ET) if it's within max_wait, then report which
    one it is. GitHub's own cron can start hours late; this runs within about a minute of the time.
    Writes slot=<premarket|watchlist|reminder|none> to GITHUB_OUTPUT."""
    times = [("premarket", C.PREMARKET_TIME_ET), ("watchlist", C.TREND_SCAN_TIME_ET), ("reminder", C.CLOSE_REMINDER_ET),
             ("review", C.REVIEW_TIME_ET)]
    now = datetime.now(ET)
    due = []
    for i in range(5):
        d = now.date() + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        for kind, hm in times:
            t = datetime.combine(d, datetime.strptime(hm, "%H:%M").time(), ET)
            if t > now - timedelta(seconds=grace) and not (t <= now and already_ran(kind, d)):
                due.append((t, kind))
    t, kind = min(due)
    wait = (t - now).total_seconds()
    if wait > max_wait:
        log.info("keeper: next run %s at %s, sleeping %.0f min then handing off", kind, t, max_wait / 60)
        time.sleep(max_wait)
        kind = "none"
    else:
        log.info("keeper: sleeping %.0f min until %s at %s", max(wait, 0) / 60, kind, t)
        time.sleep(max(wait, 0))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"slot={kind}\n")
    print(kind)


def already_ran(kind, day):
    try:
        runs = json.loads(STATUS_FILE.read_text()).get("runs", [])
    except Exception:
        return False
    return any(r.get("kind") == kind and r.get("result") != "failed" and r.get("time", "")[:10] == day.isoformat()
               for r in runs)


def run_url():
    e = os.environ
    if e.get("GITHUB_RUN_ID"):
        return f"{e.get('GITHUB_SERVER_URL', 'https://github.com')}/{e['GITHUB_REPOSITORY']}/actions/runs/{e['GITHUB_RUN_ID']}"
    return None


def record_run(entry):
    """Prepend this run to docs/status.json (dashboard) and write a GitHub Actions run summary."""
    try:
        status = json.loads(STATUS_FILE.read_text())
    except Exception:
        status = {"runs": []}
    if entry.get("kind") != "skip":            # skips are noise; keep only the latest one for "last checked"
        status["runs"] = [entry] + status["runs"][:STATUS_KEEP - 1]
    status["last_checked"] = entry["time"]
    status["settings"] = {"take_profit": C.TAKE_PROFIT, "stop_loss": C.STOP_LOSS, "max_trade": C.MAX_TRADE_DOLLARS,
                          "watchlist_size": C.WATCHLIST_SIZE, "scan_time": C.TREND_SCAN_TIME_ET,
                          "reminder_time": C.CLOSE_REMINDER_ET}
    STATUS_FILE.parent.mkdir(exist_ok=True)
    STATUS_FILE.write_text(json.dumps(status, indent=1, default=str))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        md = [f"## {entry.get('kind', 'run').title()}: {entry.get('result', '')}", f"{datetime.fromisoformat(entry['time']).astimezone(CT):%Y-%m-%d %-I:%M %p} CT · {entry['seconds']}s"]
        if entry.get("picks"):
            md += ["", "| | Ticker | Price | Contract | Size | Skip if |", "|---|---|---|---|---|---|"]
            md += [f"| {'🟢' if p['direction'] == 'CALLS' else '🔴'} {p['direction']} | {p['ticker']} | ${p['price']} | "
                   f"{p.get('contract', '')} | {p.get('size', '')} | ${p['stop']} |" for p in entry["picks"]]
        if entry.get("error"):
            md += ["", f"**Error:** `{entry['error']}`"]
        with open(summary, "a") as f:
            f.write("\n".join(md) + "\n")


def tick():
    """Called every few minutes by launchd; decides what (if anything) to run."""
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return
    hm = now.strftime("%H:%M")
    day = now.date().isoformat()
    state = load_state()

    if C.STRATEGY == "trend":
        for slot, job in [(C.TREND_SCAN_TIME_ET, run_trend_scan), (C.CLOSE_REMINDER_ET, close_reminder)]:
            key = f"{day}|trend|{slot}"
            if slot and slot <= hm <= "16:00" and key not in state["runs"]:
                state["runs"][key] = day
                save_state(state)
                job()
                state = load_state()
        return

    for slot in C.SWING_SCAN_TIMES_ET:
        key = f"{day}|swing|{slot}"
        if slot <= hm <= "16:30" and key not in state["runs"]:
            state["runs"][key] = day
            save_state(state)
            run_scan("swing")
            state = load_state()

    if C.INTRADAY_START_ET <= hm <= C.INTRADAY_END_ET:
        bucket = now.replace(minute=now.minute - now.minute % 15).strftime("%H:%M")
        key = f"{day}|intraday|{bucket}"
        if key not in state["runs"]:
            state["runs"][key] = day
            save_state(state)
            run_scan("intraday")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "cloud", "wait", "premarket", "trend", "swing", "intraday", "universe", "test-notify"])
    ap.add_argument("--dry-run", action="store_true", help="print callouts instead of sending")
    a = ap.parse_args()

    lock = open(DATA / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("another scan is running; skipping")
        return

    if a.cmd == "run":
        tick()
    elif a.cmd == "cloud":
        cloud()
    elif a.cmd == "wait":
        wait_for_slot()
    elif a.cmd == "premarket":
        run_premarket(dry_run=a.dry_run)
    elif a.cmd == "trend":
        run_trend_scan(dry_run=a.dry_run)
    elif a.cmd == "universe":
        build_universe()
    elif a.cmd == "test-notify":
        demo = [{"ticker": "NVDA", "direction": "PUTS", "symbol": "NVDA261030P00215000", "ask": 3.85, "qty": 2},
                {"ticker": "WMT", "direction": "CALLS", "symbol": "WMT261030C00120000", "ask": 1.63, "qty": 6}]
        notify("TEST · 📋 2 trades · 1 put · 1 call", "\n".join([trade_line(h) for h in demo] +
               ["", f"This is a test. Real trades arrive around {ct(C.TREND_SCAN_TIME_ET)} on trading days."]),
               priority=4, tags=["clipboard"])
        print("Sent test notification.")
    else:
        run_scan(a.cmd, dry_run=a.dry_run)


if __name__ == "__main__":
    main()
