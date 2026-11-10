"""Research settings. Separate from config.py: nothing here changes the live pushes or the watchlist.
Results go to docs/research.json and the dashboard's Research page."""

ENABLED = True                    # False = the research job does nothing

# ---------- 15m EMA50 + MACD + CCI strategy (shadow: logged and scored, never pushed) ----------
TIMEFRAME = "15m"                 # the strategy only runs on 15-minute candles
EMA_LEN = 50
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
CCI_LEN = 20                      # on HLC3 (typical price)
CCI_UPPER, CCI_LOWER = 100, -100  # past these = strong momentum (tracked, not required)
FIRST_SIGNAL_CT = "08:45"         # first candle close evaluated (the 8:30-8:45 candle)
LAST_SIGNAL_CT = "14:15"          # later signals leave too little time before the 2:50 CT exit
EXIT_CT = "14:50"                 # same exit rules as the watchlist (+3% / -20% / out by 2:50 CT)
TICKERS = ["SPY", "QQQ", "IWM", "AAPL", "NVDA", "MSFT", "AMZN", "GOOGL", "META", "AMD", "AVGO", "NFLX",
           "PLTR", "JPM", "MRVL", "INTC", "HOOD", "ORCL", "WMT", "NBIS", "CRWV"]   # config.EXCLUDE still applies
SHADOW_START = "2026-10-29"       # first day logged; Yahoo drops 1-minute option data about a week after expiry
MIN_DAY_OPTION_VOLUME = 500       # contracts traded that day; below this the signal is flagged "thin"

# ---------- Backtest (estimated option prices; Yahoo keeps no historical bid/ask) ----------
BACKTEST_DAYS = 60                # Yahoo's limit for 15m and 5m candles
BACKTEST_EVERY_DAYS = 7           # rerun the backtest once a week
SPREAD_PCT = 0.04                 # full bid/ask spread as a share of the option price; half paid each side
SLIPPAGE_PCT = 0.005              # extra per side on top of the spread
COMMISSION_PER_CONTRACT = 0.65    # per contract per side; set to your broker's (Robinhood: $0 + small fees)
MIN_VOL = 0.25                    # floor for the volatility used to price options

# ---------- Watchlist 15m check (step 2: why picks fail) ----------
CROSS_LOOKBACK = 4                # "MACD crossed in the last hour" = within 4 candles
CCI_TRACK_FROM = "2026-11-09"     # first trading day after the CCI past ±100 finding; results from here are the real test
DECISION_DAYS = 20                # trading days of tracking before deciding whether to use it as a filter
