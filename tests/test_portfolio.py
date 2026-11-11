"""Tests for the simulated Strategy Portfolio Dashboard (portfolio.py). Run: .venv/bin/python -m unittest discover tests"""
import hashlib
import json
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import portfolio as PF          # noqa: E402
import portfolio_config as P    # noqa: E402


def bars(rows):
    """[(hh:mm, open, high, low, close)] -> saved-minutes format."""
    return {"t": [r[0] for r in rows], "o": [r[1] for r in rows], "h": [r[2] for r in rows], "l": [r[3] for r in rows],
            "c": [r[4] for r in rows], "v": [10] * len(rows)}


def cand(cid, day, symbol, push="08:47", entry=5.0, rank=0, src="watchlist", quote=None, **extra):
    return {"id": cid, "src": src, "date": day, "rank": rank, "ticker": symbol[:4], "dir": "PUTS", "symbol": symbol,
            "occ": None, "signal": f"{day}T{push}", "push_hm": push, "entry_hm": PF.hm24(push) if ":" in push else push,
            "entry": entry, "quote": quote, "score": {}, **extra}


class Base(unittest.TestCase):
    def setUp(self):
        PF._minutes_cache.clear()

    def minutes(self, day, data):
        PF._minutes_cache[day] = data


class Settings(unittest.TestCase):
    def test_live_settings_unchanged(self):
        import config as C
        import research_config as R
        self.assertEqual((C.TAKE_PROFIT, C.STOP_LOSS, C.MAX_TRADE_DOLLARS), (0.03, 0.20, 1_000))
        self.assertEqual((C.PREMARKET_TIME_ET, C.TREND_SCAN_TIME_ET, C.CLOSE_REMINDER_ET, C.REVIEW_TIME_ET),
                         ("09:15", "09:45", "15:30", "16:15"))
        self.assertEqual((R.EMA_LEN, R.MACD_FAST, R.MACD_SLOW, R.MACD_SIGNAL, R.CCI_LEN, R.EXIT_CT),
                         (50, 12, 26, 9, 20, "14:50"))
        self.assertEqual((R.SPREAD_PCT, R.SLIPPAGE_PCT, R.COMMISSION_PER_CONTRACT), (0.04, 0.005, 0.65))  # old backtest model
        self.assertEqual(P.DEFAULT_SCENARIO, ("3", "20", "proxy"))
        self.assertEqual((P.START_CASH, P.TRADE_CAP, P.EXIT_CT), (1000.0, float(C.MAX_TRADE_DOLLARS), R.EXIT_CT))

    def test_portfolio_never_imports_alerting_code(self):
        code = "import sys; sys.path.insert(0, %r); import portfolio; print('scanner' in sys.modules, 'requests' in sys.modules, 'yfinance' in sys.modules)" % str(ROOT)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT).stdout.split()
        self.assertEqual(out, ["False", "False", "False"])


class Fees(unittest.TestCase):
    def test_robinhood_stock_option_fees(self):
        self.assertEqual(P.COMMISSION_PER_CONTRACT, 0.0)
        b = PF.buy_fees(3)
        self.assertEqual((b["com"], b["orf"], b["cat"], b["taf"], b["sec"]), (0.0, 0.12, 0.0, 0.0, 0.0))
        s = PF.sell_fees(3, 1000.0)                       # TAF 0.00987 is under a cent -> $0; SEC 0.0206 rounds up to $0.03
        self.assertEqual((s["com"], s["orf"], s["cat"], s["taf"], s["sec"]), (0.0, 0.12, 0.0, 0.0, 0.03))
        self.assertEqual(PF.sell_fees(4, 1000.0)["taf"], 0.01)          # 0.01316 -> $0.01
        self.assertEqual(PF.sell_fees(1, 100.0)["taf"], 0.0)           # under a cent rounds down to $0
        self.assertEqual(PF.sell_fees(10_000, 1e6)["taf"], 9.79)        # capped


class Ledger(Base):
    D = "2026-11-10"

    def run1(self, cands, scen=("3", "20", "proxy"), days=None, state=None):
        return PF.run_sessions(cands, days or [self.D], {self.D, "2026-11-11"}, scen, state)

    def test_starts_at_exactly_1000_and_reconciles(self):
        trades, snaps, state = self.run1([])
        self.assertEqual(PF.new_state()["settled"], 1000.0)
        self.assertEqual((snaps[0]["open"], snaps[0]["close"], snaps[0]["flag"]), (1000.0, 1000.0, "no signals"))

    def test_cannot_buy_one_contract_over_the_cap(self):
        self.minutes(self.D, {"X1": bars([("08:48", 10.01, 10.5, 10.0, 10.4)]), "X2": bars([("08:48", 9.99, 10.4, 9.9, 10.3)])})
        t, s, _ = self.run1([cand("a", self.D, "X1", entry=10.01)])
        self.assertEqual(t[0]["st"], "skipped")
        self.assertIn("cap", t[0]["why"])
        self.assertEqual(s[0]["close"], 1000.0)          # skipping can't make money
        t, s, _ = self.run1([cand("b", self.D, "X2", entry=9.99)])
        self.assertEqual((t[0]["st"], t[0]["q"], t[0]["cost"]), ("filled", 1, 999.04))

    def test_no_double_spend_and_t_plus_1(self):
        day = {"S1": bars([("08:48", 6.0, 6.0, 5.9, 5.95), ("08:49", 6.0, 6.2, 5.9, 6.1)]),
               "S2": bars([("08:48", 6.0, 6.3, 5.9, 6.2)])}
        self.minutes(self.D, day)
        self.minutes("2026-11-11", {"S3": bars([("08:48", 6.0, 6.3, 5.9, 6.2)])})
        cs = [cand("a", self.D, "S1", rank=0), cand("b", self.D, "S2", rank=1),
              cand("c", "2026-11-11", "S3")]
        t, s, state = self.run1(cs, days=[self.D, "2026-11-11"])
        self.assertEqual([x["st"] for x in t], ["filled", "skipped", "filled"])
        self.assertIn("settled cash", t[1]["why"])
        for snap in s:
            self.assertGreaterEqual(snap["settled"], 0)
            self.assertAlmostEqual(snap["open"] + snap["pnl"], snap["close"], places=2)
        # day 1's sale settles for day 2
        self.assertGreater(s[1]["open"], 1000)
        nets = sum(x["net"] for x in t if x["st"] == "filled")
        self.assertAlmostEqual(1000 + nets, s[-1]["close"], places=2)

    def test_accounts_independent(self):
        self.minutes(self.D, {"S1": bars([("08:48", 6.0, 6.3, 5.9, 6.2)])})
        c = [cand("a", self.D, "S1")]
        s0 = PF.new_state()
        _, _, st1 = self.run1(c, state=s0)
        _, _, st2 = self.run1([], state=s0)
        self.assertEqual(s0, PF.new_state())              # caller's state untouched
        self.assertNotEqual(st1, st2)

    def test_spread_counted_once(self):
        self.minutes(self.D, {"S1": bars([("08:48", 5.0, 5.0, 4.9, 4.95), ("08:49", 5.0, 5.3, 4.9, 5.2)])})
        q = {"bid": 4.9, "ask": 5.1, "mid": 5.0, "spread": 0.2, "spread_pct": 0.04, "time": "08:47"}
        t, _, _ = self.run1([cand("a", self.D, "S1", quote=q)], scen=("3", "20", "spread"))
        r = t[0]
        self.assertEqual((r["bp"], r["xp"]), (5.1, 5.05))   # +2% on the buy, -2% on the sale; nothing else
        self.assertAlmostEqual(r["net"], r["gross"] - r["spr"] - r["fee"], places=2)
        t2, _, _ = self.run1([cand("a", self.D, "S1", quote=q)])
        self.assertEqual(t2[0]["spr"], 0.0)                 # proxy fills: no spread deducted

    def test_missing_data_makes_no_profit(self):
        self.minutes(self.D, {})
        t, s, _ = self.run1([cand("a", self.D, "NOPE")])
        self.assertEqual(t[0]["st"], "skipped")
        self.assertEqual((s[0]["close"], s[0]["flag"]), (1000.0, "incomplete"))
        self.assertIsNone(PF.path(cand("b", self.D, "NOPE"), 0.03, 0.15))   # -15% never guessed from a scorecard

    def test_ambiguous_minute_counts_as_stop(self):
        self.minutes(self.D, {"S1": bars([("08:48", 5.0, 5.0, 5.0, 5.0), ("08:49", 5.0, 5.5, 3.9, 4.0)])})
        p = PF.path(cand("a", self.D, "S1"), 0.03, 0.20)
        self.assertEqual((p["why"], p["amb"]), ("stop", True))

    def test_rerun_does_not_double_post(self):
        self.minutes(self.D, {"S1": bars([("08:48", 6.0, 6.3, 5.9, 6.2)])})
        c = cand("wl:x", self.D, "S1")
        cands = {c["id"]: c}
        rec = {"watchlist": {self.D}, "shadow": {self.D}}
        with mock.patch.object(P, "FORWARD_START", self.D):
            l1 = PF.forward(cands, rec, {})
            l2 = PF.forward(cands, rec, json.loads(json.dumps(l1)))
        a1, a2 = l1["accounts"]["A"]["3|20|proxy"], l2["accounts"]["A"]["3|20|proxy"]
        self.assertEqual(len(a1["trades"]), 1)
        self.assertEqual(a1["trades"], a2["trades"])
        self.assertEqual(a1["days"], a2["days"])


class ExistingOutputs(unittest.TestCase):
    FILES = ["docs/status.json", "docs/research.json", "config.py", "research_config.py", "scanner.py", "research.py"]

    def digest(self):
        return {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in self.FILES}

    def test_job_leaves_existing_files_untouched_and_never_networks(self):
        before = self.digest()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(PF, "OUT", Path(tmp) / "p.json"), mock.patch.object(PF, "LEDGER", Path(tmp) / "l.json"), \
                mock.patch.object(socket, "socket", side_effect=AssertionError("network used")):
            PF.main()
            out = json.loads((Path(tmp) / "p.json").read_text())
        self.assertEqual(before, self.digest())
        for mode in out["modes"].values():
            for acct in mode["accounts"].values():
                for r in acct.values():
                    if r["status"] == "ok":
                        self.assertEqual(r["days"][0]["open"], 1000.0)

    # sha256 of research.html's renderBacktest(), pinned 2026-11-10 (Best removed at the user's request). Change it
    # only on purpose: a mismatch means the backtest section of the Research page was altered.
    BACKTEST_RENDERER = "1103ee6d9751840bbcd0c062b8160a5d9cc837cca7edb7eabde673a19c2306c7"

    def test_backtest_section_renders_as_before(self):
        s = (ROOT / "docs/research.html").read_text()
        code = s[s.index("function renderBacktest()"):s.index("function render()")]
        self.assertEqual(hashlib.sha256(code.encode()).hexdigest(), self.BACKTEST_RENDERER)


class NoTradeList(Base):
    D = "2026-11-10"

    def test_parse(self):
        import no_trade
        self.assertEqual(no_trade.parse("amzn, HOOD\nbrk.b  tsla tsla 123 $$"), ["AMZN", "HOOD", "BRK.B", "TSLA"])

    def test_excluded_names_are_not_traded_and_rebuild_is_recorded(self):
        import no_trade
        self.minutes(self.D, {"S1": bars([("08:48", 6.0, 6.3, 5.9, 6.2)]), "S2": bars([("08:48", 3.0, 3.2, 2.9, 3.1)])})
        c1, c2 = cand("wl:1", self.D, "S1", rank=0), cand("wl:2", self.D, "S2", rank=1)
        c1["ticker"], c2["ticker"] = "AMZN", "HOOD"
        rec = {"watchlist": {self.D}, "shadow": {self.D}}
        with mock.patch.object(P, "FORWARD_START", self.D):
            l1 = PF.forward({c["id"]: c for c in (c1, c2)}, rec, {})
            l2 = PF.forward({"wl:2": c2}, rec, json.loads(json.dumps(l1)), skip={"AMZN"})
        ids = [t["id"] for t in l2["accounts"]["A"]["3|20|proxy"]["trades"]]
        self.assertEqual(ids, ["wl:2"])
        self.assertEqual(l2["no_trade"], ["AMZN"])
        self.assertEqual(l2["rebuilds"][-1]["why"], "no-trade list changed")


if __name__ == "__main__":
    unittest.main()
