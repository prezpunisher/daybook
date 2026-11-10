# Daybook

Scans US stocks (market cap > $100M, 30-day average volume > 500K, price >= $5) and pushes to your
phone (ntfy) and Mac. It only sends alerts and never places trades.

## Default: trend watchlist (`STRATEGY = "trend"` in config.py)
The pattern: trade with the daily trend, weekly options, same day, small size.

- **8:15 CT:** premarket check with futures, VIX and the likely list.
- **8:45 CT:** one push listing up to 8 of the most liquid names whose EMA45, MACD and Supertrend all
  agreed at yesterday's close and that are still on the trend side this morning. Each line has a
  weekly ATM contract and how many contracts fit under `MAX_TRADE_DOLLARS` ($1,000 by default).
  Names reporting earnings within 7 days, and names where one contract costs more than the limit, are skipped.
  The suggested contract must also average 500+ contracts a day over the last 5 sessions
  (`MIN_OPTION_AVG_VOLUME`), so it's easy to get in and out. SPY, QQQ and IWM are always checked first.
  SpaceX (SPCX), Tesla and Tesla ETFs are never suggested (`EXCLUDE` in config.py).
- **2:30 CT:** a reminder to close every option opened that day. The scorecard
  is no longer attached; it moved to the day review.
- **3:15 CT:** day review from the 1-minute option chart: minutes until each called option first hit +3% / +5%,
  best gain within 5/15/30/60 min, and the result under the rules. Also on the dashboard.

GitHub's built-in cron can start hours late, so a "keeper" workflow run sleeps until each run time,
runs it, and starts the next keeper; a 3-hourly cron only restarts the loop if it ever stops.

All times shown in pushes, on the dashboard and in TradingView are Central. The scheduling logic runs on
Eastern (market) time, so daylight saving never shifts anything.

Backtests (`backtest_trend.py`, plus a 5-year open-to-close check) found no entry timing that predicts
direction better than a coin flip, so the watchlist does not say *when* to buy. Its job is to keep
trades small, with the trend and out by the close, which is what separated the wins from the losses.

## Research (separate from the live pushes)
A daily "Research" GitHub job (`research.py`, settings in `research_config.py`) runs after the close and fills the
dashboard's Research page (`docs/research.html`). It never sends pushes and never touches the watchlist.
- **Watchlist 15m check** (`side_analysis.py`): for each scored pick, the stock's 15-minute EMA50, MACD 12/26/9
  and CCI 20 at the push, to learn why picks fail. After 20 trading days we decide whether it becomes a filter.
- **15m EMA50 + MACD + CCI strategy, in shadow** (`strategy_15m.py`): every signal on 21 liquid names, scored on
  its weekly ATM option's real 1-minute trades with the watchlist's exit rules.
- **Backtest**: the same strategy over 60 days on estimated option prices with spread, slippage and commission,
  rerun weekly. TradingView version: `tradingview/ema50_macd_cci_15m.pine`.

## Strategy Portfolio Dashboard (simulated, research only)
`docs/portfolio.html`, built by `portfolio.py` (settings in `portfolio_config.py`) right after the daily research job.
Four independent simulated $1,000 accounts: A every watchlist pick, B every 15m shadow signal, C watchlist picks where
the 15m EMA50/MACD/CCI all agreed, D shadow PUTS that agree with the daily trend. Whole contracts that fit the free
cash and the $1,000 cap, T+1 settlement (cash account), Robinhood's stock/ETF option fees ($0 commission plus
regulatory fees), exits walked forward minute by minute. Prices are a proxy (1-minute last trades), not bid/ask.
Forward paper trades start 2026-11-10 and are booked once per session (`research_data/portfolio_ledger.json`); a
reconstructed historical replay is shown separately. It reads status.json / research.json and never changes them,
sends nothing and places no orders. Tests: `.venv/bin/python -m unittest discover -s tests`.

## Legacy: RSI callouts (`STRATEGY = "rsi"`)
RSI crosses 30/70, confirmed by EMA45, MACD 12/26/9 and Supertrend 7/3 within 5 candles. Backtested
negative (`backtest.py`): the option hits +3% about 80% of the time, but the misses lose 35-45%.

## Commands
    .venv/bin/python scanner.py trend --dry-run      # build the watchlist now, print instead of notify
    .venv/bin/python scanner.py test-notify
    .venv/bin/python backtest_trend.py               # research backtest of intraday entry triggers
    ./install_schedule.sh                            # run automatically in the background (already installed)

Callout history: data/alerts.csv  ·  Logs: data/scanner.log
