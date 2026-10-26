# Daybook

Scans US stocks (market cap > $100M, 30-day average volume > 500K, price >= $5) and pushes to your
phone (ntfy) and Mac. It only sends alerts and never places trades.

## Default: trend watchlist (`STRATEGY = "trend"` in config.py)
The pattern: trade with the daily trend, weekly options, same day, small size.

- **~7:15 CT:** premarket check with futures, VIX and the likely list.
- **8:45 CT:** one push listing up to 8 of the most liquid names whose EMA45, MACD and Supertrend all
  agreed at yesterday's close and that are still on the trend side this morning. Each line has a
  weekly ATM contract and how many contracts fit under `MAX_TRADE_DOLLARS` ($1,000 by default).
  Names reporting earnings within 7 days, and names where one contract costs more than the limit, are skipped.
- **2:25 CT:** a reminder to close every option opened that day, plus a scorecard of the morning's alerts
  (option volume at the alert, and when each option first hit +3%, +5% or −20%). Also on the dashboard.

All times shown in pushes, on the dashboard and in TradingView are Central. The scheduling logic runs on
Eastern (market) time, so daylight saving never shifts anything.

Backtests (`backtest_trend.py`, plus a 5-year open-to-close check) found no entry timing that predicts
direction better than a coin flip, so the watchlist does not say *when* to buy. Its job is to keep
trades small, with the trend and out by the close, which is what separated the wins from the losses.

## Legacy: RSI callouts (`STRATEGY = "rsi"`)
RSI crosses 30/70, confirmed by EMA45, MACD 12/26/9 and Supertrend 7/3 within 5 candles. Backtested
negative (`backtest.py`): the option hits +3% about 80% of the time, but the misses lose 35-45%.

## Commands
    .venv/bin/python scanner.py trend --dry-run      # build the watchlist now, print instead of notify
    .venv/bin/python scanner.py test-notify
    .venv/bin/python backtest_trend.py               # research backtest of intraday entry triggers
    ./install_schedule.sh                            # run automatically in the background (already installed)

Callout history: data/alerts.csv  ·  Logs: data/scanner.log
