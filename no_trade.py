#!/usr/bin/env python3
"""Your no-trade list: stocks you won't trade, left out of every measured result (dashboard scorecard, Research page,
simulated portfolios). It never changes the live watchlist or pushes, and signals for these names are still recorded,
so taking a name off the list brings its history back.

Saved in docs/no_trade.json. The dashboard's editor opens a prefilled GitHub issue; the "No-trade list" workflow runs
`python no_trade.py apply` with the issue text, which rewrites the file.
"""
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

FILE = Path(__file__).resolve().parent / "docs" / "no_trade.json"
MAX = 200


def parse(text):
    """Tickers from free text: 'TSLA, aapl' / one per line. Only plain symbols (letters, optional . or -)."""
    seen = []
    for tok in re.findall(r"[A-Za-z][A-Za-z.\-]{0,9}", text or ""):
        t = tok.upper().strip(".-")
        if re.fullmatch(r"[A-Z]{1,6}([.\-][A-Z]{1,2})?", t) and t not in seen:
            seen.append(t)
    return seen[:MAX]


def load():
    try:
        return set(json.loads(FILE.read_text()).get("tickers", []))
    except Exception:
        return set()


def apply(text, source):
    """The issue body has a line 'Tickers: ...' (or the whole body is the list)."""
    m = re.search(r"tickers?\s*:\s*(.*)", text or "", re.I)
    tickers = sorted(parse(m.group(1) if m else ""))
    FILE.write_text(json.dumps({"tickers": tickers, "updated": datetime.now(ZoneInfo("America/Chicago")).isoformat(timespec="minutes"),
                                "source": source}, indent=1) + "\n")
    return tickers


if __name__ == "__main__":
    if sys.argv[1:2] == ["apply"]:
        print("no-trade list:", ", ".join(apply(os.environ.get("ISSUE_BODY", ""), os.environ.get("ISSUE_URL"))) or "(empty)")
