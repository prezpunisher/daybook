#!/usr/bin/env python3
"""Strategy Portfolio Dashboard: four simulated $1,000 accounts, research only.

Reads the existing outputs and never changes them:
  docs/status.json               watchlist picks, their alert-time bid/ask, and each day's 1-minute scorecard
  docs/research.json             15m shadow signals and the watchlist 15m check (all three aligned)
  research_data/minutes/*.json   1-minute option prices saved by the research job
Writes only:
  docs/portfolio.json            what the Portfolio page shows
  research_data/portfolio_ledger.json   the forward paper ledger, appended one session at a time

Never sends pushes or places orders; it has no network access at all. Two modes, never blended:
  forward     observed paper trades from portfolio_config.FORWARD_START on, persisted day by day (re-running a day is a
              no-op; `--rebuild-forward` recomputes it explicitly and records when)
  historical  reconstructed from the records that existed before that date, recomputed each run

Fills are a price proxy: Yahoo's 1-minute option trades (last prints), not bid/ask quotes. "spread" fills apply the
bid/ask spread recorded at the watchlist alert (watchlist accounts only; the 15m signals have no live quote).

  python portfolio.py [--rebuild-forward]
"""
import json
import math
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import no_trade
import portfolio_config as P

ROOT = Path(__file__).resolve().parent
STATUS = ROOT / "docs" / "status.json"
RESEARCH = ROOT / "docs" / "research.json"
MINUTES = ROOT / "research_data" / "minutes"
OUT = ROOT / "docs" / "portfolio.json"
LEDGER = ROOT / "research_data" / "portfolio_ledger.json"
CT = ZoneInfo("America/Chicago")
SHADOW_ORDER = ["SPY", "QQQ", "IWM", "AAPL", "NVDA", "MSFT", "AMZN", "GOOGL", "META", "AMD", "AVGO", "NFLX",
                "PLTR", "JPM", "MRVL", "INTC", "HOOD", "ORCL", "WMT", "NBIS", "CRWV"]   # research_config.TICKERS order
try:
    import research_config
    SHADOW_ORDER = list(research_config.TICKERS)
except Exception:
    pass


# ---------------------------------------------------------------- small helpers

def hm24(clock):
    """'8:49' / '1:15' (CT, the session is 8:30-3:00, no am/pm) -> '08:49' / '13:15'."""
    h, m = map(int, clock.split(":"))
    return f"{h + 12 if h < 8 else h:02d}:{m:02d}"


def cents(x):
    return round(x + 1e-9, 2)


def fee_round(x):
    """Nearest penny, but under one cent rounds down to $0 (CAT and TAF rules)."""
    return 0.0 if x < 0.01 else cents(x)


def buy_fees(q):
    return {"com": cents(P.COMMISSION_PER_CONTRACT * q), "orf": cents(P.ORF_OCC_PER_CONTRACT * q),
            "cat": fee_round(P.CAT_PER_CONTRACT * q), "taf": 0.0, "sec": 0.0}


def sell_fees(q, principal):
    return {"com": cents(P.COMMISSION_PER_CONTRACT * q), "orf": cents(P.ORF_OCC_PER_CONTRACT * q),
            "cat": fee_round(P.CAT_PER_CONTRACT * q), "taf": min(fee_round(P.TAF_PER_CONTRACT * q), P.TAF_MAX),
            "sec": math.ceil(principal * P.SEC_RATE * 100 - 1e-9) / 100 if principal > 0 else 0.0}


def total(fees):
    return cents(sum(fees.values()))


def sessions_between(start, end):
    d, out = date.fromisoformat(start), []
    while d <= date.fromisoformat(end):
        if d.weekday() < 5 and d.isoformat() not in P.HOLIDAYS:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def parse_occ(sym):
    m = re.match(r"^([A-Z.]+?)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$", sym or "")
    if not m:
        return None
    return {"root": m.group(1), "exp": f"20{m.group(2)}-{m.group(3)}-{m.group(4)}", "type": m.group(5),
            "strike": int(m.group(6)) / 1000}


def parse_quote(contract, fallback_hm):
    """'Nov 13 $748P (4 DTE) 8.38/8.48 at 8:56 CT, OI 2,268, IV 24%' -> bid/ask/mid/spread, OI, IV."""
    if not contract:
        return None, None, None
    m = re.search(r"\)\s+([\d.]+)/([\d.]+)(?:\s+at\s+(\d{1,2}:\d{2})\s*CT)?", contract)
    oi = re.search(r"OI ([\d,]+)", contract)
    iv = re.search(r"IV (\d+)%", contract)
    quote = None
    if m:
        bid, ask = float(m.group(1)), float(m.group(2))
        if bid > 0 and ask >= bid:
            mid = (bid + ask) / 2
            quote = {"bid": bid, "ask": ask, "mid": round(mid, 3), "spread": round(ask - bid, 2),
                     "spread_pct": round((ask - bid) / mid, 4), "time": hm24(m.group(3)) if m.group(3) else fallback_hm,
                     "source": "Yahoo option chain at the watchlist alert"}
    return quote, int(oi.group(1).replace(",", "")) if oi else None, int(iv.group(1)) / 100 if iv else None


# ---------------------------------------------------------------- signals (read-only)

def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def candidates(status, research):
    """Every signal any account could trade, keyed by a stable id. Accounts pick their subset."""
    runs = status.get("runs", [])
    cards, picks = {}, {}
    for r in runs:                                       # newest first; the first scorecard per day is the one used
        d = r.get("time", "")[:10]
        if r.get("kind") in ("review", "reminder") and r.get("scorecard"):
            cards.setdefault(d, r["scorecard"])
        if r.get("kind") == "watchlist" and r.get("picks") and r.get("result") != "failed":
            push = datetime.fromisoformat(r["time"]) + timedelta(seconds=r.get("seconds", 0))
            for p in r["picks"]:
                if p.get("symbol"):
                    picks.setdefault((d, p["symbol"]), (p, push.astimezone(CT)))
    aligned = {(w["date"], w.get("symbol")): w.get("all3_ok") for w in research.get("watchlist_check", [])}
    out = {}
    for d, sc in cards.items():
        for rank, k in enumerate(sc):
            sym = k.get("symbol")
            if not sym or k.get("entry") is None:
                continue
            pick, push = picks.get((d, sym), ({}, None))
            push_hm = push.strftime("%H:%M") if push else hm24(k["alert"])
            quote, oi, iv = parse_quote(pick.get("contract"), push_hm)
            cid = f"wl:{d}:{sym}"
            out[cid] = {"id": cid, "src": "watchlist", "date": d, "rank": rank, "ticker": k["ticker"],
                        "dir": k["direction"], "symbol": sym, "occ": parse_occ(sym),
                        "signal": push.isoformat(timespec="seconds") if push else f"{d}T{push_hm}",
                        "push_hm": hm24(k["alert"]), "entry_hm": hm24(k["entry_time"]), "entry": k["entry"],
                        "quote": quote, "vol_live": k.get("vol_before_alert"), "oi": oi, "iv": iv,
                        "all3": aligned.get((d, sym)), "score": {x: k.get(x) for x in
                        ("min3", "min5", "min10", "min20", "hit3", "hit5", "hit10", "hit20", "now")}}
    for r in research.get("shadow", []):
        if not r.get("symbol") or r.get("entry") is None:
            continue
        cid = f"sh:{r['date']}:{hm24(r['time'])}:{r['ticker']}:{r['direction']}"
        rank = SHADOW_ORDER.index(r["ticker"]) if r["ticker"] in SHADOW_ORDER else len(SHADOW_ORDER)
        out[cid] = {"id": cid, "src": "shadow", "date": r["date"], "rank": rank, "ticker": r["ticker"],
                    "dir": r["direction"], "symbol": r["symbol"], "occ": parse_occ(r["symbol"]),
                    "signal": f"{r['date']}T{hm24(r['time'])}", "push_hm": hm24(r["time"]),
                    "entry_hm": hm24(r["entry_time"]), "entry": r["entry"], "quote": None,
                    "vol_live": None, "oi": None, "iv": None, "trend": r.get("daily_trend_agrees"),
                    "score": {x: r.get(x) for x in ("min3", "min5", "min10", "min20", "hit3", "hit5", "hit10", "hit20", "now")}}
    return out


def account_filter(acct_id):
    return {"A": lambda c: c["src"] == "watchlist",
            "B": lambda c: c["src"] == "shadow",
            "C": lambda c: c["src"] == "watchlist" and c.get("all3") is True,
            "D": lambda c: c["src"] == "shadow" and c["dir"] == "PUTS" and c.get("trend") is True}[acct_id]


def source_days(status, research):
    """Sessions each source actually recorded (scored), so a missing day shows as missing, not as a flat day."""
    wl = {r["time"][:10] for r in status.get("runs", []) if r.get("kind") in ("review", "reminder") and r.get("scorecard")}
    wl |= {r["time"][:10] for r in status.get("runs", []) if r.get("kind") == "review"}
    return {"watchlist": wl, "shadow": set(research.get("shadow_days", []))}


# ---------------------------------------------------------------- price paths (no hindsight: walk forward minute by minute)

_minutes_cache = {}


def minutes_for(day):
    if day not in _minutes_cache:
        _minutes_cache[day] = load_json(MINUTES / f"{day}.json", {})
    return _minutes_cache[day]


def path(c, target, stop):
    """Entry and exit for one signal under one exit rule, from the saved 1-minute option prices, or (for the live -20%
    rule only) from the day's scorecard. Returns None when the data can't support it. Same-minute target and stop =
    ambiguous: counted as the stop (conservative)."""
    b = minutes_for(c["date"]).get(c["symbol"])
    if b:
        idx = [i for i, t in enumerate(b["t"]) if c["push_hm"] < t <= P.EXIT_CT]
        if not idx:
            return None
        e = b["o"][idx[0]]
        if not e or e <= 0:
            return None
        lvl_t, lvl_s = e * (1 + target), e * (1 - stop)
        hi = lo = e
        for i in idx:
            t, o, h, l, cl = b["t"][i], b["o"][i], b["h"][i], b["l"][i], b["c"][i]
            hit_s, hit_t = l <= lvl_s, h >= lvl_t
            hi, lo = max(hi, h if not hit_t else lvl_t), min(lo, l if not hit_s else lvl_s)
            if hit_s:
                fill = o if (o < lvl_s and i != idx[0]) else lvl_s          # gapped through the stop: fill at the open
                return {"bt": b["t"][idx[0]], "e": e, "xt": t, "x": fill, "why": "stop", "amb": hit_t,
                        "mfe": hi / e - 1, "mae": lo / e - 1, "data": "minutes"}
            if hit_t:
                return {"bt": b["t"][idx[0]], "e": e, "xt": t, "x": lvl_t, "why": "target", "amb": False,
                        "mfe": hi / e - 1, "mae": lo / e - 1, "data": "minutes"}
        last = idx[-1]
        return {"bt": b["t"][idx[0]], "e": e, "xt": b["t"][last], "x": b["c"][last], "why": "time", "amb": False,
                "mfe": hi / e - 1, "mae": lo / e - 1, "data": "minutes"}
    if abs(stop - 0.20) > 1e-9:
        return None                                      # only the live -20% was recorded before minutes were saved
    s, e = c["score"], c["entry"]
    key = {0.03: "3", 0.05: "5", 0.10: "10"}.get(round(target, 2))
    if key is None or s.get("now") is None:
        return None
    mt, ms = s.get(f"min{key}"), s.get("min20")
    if mt is not None and (ms is None or mt < ms):
        return {"bt": c["entry_hm"], "e": e, "xt": hm24(s[f"hit{key}"]), "x": e * (1 + target), "why": "target",
                "amb": False, "mfe": None, "mae": None, "data": "scorecard"}
    if ms is not None:
        return {"bt": c["entry_hm"], "e": e, "xt": hm24(s["hit20"]), "x": e * 0.8, "why": "stop", "amb": mt == ms,
                "mfe": None, "mae": None, "data": "scorecard"}
    return {"bt": c["entry_hm"], "e": e, "xt": P.EXIT_CT, "x": e * (1 + s["now"]), "why": "time", "amb": False,
            "mfe": None, "mae": None, "data": "scorecard"}


def fills(c, pth, mode):
    """Buy/sell prices. proxy = the last-trade prices themselves. spread = pay half the alert-time quoted spread on
    each side (counted once; nothing else is deducted for the spread)."""
    if mode == "proxy":
        return cents(pth["e"]), cents(pth["x"])
    q = c.get("quote")
    if not q:
        return None
    half = q["spread_pct"] / 2
    return cents(cents(pth["e"]) * (1 + half)), max(cents(cents(pth["x"]) * (1 - half)), 0.0)


# ---------------------------------------------------------------- the ledger

def new_state():
    return {"settled": P.START_CASH, "unsettled": []}       # unsettled: [[amount, trade date]]


def run_sessions(cands, days, recorded, scen, state=None):
    """Replay `days` in order for one account and one scenario. cands: that account's signals. recorded: sessions the
    source has data for. Returns (trades, snapshots, state). Deterministic; cash never goes negative."""
    tgt, stp, mode = P.TARGETS[scen[0]], P.STOPS[scen[1]], scen[2]
    state = json.loads(json.dumps(state or new_state()))
    trades, snaps = [], []
    by_day = {}
    for c in cands:
        by_day.setdefault(c["date"], []).append(c)
    for d in days:
        state["settled"] = cents(state["settled"] + sum(a for a, td in state["unsettled"] if td < d))
        state["unsettled"] = [[a, td] for a, td in state["unsettled"] if td >= d]
        opening = cents(state["settled"] + sum(a for a, _ in state["unsettled"]))
        todays = sorted(by_day.get(d, []), key=lambda c: (c["entry_hm"], c["rank"], c["symbol"]))
        open_pos, realized, n_fills, skipped, incomplete = [], 0.0, 0, 0, False

        def close_until(t):
            nonlocal realized, n_fills
            for pos in sorted([p for p in open_pos if t is None or p["xt"] <= t], key=lambda p: p["xt"]):
                open_pos.remove(pos)
                proceeds = cents(pos["sp"] * P.MULTIPLIER * pos["q"])
                sf = sell_fees(pos["q"], proceeds)
                cash_in = cents(proceeds - total(sf))
                state["unsettled"].append([cash_in, d])
                row = pos["row"]
                fees = {k: cents(row["fees"][k] + sf[k]) for k in sf}
                row.update({"xt": pos["xt"], "xp": pos["sp"], "xr": pos["why"], "fees": fees, "fee": total(fees),
                            "net": cents(cash_in - row["cost"])})
                row["bal"] = cents(state["settled"] + sum(a for a, _ in state["unsettled"]) + sum(p["row"]["cost"] for p in open_pos))
                realized += row["net"]
                n_fills += 1

        for c in todays:
            close_until(c["entry_hm"])
            row = {"id": c["id"]}
            pth = path(c, tgt, stp)
            px = fills(c, pth, mode) if pth else None
            if pth is None or px is None:
                row.update({"st": "skipped", "why": "no price data for this exit rule" if pth is None else "no recorded bid/ask quote"})
                skipped += 1
                incomplete = incomplete or pth is None
                trades.append(row)
                continue
            buy, sell = px
            one = cents(buy * P.MULTIPLIER + total(buy_fees(1)))
            budget = min(state["settled"], P.TRADE_CAP)
            q = int(budget // (buy * P.MULTIPLIER)) if buy > 0 else 0
            while q > 0 and cents(buy * P.MULTIPLIER * q + total(buy_fees(q))) > budget:
                q -= 1
            if q < 1:
                held = sum(a for a, _ in state["unsettled"]) + sum(p["row"]["cost"] for p in open_pos)
                why = (f"1 contract costs ${one:,.2f}, over the ${P.TRADE_CAP:,.0f} per-trade cap" if one > P.TRADE_CAP
                       else f"not enough settled cash (${state['settled']:,.2f} free" +
                       (f", ${held:,.2f} unsettled or in a position)" if held > 0.005 else ")"))
                row.update({"st": "skipped", "why": why, "bp": buy})
                skipped += 1
                trades.append(row)
                continue
            bf = buy_fees(q)
            cost = cents(buy * P.MULTIPLIER * q + total(bf))
            state["settled"] = cents(state["settled"] - cost)
            assert state["settled"] >= -0.005, "negative cash"
            e_px, x_px = cents(pth["e"]), cents(pth["x"])          # last-trade proxy prices, to the cent
            gross = cents((x_px - e_px) * P.MULTIPLIER * q)
            spread = cents((buy - e_px) * P.MULTIPLIER * q + (x_px - sell) * P.MULTIPLIER * q)
            row.update({"st": "filled", "q": q, "bt": pth["bt"], "bp": buy, "cost": cost, "fees": bf, "gross": gross,
                        "spr": spread, "amb": pth["amb"], "data": pth["data"],
                        "mfe": None if pth["mfe"] is None else round(pth["mfe"], 4),
                        "mae": None if pth["mae"] is None else round(pth["mae"], 4)})
            open_pos.append({"xt": pth["xt"], "sp": sell, "why": pth["why"], "q": q, "row": row})
            trades.append(row)
            n_fills += 1
        close_until(None)
        closing = cents(state["settled"] + sum(a for a, _ in state["unsettled"]))
        assert abs(opening + realized - closing) < 0.02, f"{d}: equity doesn't reconcile"
        flag = "no data" if d not in recorded else ("incomplete" if incomplete else ("no signals" if not todays else "ok"))
        snaps.append({"d": d, "open": opening, "close": closing, "pnl": cents(closing - opening),
                      "settled": state["settled"], "unsettled": cents(sum(a for a, _ in state["unsettled"])),
                      "openv": 0.0, "fills": n_fills, "sig": len(todays), "skip": skipped, "flag": flag})
    return trades, snaps, state


def summary(trades, snaps):
    done = [t for t in trades if t.get("st") == "filled" and "net" in t]
    nets = [t["net"] for t in done]
    wins, losses = [x for x in nets if x > 0], [x for x in nets if x <= 0]
    eq = [P.START_CASH] + [s["close"] for s in snaps]
    peak, dd, ddp = eq[0], 0.0, 0.0
    for v in eq:
        peak = max(peak, v)
        if peak - v > dd:
            dd, ddp = peak - v, (peak - v) / peak
    last = snaps[-1] if snaps else None
    unaffordable = sum(1 for t in trades if t.get("st") == "skipped" and ("cash" in t["why"] or "cap" in t["why"]))
    fees = {k: cents(sum(t["fees"][k] for t in done)) for k in ("com", "orf", "cat", "taf", "sec")}
    quoted = [t for t in done if t.get("spr")]
    return {
        "equity": last["close"] if last else P.START_CASH, "net": cents((last["close"] if last else P.START_CASH) - P.START_CASH),
        "net_pct": round(((last["close"] if last else P.START_CASH) / P.START_CASH - 1), 4),
        "today": last["pnl"] if last else 0.0, "today_pct": round(last["pnl"] / last["open"], 4) if last and last["open"] else 0.0,
        "today_date": last["d"] if last else None,
        "settled": last["settled"] if last else P.START_CASH, "unsettled": last["unsettled"] if last else 0.0,
        "openv": 0.0, "trades": len(done), "wins": len(wins), "win_rate": round(len(wins) / len(done), 3) if done else None,
        "expectancy": cents(sum(nets) / len(nets)) if nets else None,
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
        "avg_win": cents(sum(wins) / len(wins)) if wins else None, "avg_loss": cents(sum(losses) / len(losses)) if losses else None,
        "max_dd": cents(dd), "max_dd_pct": round(ddp, 4), "signals": len(trades),
        "skipped": sum(1 for t in trades if t.get("st") == "skipped"), "unaffordable": unaffordable,
        "unaffordable_pct": round(unaffordable / len(trades), 3) if trades else None,
        "gross": cents(sum(t["gross"] for t in done)), "spread": cents(sum(t["spr"] for t in done)),
        "fees": fees, "fees_total": cents(sum(fees.values())), "net_trades": cents(sum(nets)),
        "from_minutes": sum(1 for t in done if t.get("data") == "minutes"), "ambiguous": sum(1 for t in done if t.get("amb")),
        "quoted": len(quoted), "days": len(snaps), "green": sum(1 for s in snaps if s["pnl"] > 0), "red": sum(1 for s in snaps if s["pnl"] < 0),
    }


def scenarios():
    return [(t, s, m) for t in P.TARGETS for s in P.STOPS for m in ("proxy", "spread")]


def skey(scen):
    return "|".join(scen)


# ---------------------------------------------------------------- the two modes

def historical(cands, recorded):
    """Reconstructed from records made before FORWARD_START. Unsupported where the data can't cover every signal."""
    end = (date.fromisoformat(P.FORWARD_START) - timedelta(days=1)).isoformat()
    out = {}
    for a in P.ACCOUNTS:
        mine = [c for c in cands.values() if account_filter(a["id"])(c) and c["date"] <= end]
        rec = recorded[a["source"]]
        first = min([d for d in rec if d <= end] or [None]) if rec else None
        res = {}
        for scen in scenarios():
            if not first:
                res[skey(scen)] = {"status": "unsupported", "reason": "No recorded signals before the forward start."}
                continue
            if scen[2] == "spread" and a["source"] == "shadow":
                res[skey(scen)] = {"status": "unsupported", "reason": "Need recorded bid/ask quotes: the 15m signals are found after the close, so no live quote was saved."}
                continue
            missing = [c for c in mine if path(c, P.TARGETS[scen[0]], P.STOPS[scen[1]]) is None]
            if missing:
                res[skey(scen)] = {"status": "unsupported", "reason": f"−{scen[1]}% needs the option's 1-minute prices, saved only from Nov 5 on; {len(missing)} of {len(mine)} signals lack them."}
                continue
            days = sessions_between(first, end)
            trades, snaps, _ = run_sessions(mine, days, rec, scen)
            res[skey(scen)] = {"status": "ok", "start": first, "trades": trades, "days": snaps, "summary": summary(trades, snaps)}
        out[a["id"]] = res
    return out


def forward(cands, recorded, ledger, rebuild=False, today=None, skip=()):
    """Observed paper trades from FORWARD_START on. A session is booked once, after the research job has processed it
    (it's in research.json's shadow_days); booked sessions are never re-posted unless rebuilding. Changing the no-trade
    list rebuilds it (recorded with the reason), since the cash path depends on which names were traded."""
    skip = sorted(skip)
    why = "requested" if rebuild else "model changed" if ledger.get("model_version") != P.MODEL_VERSION or \
        ledger.get("start") != P.FORWARD_START else "no-trade list changed" if ledger and ledger.get("no_trade", []) != skip else None
    if why:
        ledger = {"model_version": P.MODEL_VERSION, "start": P.FORWARD_START, "no_trade": skip, "rebuilds": ledger.get("rebuilds", []) +
                  ([{"at": datetime.now(CT).isoformat(timespec="seconds"), "why": why}] if ledger else []), "accounts": {}, "cands": {}}
    ready = sorted(d for d in recorded["shadow"] if d >= P.FORWARD_START)
    final = ready[-1] if ready else None
    for a in P.ACCOUNTS:
        acct = ledger["accounts"].setdefault(a["id"], {})
        # a session booked as "no data" whose data arrived later (e.g. a day review rerun that evening): rebook this
        # account from the start so the cash path is right, and record why
        late = sorted({s["d"] for led in acct.values() for s in led["days"] if s["flag"] == "no data" and s["d"] in recorded[a["source"]]})
        if late:
            ledger["accounts"][a["id"]] = acct = {}
            ledger.setdefault("rebuilds", []).append({"at": datetime.now(CT).isoformat(timespec="seconds"),
                                                      "why": f"late data for {', '.join(late)}", "account": a["id"]})
        mine = [c for c in cands.values() if account_filter(a["id"])(c) and c["date"] >= P.FORWARD_START]
        for scen in scenarios():
            led = acct.setdefault(skey(scen), {"last": None, "state": new_state(), "trades": [], "days": []})
            if not final:
                continue
            start = (date.fromisoformat(led["last"]) + timedelta(days=1)).isoformat() if led["last"] else P.FORWARD_START
            days = sessions_between(start, final) if start <= final else []
            if not days:
                continue
            trades, snaps, state = run_sessions([c for c in mine if c["date"] in days], days, recorded[a["source"]], scen, led["state"])
            led["trades"] += trades
            led["days"] += snaps
            led["state"], led["last"] = state, days[-1]
            for c in mine:
                if c["date"] in days:
                    ledger["cands"][c["id"]] = c
    return ledger


def forward_view(ledger):
    out = {}
    for a in P.ACCOUNTS:
        res = {}
        for scen in scenarios():
            led = ledger["accounts"].get(a["id"], {}).get(skey(scen))
            if scen[2] == "spread" and a["source"] == "shadow":
                res[skey(scen)] = {"status": "unsupported", "reason": "Need recorded bid/ask quotes: the 15m signals are found after the close, so no live quote is saved."}
            elif not led or not led["days"]:
                res[skey(scen)] = {"status": "waiting", "reason": f"Starts {P.FORWARD_START}; the first session is booked after that day's close."}
            else:
                res[skey(scen)] = {"status": "ok", "start": P.FORWARD_START, "trades": led["trades"], "days": led["days"],
                                   "summary": summary(led["trades"], led["days"])}
        out[a["id"]] = res
    return out


def slim(c):
    """Candidate fields the page shows (no scoring internals)."""
    return {k: c.get(k) for k in ("date", "ticker", "dir", "symbol", "occ", "signal", "push_hm", "entry_hm", "quote",
                                  "vol_live", "oi", "iv", "src")}


def build(rebuild=False):
    status, research = load_json(STATUS, {}), load_json(RESEARCH, {})
    _minutes_cache.clear()
    skip = no_trade.load()
    every = candidates(status, research)
    cands = {k: c for k, c in every.items() if c["ticker"] not in skip}      # names you won't trade aren't measured
    recorded = source_days(status, research)
    hist = historical(cands, recorded)
    ledger = forward(cands, recorded, load_json(LEDGER, {}), rebuild=rebuild, skip=skip)
    used = {t["id"] for mode in (hist,) for acct in mode.values() for r in acct.values() for t in r.get("trades", [])}
    all_c = {**{k: slim(v) for k, v in cands.items() if k in used}, **{k: slim(v) for k, v in ledger["cands"].items()}}
    out = {
        "model_version": P.MODEL_VERSION, "generated": datetime.now(CT).isoformat(timespec="minutes"),
        "model": {"start_cash": P.START_CASH, "trade_cap": P.TRADE_CAP, "forward_start": P.FORWARD_START,
                  "exit": P.EXIT_CT, "multiplier": P.MULTIPLIER, "settlement": P.SETTLEMENT, "tie_break": P.TIE_BREAK,
                  "fees": {"source": P.FEE_SOURCE, "commission": P.COMMISSION_PER_CONTRACT, "orf_occ": P.ORF_OCC_PER_CONTRACT,
                           "cat": P.CAT_PER_CONTRACT, "taf": P.TAF_PER_CONTRACT, "taf_max": P.TAF_MAX,
                           "sec_per_million": round(P.SEC_RATE * 1_000_000, 2), "applies_to": P.SUPPORTED_UNDERLYING},
                  "targets": list(P.TARGETS), "stops": list(P.STOPS), "default": list(P.DEFAULT_SCENARIO),
                  "no_trade": sorted(skip), "no_trade_signals": len(every) - len(cands)},
        "accounts": P.ACCOUNTS,
        "candidates": all_c,
        "modes": {"forward": {"label": "Forward paper trades", "accounts": forward_view(ledger)},
                  "historical": {"label": "Historical replay (reconstructed)", "accounts": hist}},
    }
    return out, ledger


def main():
    out, ledger = build(rebuild="--rebuild-forward" in sys.argv)
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(ledger, separators=(",", ":"), default=str))
    OUT.write_text(json.dumps(out, separators=(",", ":"), default=str))
    s = out["modes"]["historical"]["accounts"]
    for a in P.ACCOUNTS:
        r = s[a["id"]][skey(P.DEFAULT_SCENARIO)]
        print(a["id"], a["name"], r["status"], r.get("summary", {}).get("equity"), r.get("reason", ""))


if __name__ == "__main__":
    main()
