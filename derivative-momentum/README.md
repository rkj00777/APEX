# NSE Derivative Momentum Engine (free-data, EOD)

This is an independently runnable research engine. It uses public NSE daily derivatives bhavcopies and TejHQ's keyless/free NSE equity EOD data. No stock list, API key, paid connector, or sample market data is required.

## What it does
- Blindly derives the F&O stock universe from official NSE daily derivatives bhavcopies.
- Builds three daily-close signal families: 20-session breakout + volume confirmation; 3-session compression with futures OI buildup; gap-and-go.
- Adds EMA20/EMA50, relative strength vs Nifty 50 when the free benchmark endpoint is available, and futures price/OI classification. Historical MWPL/ban-list gating is not yet implemented; do not use this build for live trade decisions.
- Backtests option calls using real daily option OHLC from NSE bhavcopies: signal after close, next-session option open entry, target/stop checks on subsequent option OHLC, max 3-session hold, estimated transaction/slippage cost.
- Emits CSV trades, daily scan, metrics JSON, and data-quality report.
- GitHub Actions can run manually or on weekdays. Artifacts are downloadable from the workflow run.

## Important scope boundary
Public EOD data cannot validate 3:15 PM intraday signals, first-five-minute execution, intraday volume-at-time, or intraday option fills. This engine therefore uses **completed daily candles only** and never claims to reproduce an intraday strategy. Daily OHLC stop/target order is ambiguous if both levels are touched in one candle; the backtest applies the conservative stop-first rule. This is a research prototype, not a production-ready trading system. In particular, historical ban-list/MWPL eligibility, contract lot-size history, point-in-time F&O eligibility, and full Indian brokerage/tax charges remain validation gates.

## Run
```bash
python -m pip install -r requirements.txt
python engine.py --mode backtest --start 2024-01-01 --end 2026-10-26 --out output
python engine.py --mode scan --lookback 300 --out output
```

The backtest downloads real source files at run time. It can take a while on a cold cache. The NSE public archive sometimes throttles requests; failed date/symbol combinations are logged and excluded, never filled with invented prices. For a defensible result, require at least 95% expected trading-session coverage and inspect `output/data_quality.json`.

## Data sources
- NSE official derivatives reports and bhavcopy archive: https://www.nseindia.com/all-reports-derivatives
- NSE official historical reports: https://www.nseindia.com/static/resources/historical-reports-capital-market-daily-monthly-archives
- Free TejHQ NSE equity EOD parquet (exchange bhavcopy-derived): https://data.tejhq.dev/nse/year=YYYY/nse_YYYY.parquet
- TejHQ dataset documentation: https://github.com/tejhq/tej-bazaar

## Interpretation
A good-looking backtest is not enough. Before any live use, add walk-forward validation, historical F&O eligibility/ban reconstruction, survivorship-aware universe snapshots, exact contract/lot-size history, realistic brokerage/STT/GST/exchange charges, and paper-trading validation. This repo does not place orders.
