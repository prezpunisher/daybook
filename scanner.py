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
ET = ZoneInfo("America/New_York")
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


def get_universe(max_age_hours=20):
    if UNIVERSE_FILE.exists():
        u = json.loads(UNIVERSE_FILE.read_text())
        age = datetime.now(ET) - datetime.fromisoformat(u["built"])
        if age < timedelta(hours=max_age_hours):
            return u["tickers"]
    return build_universe()


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
            return f"chain unavailable ({e.__class__.__name__})", None, None
    return "chain unavailable (rate limited)", None, None


def _suggest_contract(ticker, direction, price, target_dte, min_dte):
    tk = yf.Ticker(ticker)
    exps = tk.options
    if not exps:
        return "no listed options found", None, None
    today = date.today()
    cands = [(e, (date.fromisoformat(e) - today).days) for e in exps]
    cands = [x for x in cands if x[1] >= min_dte] or cands
    exp, dte = min(cands, key=lambda x: abs(x[1] - target_dte))
    chain = tk.option_chain(exp)
    call = direction == "CALLS"
    tbl = chain.calls if call else chain.puts
    tbl = tbl.iloc[(tbl["strike"] - price).abs().argsort()[:1]]
    if tbl.empty:
        return "no strikes near price", None, None
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
    s = f"{exp_s} ${row['strike']:g}{'C' if call else 'P'} ({dte} DTE) {bid:.2f}/{ask:.2f}, OI {oi:,}"
    if iv > 0.01:
        s += f", IV {iv:.0%}"
    if warn:
        s += " ⚠️ " + ", ".join(warn)
    target = None
    if mid > 0 and iv > 0.01:
        target_stock = stock_price_for_target(price, float(row["strike"]), dte / 365, iv, call, C.TAKE_PROFIT)
        target = f"+{C.TAKE_PROFIT:.0%} target: option ~${mid * (1 + C.TAKE_PROFIT):.2f} ≈ stock ${target_stock:.2f}"
    return s, target, ask or None


def earnings_soon(ticker):
    """Next earnings date if it falls within EARNINGS_BUFFER_DAYS, else None."""
    for attempt in range(3):
        try:
            dates = (yf.Ticker(ticker).calendar or {}).get("Earnings Date") or []
            break
        except yf.exceptions.YFRateLimitError:
            time.sleep(5 * 2 ** attempt)
        except Exception:
            return None
    else:
        return None
    today = date.today()
    soon = [d for d in dates if 0 <= (d - today).days <= C.EARNINGS_BUFFER_DAYS]
    return min(soon) if soon else None


# ---------------------------------------------------------------- notify

def notify(title, message, ticker=None, priority=3, tags=None):
    """Returns True if the phone push was accepted by ntfy."""
    sent = False
    if C.NTFY_TOPIC:
        payload = {"topic": C.NTFY_TOPIC, "title": title, "message": message, "priority": priority,
                   "tags": tags or []}
        if ticker:
            payload["click"] = f"https://www.tradingview.com/chart/?symbol={ticker}"
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
        time.sleep(5)  # let Yahoo's rate limiter cool off after the bulk download
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
            h["contract"], target, _ = suggest_contract(h["ticker"], h["direction"], h["price"], *dte)
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

    time.sleep(5)  # let Yahoo's rate limiter cool off after the bulk download
    picks, skipped, pricey = [], [], []
    for h in cands:
        if len(picks) >= C.WATCHLIST_SIZE:
            break
        er = earnings_soon(h["ticker"])
        if er:
            skipped.append(f"{h['ticker']} ({er:%b %d})")
            continue
        h["contract"], h["target"], ask = suggest_contract(h["ticker"], h["direction"], h["price"],
                                                           weekly_dte(today), C.WEEKLY_MIN_DTE)
        if not ask:
            continue
        h["qty"] = int(C.MAX_TRADE_DOLLARS // (ask * 100))
        if not h["qty"]:                    # one contract already breaks the size limit
            pricey.append(h["ticker"])
            continue
        h["size"] = f"max {h['qty']} contract{'s' if h['qty'] != 1 else ''} (${h['qty'] * ask * 100:,.0f})"
        picks.append(h)

    info.update(aligned=len(cands), skipped_earnings=skipped, skipped_pricey=pricey,
                picks=[{k: (round(v, 2) if isinstance(v, float) else v) for k, v in h.items()
                        if k in ("ticker", "direction", "price", "stop", "day_chg", "contract", "target", "qty", "size")}
                       for h in picks])
    lines = []
    for h in picks:
        call = h["direction"] == "CALLS"
        lines.append(f"{'🟢' if call else '🔴'} {h['ticker']} ${h['price']:.2f} ({h['day_chg']:+.1f}% today)\n"
                     f"   {h['contract']}\n   {h['size']} · skip if {'below' if call else 'above'} ${h['stop']:.2f}")
    stop = f", or at -{C.STOP_LOSS:.0%}" if C.STOP_LOSS else ""
    rules = (f"Rules: in and out today · sell the option at +{C.TAKE_PROFIT:.0%} or more{stop} · "
             f"max ${C.MAX_TRADE_DOLLARS:,} per trade · only the direction shown")
    footer = [vix_text]
    if skipped:
        footer.append("Earnings soon, skipped: " + ", ".join(skipped))
    if pricey:
        footer.append(f"1 contract over ${C.MAX_TRADE_DOLLARS:,}, skipped: " + ", ".join(pricey))
    title = f"Trend watchlist {session:%a %b %d}: {len(picks)} names"
    body = "\n".join(lines + [rules] + footer) if picks else "No names line up with their trend today. Sitting out is a trade too.\n" + vix_text

    if dry_run:
        print(f"\n{title}\n{body}")
        return picks
    info["notified"] = notify(title, body, priority=4, tags=["clipboard"])
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


def close_reminder():
    return notify("⏰ Close today's trades",
           "Sell every option you opened today before 3:50. Holding overnight is where the big losses came from.",
           priority=4, tags=["alarm_clock"])


def cloud():
    """Entry point for GitHub Actions. Cron runs in UTC at both the EDT and EST times; only the run
    that lands in the right Eastern-time window does anything, so DST never shifts or doubles alerts."""
    now = datetime.now(ET)
    hm = now.strftime("%H:%M")
    entry = {"time": now.isoformat(timespec="seconds"), "trigger": os.environ.get("GITHUB_EVENT_NAME", "manual"),
             "run_url": run_url()}
    t0 = time.time()
    try:
        if now.weekday() >= 5 and entry["trigger"] == "schedule":
            entry.update(kind="skip", result="weekend")
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
            entry.update(kind="skip", result=f"{hm} ET is outside both windows")
    except Exception as e:
        log.exception("cloud run failed")
        entry.update(result="failed", error=f"{e.__class__.__name__}: {e}"[:300])
        entry["notified"] = notify("⚠️ Scanner failed", f"The {hm} ET run hit an error: {entry['error']}", priority=4)
        raise
    finally:
        entry["seconds"] = round(time.time() - t0)
        record_run(entry)


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
        md = [f"## {entry.get('kind', 'run').title()}: {entry.get('result', '')}", f"{entry['time']} · {entry['seconds']}s"]
        if entry.get("picks"):
            md += ["", "| | Ticker | Price | Contract | Size | Skip if |", "|---|---|---|---|---|---|"]
            md += [f"| {'🟢' if p['direction'] == 'CALLS' else '🔴'} {p['direction']} | {p['ticker']} | ${p['price']} | "
                   f"{p['contract']} | {p['size']} | ${p['stop']} |" for p in entry["picks"]]
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
    ap.add_argument("cmd", choices=["run", "cloud", "trend", "swing", "intraday", "universe", "test-notify"])
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
    elif a.cmd == "trend":
        run_trend_scan(dry_run=a.dry_run)
    elif a.cmd == "universe":
        build_universe()
    elif a.cmd == "test-notify":
        notify("TEST · Trend watchlist: 2 names",
               "🔴 NVDA $210.96 (-0.6% today)\n   Oct 30 $210P (5 DTE) 3.90/3.95, OI 19,110\n   max 2 contracts ($790) · skip if above $233.44\n"
               "🟢 AAPL $347.73 (-0.1% today)\n   Oct 30 $347.5C (5 DTE) 4.35/4.60, OI 1,800\n   max 2 contracts ($920) · skip if below $326.72\n"
               f"Rules: in and out today · sell the option at +{C.TAKE_PROFIT:.0%} or more, or at -{C.STOP_LOSS:.0%} · "
               f"max ${C.MAX_TRADE_DOLLARS:,} per trade\nThis is a test. Real watchlist arrives 9:45 ET on trading days.",
               priority=4, tags=["clipboard"])
        print("Sent test notification.")
    else:
        run_scan(a.cmd, dry_run=a.dry_run)


if __name__ == "__main__":
    main()
