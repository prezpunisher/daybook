"""Strategy Portfolio Dashboard settings: simulated $1,000 accounts, research only.

Nothing here is read by the live Daybook (scanner.py / config.py) or by the research job. These settings apply only to
portfolio.py, which writes docs/portfolio.json and research_data/portfolio_ledger.json.
"""

MODEL_VERSION = 1

START_CASH = 1_000.00             # every account starts here; no deposits, resets, margin or transfers
TRADE_CAP = 1_000.00              # the Daybook's existing max per trade (config.MAX_TRADE_DOLLARS); cost + fees must fit
FORWARD_START = "2026-11-10"      # first session after the feature was enabled; forward paper ledger starts here
EXIT_CT = "14:50"                 # the existing out-by-2:50 CT rule
MULTIPLIER = 100                  # standard U.S. equity option; adjusted/non-standard contracts are not modeled

# Exit rules the page can compare. The default is the live rule (+3% / -20%); the rest are labeled comparisons.
TARGETS = {"3": 0.03, "5": 0.05, "10": 0.10}
STOPS = {"20": 0.20, "15": 0.15}
DEFAULT_SCENARIO = ("3", "20", "proxy")

# Settlement: a Robinhood cash account; option trades settle T+1, so sale proceeds can't be spent until the next
# session. (Margin accounts can trade unsettled funds but under $25k are limited by the pattern day trader rule;
# that isn't modeled.) No negative cash, no leverage.
SETTLEMENT = "T+1 cash account: sale proceeds become buying power at the next session"

# ---------- Robinhood Financial fees for standard (non-index) U.S. stock/ETF options, non-professional account ----------
FEE_SOURCE = {
    "schedule": "Robinhood Financial Standard Pricing Fee Schedule (document 20260929-5969628-19014374)",
    "url": "https://cdn.robinhood.com/assets/robinhood/legal/RHF%20Fee%20Schedule.pdf",
    "support": "https://robinhood.com/us/en/support/articles/360001214663/",
    "checked": "2026-11-09",
}
COMMISSION_PER_CONTRACT = 0.00    # $0 for stock/ETF options, self-directed non-professional accounts
ORF_OCC_PER_CONTRACT = 0.04       # Options Regulatory + OCC Clearing Fee, buys and sells
CAT_PER_CONTRACT = 0.0003         # Consolidated Audit Trail fee per options contract; rounded to the penny, under 1c -> $0
TAF_PER_CONTRACT = 0.00329        # FINRA Trading Activity Fee, options sells only (effective 2026-01-01); max $9.79
TAF_MAX = 9.79
SEC_RATE = 20.60 / 1_000_000      # SEC regulatory fee on sale principal (effective 2026-04-04), rounded UP to the penny
# Not modeled (marked unsupported if they ever appear): index options ($0.50 / $0.35 contract fee + exchange fees),
# "professional" accounts ($0.50 per contract), exercise/assignment.
SUPPORTED_UNDERLYING = "U.S. stocks and ETFs"

# ---------- accounts (four independent ledgers; a signal in two accounts is two separate hypothetical trades) ----------
ACCOUNTS = [
    {"id": "A", "name": "Current Watchlist", "source": "watchlist",
     "rule": "Every 8:45 watchlist pick, its suggested contract, bought at the first trade after the push."},
    {"id": "B", "name": "EMA + MACD + CCI Shadow", "source": "shadow",
     "rule": "Every 15-minute EMA50 + MACD + CCI signal from the research job, bought at the first trade after the signal candle."},
    {"id": "C", "name": "All Three Aligned", "source": "watchlist",
     "rule": "Watchlist picks where the stock's 15m EMA50, MACD and CCI all agreed with the trade at the push."},
    {"id": "D", "name": "Daily-Trend PUTS", "source": "shadow",
     "rule": "15m shadow signals that are PUTS and agree with the daily trend."},
]
# Tie-breaker for signals with the same entry minute (not a known fill sequence): watchlist = the watchlist's own
# order (most liquid first); shadow = the order of research_config.TICKERS; then the option symbol.
TIE_BREAK = "Same entry minute: watchlist order for picks, research ticker-list order for 15m signals, then option symbol."

# NYSE full-day holidays (sessions are weekdays minus these). Extend each year.
HOLIDAYS = {"2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
            "2026-09-07", "2026-11-26", "2026-12-25",
            "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05",
            "2027-09-06", "2027-11-25", "2027-12-24"}
