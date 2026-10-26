"""Scanner settings. Edit these, then the next scheduled run picks them up."""
import os

# ---------- Universe filters ----------
MIN_MARKET_CAP = 100_000_000      # $100M+
MIN_AVG_VOLUME = 500_000          # average daily shares traded
AVG_VOLUME_DAYS = 30              # window for the average-volume calculation
MIN_PRICE = 5.0                   # sub-$5 names usually have poor option chains; set 0 to disable

# ---------- Trend watchlist (default strategy) ----------
# The pattern: trade only in the direction of the daily trend (EMA45, MACD and
# Supertrend all agree at the prior close), weekly options, in and out the same day, small size.
# Backtests found no timing signal that beats a coin flip, so the bot sends a short morning watchlist
# and enforces the rules that separated the wins from the losses, instead of "buy now" callouts.
STRATEGY = "trend"                # "trend" = morning watchlist + close reminder; "rsi" = legacy RSI callouts
PREMARKET_SCAN = True             # ~8:15 ET = 7:15 CT: system check + likely watchlist with premarket moves
TREND_SCAN_TIME_ET = "09:45"      # 8:45 CT; after the opening swings settle
CLOSE_REMINDER_ET = "15:30"       # 2:30 CT; "close every option you opened today" push; None to disable
WATCHLIST_SIZE = 8
MAX_TRADE_DOLLARS = 1_000         # max cost per trade; the alert sizes contracts to stay under it
STOP_LOSS = 0.20                  # option stop-loss shown in alerts (e.g. 0.20 = -20%); None = manage it yourself
EARNINGS_BUFFER_DAYS = 7          # skip names reporting earnings within this many days
WEEKLY_MIN_DTE = 2                # this Friday's expiry, or next Friday's on Thu/Fri
MIN_OPTION_AVG_VOLUME = 1_000     # the suggested contract must average this many contracts/day (last 5 sessions)
MIN_OPTION_VOLUME_TODAY = 100     # ...and have traded this many already today by the morning scan
MAX_NAMES_CHECKED = 40            # most liquid aligned names to check before giving up on filling the list

# ---------- Legacy RSI strategy (STRATEGY = "rsi") ----------
# CALL: RSI crosses above 30 (trigger) + price > EMA45 + MACD > signal + Supertrend bullish
# PUT:  RSI crosses below 70 (trigger) + price < EMA45 + MACD < signal + Supertrend bearish
EMA_LEN = 45
RSI_LEN = 14
RSI_LOW = 30
RSI_HIGH = 70
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
SUPERTREND_LEN = 7
SUPERTREND_FACTOR = 3.0
# Confirmations (EMA, MACD, Supertrend) must all line up within this many candles of the
# RSI cross, counting the cross candle. 1 = everything on the same candle.
CONFIRM_WINDOW = 5
# VIX is context only. Set a number (e.g. 30) to suppress callouts when VIX is above it.
VIX_MAX = None

TAKE_PROFIT = 0.03                # option profit target shown in alerts and used in the backtest

# ---------- Schedule ----------
SWING_SCAN_TIMES_ET = ["15:40"]   # daily candles; near the close so the candle is nearly final
INTRADAY_START_ET = "09:45"
INTRADAY_END_ET = "15:55"
INTRADAY_INTERVAL = "15m"
INTRADAY_MAX_TICKERS = 400        # most liquid names by $ volume
MAX_INDIVIDUAL_ALERTS = 6         # extra callouts in one scan get bundled into a summary push

# ---------- Option contract suggestion ----------
SWING_TARGET_DTE = 35             # look for an expiration near 35 days out
SWING_MIN_DTE = 21
INTRADAY_TARGET_DTE = 7
INTRADAY_MIN_DTE = 2              # avoid 0-1 DTE by default
MIN_OPEN_INTEREST = 100
MAX_SPREAD_PCT = 0.10             # warn if bid/ask spread > 10% of mid

# ---------- Notifications ----------
NTFY_SERVER = "https://ntfy.sh"
# Your private ntfy topic: from the NTFY_TOPIC env var (cloud secret) or the untracked .ntfy_topic file (Mac).
_topic_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ntfy_topic")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC") or (open(_topic_file).read().strip() if os.path.exists(_topic_file) else "")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "")   # tapping the morning push opens this
MAC_NOTIFICATIONS = os.environ.get("MAC_NOTIFICATIONS", "1") == "1"   # the cloud run sets this to 0
