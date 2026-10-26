#!/usr/bin/env python3
"""Free-source NSE EOD derivative momentum scanner and option OHLC backtest.

Signals are computed only from completed daily bars. This intentionally does not
simulate 3:15 PM or first-five-minute intraday execution.
"""
from __future__ import annotations
import argparse, io, json, math, os, time, zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
import pandas as pd
import requests

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128 Safari/537.36",
    "Accept": "*/*", "Referer": "https://www.nseindia.com/",
}
SESSION = requests.Session()
SESSION.headers.update(NSE_HEADERS)
CACHE = Path("cache")
OLD_CUTOFF = date(2024, 7, 5)
COST_PER_SIDE = 0.005  # conservative 0.5% premium friction per side; replace with broker-specific charges
TARGET = 0.30
STOP = 0.20
MAX_HOLD = 3

def norm(s: Any) -> str:
    return "".join(ch for ch in str(s).strip().lower() if ch.isalnum())

def pick_col(df: pd.DataFrame, aliases: list[str], required: bool = True) -> str | None:
    lookup = {norm(c): c for c in df.columns}
    for a in aliases:
        if norm(a) in lookup:
            return lookup[norm(a)]
    if required:
        raise ValueError(f"Missing required column among {aliases}; columns={list(df.columns)[:60]}")
    return None

def request_bytes(url: str, tries: int = 4) -> bytes:
    last = None
    for attempt in range(tries):
        try:
            r = requests.get(url, headers=NSE_HEADERS if "nseindia" in url else {}, timeout=45)
            if r.status_code == 404:
                raise FileNotFoundError(url)
            r.raise_for_status()
            if not r.content:
                raise ValueError("empty response")
            return r.content
        except FileNotFoundError:
            raise
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Download failed after {tries} attempts: {url}: {last}")

def read_equity(start: str, end: str) -> pd.DataFrame:
    """Read official-bhavcopy-derived, free TejHQ annual NSE parquet files."""
    out = []
    years = range(pd.Timestamp(start).year, pd.Timestamp(end).year + 1)
    CACHE.mkdir(exist_ok=True)
    for year in years:
        p = CACHE / f"nse_{year}.parquet"
        if not p.exists():
            url = f"https://data.tejhq.dev/nse/year={year}/nse_{year}.parquet"
            try:
                p.write_bytes(request_bytes(url))
            except Exception as e:
                print(f"WARN equity parquet unavailable for {year}: {e}")
                continue
        try:
            d = pd.read_parquet(p)
            d.columns = [str(c).lower() for c in d.columns]
            need = {"date","symbol","open","high","low","close","volume"}
            if not need.issubset(d.columns):
                raise ValueError(f"Unexpected TejHQ schema for {year}: {list(d.columns)}")
            d["date"] = pd.to_datetime(d["date"], errors="coerce").dt.normalize()
            d = d[(d.date >= pd.Timestamp(start)) & (d.date <= pd.Timestamp(end))]
            if "series" in d:
                d = d[d.series.astype(str).isin(["EQ","BE","BZ"])]
            out.append(d[["date","symbol","open","high","low","close","volume"]].copy())
        except Exception as e:
            print(f"WARN bad equity parquet {year}: {e}")
    if not out:
        raise RuntimeError("No real equity EOD data loaded. Check TejHQ availability and output/data_quality.json.")
    df = pd.concat(out, ignore_index=True)
    for c in ["open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["date","symbol","close"]).drop_duplicates(["date","symbol"]).sort_values(["symbol","date"])
    return df

def fno_url(day: date) -> str:
    if day <= OLD_CUTOFF:
        month = day.strftime("%b").upper()
        return f"https://archives.nseindia.com/content/historical/DERIVATIVES/{day:%Y}/{month}/fo{day:%d}{month}{day:%Y}bhav.csv.zip"
    return f"https://archives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{day:%Y%m%d}_F_0000.csv.zip"

def parse_fno_bytes(blob: bytes, day: date) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise ValueError("zip has no CSV")
        raw = pd.read_csv(z.open(names[0]), low_memory=False)
    # Legacy NSE bhavcopy and current UDiFF use different schemas.
    sym = pick_col(raw, ["TckrSymb","SYMBOL","symbol"])
    inst = pick_col(raw, ["FinInstrmTp","INSTRUMENT","instrument"], required=False)
    exp = pick_col(raw, ["XpryDt","EXPIRY_DT","EXPIRY","expiry"], required=False)
    strike = pick_col(raw, ["StrkPric","STRIKE_PR","STRIKE","strike"], required=False)
    opt = pick_col(raw, ["OptnTp","OPTION_TYP","OPTION_TYPE","optiontype"], required=False)
    o = pick_col(raw, ["OpnPric","OPEN","open"])
    h = pick_col(raw, ["HghPric","HIGH","high"])
    l = pick_col(raw, ["LwPric","LOW","low"])
    c = pick_col(raw, ["ClsPric","CLOSE","close"])
    vol = pick_col(raw, ["TtlTradgVol","CONTRACTS","NO_OF_CONTRACTS","volume"], required=False)
    oi = pick_col(raw, ["OpnIntrst","OPEN_INT","OPEN_INTEREST","openinterest"], required=False)
    chgoi = pick_col(raw, ["ChngInOpnIntrst","CHG_IN_OI","CHANGE_IN_OI"], required=False)
    d = pd.DataFrame({
        "date": pd.Timestamp(day), "symbol": raw[sym].astype(str).str.strip(),
        "instrument": raw[inst].astype(str).str.upper() if inst else "",
        "expiry": pd.to_datetime(raw[exp], errors="coerce") if exp else pd.NaT,
        "strike": pd.to_numeric(raw[strike], errors="coerce") if strike else float("nan"),
        "option_type": raw[opt].astype(str).str.upper().str.strip() if opt else "",
        "open": pd.to_numeric(raw[o], errors="coerce"), "high": pd.to_numeric(raw[h], errors="coerce"),
        "low": pd.to_numeric(raw[l], errors="coerce"), "close": pd.to_numeric(raw[c], errors="coerce"),
        "volume": pd.to_numeric(raw[vol], errors="coerce") if vol else 0,
        "oi": pd.to_numeric(raw[oi], errors="coerce") if oi else float("nan"),
        "change_oi": pd.to_numeric(raw[chgoi], errors="coerce") if chgoi else float("nan"),
    })
    # Normalize legacy instrument labels and UDiFF types.
    d["kind"] = d["instrument"].map(lambda x: "option" if ("OPT" in x or x in {"STO","IDO"}) else ("future" if ("FUT" in x or x in {"STF","IDF"}) else "other"))
    d = d.dropna(subset=["open","high","low","close"])
    return d

def load_fno(start: str, end: str, symbols: set[str] | None = None) -> tuple[pd.DataFrame, dict]:
    start_d, end_d = pd.Timestamp(start).date(), pd.Timestamp(end).date()
    cache = CACHE / "fno"
    cache.mkdir(parents=True, exist_ok=True)
    frames, failures, found = [], [], 0
    day = start_d
    while day <= end_d:
        if day.weekday() < 5:
            p = cache / f"{day:%Y%m%d}.csv.gz"
            try:
                if p.exists():
                    d = pd.read_csv(p, compression="gzip", low_memory=False)
                    d["date"] = pd.to_datetime(d["date"])
                else:
                    blob = request_bytes(fno_url(day))
                    d = parse_fno_bytes(blob, day)
                    d.to_csv(p, index=False, compression="gzip")
                if symbols:
                    d = d[d.symbol.isin(symbols)]
                frames.append(d); found += 1
            except FileNotFoundError:
                # weekends/holidays have no bhavcopy; do not treat as a failed trading session
                pass
            except Exception as e:
                failures.append({"date": str(day), "error": str(e)[:300]})
            time.sleep(0.12)
        day += timedelta(days=1)
    if not frames:
        raise RuntimeError(f"No NSE F&O bhavcopy data loaded for {start} to {end}.")
    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    quality = {"requested_start": start, "requested_end": end, "fno_sessions_loaded": found,
               "failed_or_unparseable_dates": failures, "fno_rows": int(len(df))}
    return df, quality

def add_signals(eq: pd.DataFrame, fno: pd.DataFrame) -> pd.DataFrame:
    d = eq.copy().sort_values(["symbol","date"])
    g = d.groupby("symbol", group_keys=False)
    d["ema20"] = g.close.transform(lambda x: x.ewm(span=20, min_periods=20, adjust=False).mean())
    d["ema50"] = g.close.transform(lambda x: x.ewm(span=50, min_periods=50, adjust=False).mean())
    d["prior20_high"] = g.high.transform(lambda x: x.shift(1).rolling(20, min_periods=20).max())
    d["vol_sma20"] = g.volume.transform(lambda x: x.shift(1).rolling(20, min_periods=20).mean())
    d["ret1"] = g.close.pct_change()
    d["ret3"] = g.close.pct_change(3)
    # Yahoo's free chart endpoint supplies Nifty 50 only as a benchmark; if unavailable, RS is explicitly null.
    try:
        p1 = int(pd.Timestamp(d.date.min()).timestamp())
        p2 = int((pd.Timestamp(d.date.max()) + pd.Timedelta(days=1)).timestamp())
        u = f"https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI?period1={p1}&period2={p2}&interval=1d"
        js = requests.get(u, headers={"User-Agent":"Mozilla/5.0"}, timeout=30).json()["chart"]["result"][0]
        nd = pd.DataFrame({"date": pd.to_datetime(js["timestamp"], unit="s").normalize(),
                           "nifty_close": js["indicators"]["quote"][0]["close"]})
        nd = nd.dropna().sort_values("date")
        nd["nifty_ret5"] = nd.nifty_close.pct_change(5)
        d = d.merge(nd[["date","nifty_ret5"]], on="date", how="left")
        d["stock_ret5"] = d.groupby("symbol").close.pct_change(5)
        d["relative_strength5"] = d.stock_ret5 - d.nifty_ret5
    except Exception as e:
        print(f"WARN Nifty benchmark unavailable; RS omitted: {e}")
        d["relative_strength5"] = float("nan")
    # Select most liquid near-expiry stock futures, one row per symbol/date.
    fut = fno[fno.kind.eq("future")].copy()
    if not fut.empty:
        fut["expiry"] = pd.to_datetime(fut.expiry, errors="coerce")
        fut = fut[fut.expiry >= fut.date]
        fut = fut.sort_values(["date","symbol","expiry"])
        fut = fut.drop_duplicates(["date","symbol"], keep="first")
        fut["fut_ret1"] = fut.groupby("symbol").close.pct_change()
        fut["fut_oi_chg3"] = fut.groupby("symbol").oi.pct_change(3)
        d = d.merge(fut[["date","symbol","close","oi","fut_ret1","fut_oi_chg3"]].rename(
            columns={"close":"fut_close","oi":"fut_oi"}), on=["date","symbol"], how="left")
    else:
        d["fut_close"] = float("nan"); d["fut_oi"] = float("nan"); d["fut_ret1"] = float("nan"); d["fut_oi_chg3"] = float("nan")
    d["long_buildup"] = (d.fut_ret1 > 0) & (d.fut_oi_chg3 > 0)
    d["short_covering"] = (d.fut_ret1 > 0) & (d.fut_oi_chg3 < 0)
    d["breakout"] = (d.close > d.ema20) & (d.close > d.ema50) & (d.close > d.prior20_high) & (d.volume >= 2*d.vol_sma20) & (d.ret1 >= .02)
    rolling_high3 = d.groupby("symbol").high.transform(lambda x: x.rolling(3, min_periods=3).max())
    rolling_low3 = d.groupby("symbol").low.transform(lambda x: x.rolling(3, min_periods=3).min())
    prior_high = d.groupby("symbol").high.shift(1)
    prior_volume = d.groupby("symbol").volume.shift(1)
    d["compression"] = ((rolling_high3 / rolling_low3 - 1) <= .04) & \
                       (d.fut_oi_chg3 >= .04) & (d.close > d.ema20) & (d.volume > d.vol_sma20)
    d["gap_go"] = (d.open > prior_high * 1.005) & (d.close > d.open) & \
                  (d.close > d.ema20) & (d.volume >= 1.5 * prior_volume)
    d["signal"] = d[["breakout","compression","gap_go"]].fillna(False).any(axis=1)
    d["signal_type"] = d.apply(lambda r: "+".join(k for k in ("breakout","compression","gap_go") if pd.notna(r.get(k, False)) and bool(r.get(k, False))), axis=1)
    return d

def choose_option(fno: pd.DataFrame, symbol: str, signal_date: pd.Timestamp, entry_date: pd.Timestamp, spot: float) -> pd.Series | None:
    # Select a call with expiry at least 4 calendar days away and strike closest to spot (ATM proxy).
    x = fno[(fno.symbol == symbol) & fno.kind.eq("option") & fno.option_type.isin(["CE","CALL"])].copy()
    if x.empty: return None
    x["expiry"] = pd.to_datetime(x.expiry, errors="coerce")
    x = x[(x.date == entry_date) & (x.expiry >= entry_date + pd.Timedelta(days=4)) & x.strike.notna() & (x.open > 0)]
    if x.empty: return None
    x["atm_gap"] = (x.strike - spot).abs()
    x = x.sort_values(["atm_gap","expiry","volume"], ascending=[True,True,False])
    return x.iloc[0]

def backtest(signals: pd.DataFrame, fno: pd.DataFrame, start: str, end: str) -> tuple[pd.DataFrame, dict]:
    sessions = sorted(pd.to_datetime(signals.date.unique()))
    next_session = {sessions[i]: sessions[i+1] for i in range(len(sessions)-1)}
    sig = signals[(signals.date >= pd.Timestamp(start)) & (signals.date <= pd.Timestamp(end)) & signals.signal].copy()
    sig = sig.sort_values(["date","symbol"])
    fno = fno.copy(); fno["date"] = pd.to_datetime(fno.date).dt.normalize()
    trades = []
    for _, s in sig.iterrows():
        ent = next_session.get(pd.Timestamp(s.date))
        if ent is None: continue
        opt = choose_option(fno, s.symbol, pd.Timestamp(s.date), ent, float(s.close))
        if opt is None: continue
        entry = float(opt.open)
        if not math.isfinite(entry) or entry <= 0: continue
        target, stop = entry*(1+TARGET), entry*(1-STOP)
        path = fno[(fno.symbol == s.symbol) & fno.kind.eq("option") & (fno.option_type.isin(["CE","CALL"])) &
                   (fno.expiry == opt.expiry) & (fno.strike == opt.strike) & (fno.date >= ent)].sort_values("date").head(MAX_HOLD)
        exit_price, exit_date, reason = float(path.iloc[0].close), ent, "time_exit"
        for _, bar in path.iterrows():
            # Conservative when both are touched during the same daily candle.
            if float(bar.low) <= stop:
                exit_price, exit_date, reason = stop, pd.Timestamp(bar.date), "stop"
                break
            if float(bar.high) >= target:
                exit_price, exit_date, reason = target, pd.Timestamp(bar.date), "target"
                break
            exit_price, exit_date = float(bar.close), pd.Timestamp(bar.date)
        gross = exit_price / entry - 1
        net = (exit_price*(1-COST_PER_SIDE))/(entry*(1+COST_PER_SIDE)) - 1
        trades.append({"signal_date":pd.Timestamp(s.date).date().isoformat(),"entry_date":ent.date().isoformat(),
                       "exit_date":exit_date.date().isoformat(),"symbol":s.symbol,"signal_type":s.signal_type,
                       "spot_close_signal":float(s.close),"option_expiry":pd.Timestamp(opt.expiry).date().isoformat(),
                       "option_type":"CE","strike":float(opt.strike),"entry_option_open":entry,
                       "exit_option_price":exit_price,"exit_reason":reason,"gross_return_pct":round(gross*100,3),
                       "net_return_pct":round(net*100,3),"futures_setup":"long_buildup" if bool(s.long_buildup) else ("short_covering" if bool(s.short_covering) else "unconfirmed")})
    t = pd.DataFrame(trades)
    if t.empty:
        metrics = {"trades":0,"win_rate_pct":None,"mean_net_return_pct":None,"profit_factor":None,"max_drawdown_pct":None,
                   "warning":"No option contracts matched the signal/entry dates. Do not infer zero-return performance."}
        return t, metrics
    r = t.net_return_pct/100
    equity = (1+r).cumprod()
    dd = equity/equity.cummax()-1
    wins = r[r>0].sum(); losses = -r[r<0].sum()
    metrics = {"trades":int(len(t)),"win_rate_pct":round(float((r>0).mean()*100),2),
               "mean_net_return_pct":round(float(r.mean()*100),3),
               "median_net_return_pct":round(float(r.median()*100),3),
               "profit_factor":round(float(wins/losses),3) if losses else None,
               "max_drawdown_pct":round(float(dd.min()*100),3),
               "target_pct":TARGET*100,"stop_pct":STOP*100,"max_hold_sessions":MAX_HOLD,
               "cost_assumption":"0.5% premium friction each side; not a complete broker-specific fee schedule",
               "entry":"next session option open; daily OHLC only; no intraday fill simulation"}
    t["equity_multiple"] = equity.round(6)
    t["drawdown_pct"] = (dd*100).round(3)
    return t, metrics

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["scan","backtest"], default="scan")
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--lookback", type=int, default=300)
    ap.add_argument("--out", default="output")
    args = ap.parse_args()
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    end = args.end or date.today().isoformat()
    start = args.start if args.mode == "backtest" else (pd.Timestamp(end)-pd.Timedelta(days=max(500,args.lookback*2))).date().isoformat()
    quality = {"run_utc":datetime.utcnow().isoformat(timespec="seconds")+"Z","mode":args.mode,
               "start":start,"end":end,"sources":{"equity":"TejHQ free NSE bhavcopy-derived EOD parquet","derivatives":"NSE official daily F&O bhavcopy"}}
    eq = read_equity(start,end)
    symbols = set(eq.symbol.astype(str).unique())
    fno, fq = load_fno(start,end,symbols)
    quality.update(fq)
    sig = add_signals(eq,fno)
    quality["equity_rows"] = int(len(eq)); quality["equity_symbols"] = int(eq.symbol.nunique())
    quality["signal_rows"] = int(sig.signal.fillna(False).sum())
    quality["coverage_note"] = "Missing/unparseable dates are listed; daily EOD data cannot validate intraday rules."
    if args.mode == "scan":
        latest = sig[sig.date == sig.date.max()].copy()
        latest = latest[latest.signal.fillna(False)].sort_values(["breakout","compression","gap_go"], ascending=False)
        cols = [c for c in ["date","symbol","close","ret1","ema20","ema50","relative_strength5","volume","vol_sma20","fut_ret1","fut_oi_chg3","long_buildup","short_covering","breakout","compression","gap_go","signal_type"] if c in latest]
        latest[cols].to_csv(out/"latest_scan.csv",index=False)
        quality["latest_session"] = str(sig.date.max().date())
        quality["latest_candidates"] = int(len(latest))
    else:
        trades, metrics = backtest(sig,fno,start,end)
        trades.to_csv(out/"backtest_trades.csv",index=False)
        (out/"metrics.json").write_text(json.dumps(metrics,indent=2,default=str))
        quality["backtest_metrics"] = metrics
    (out/"data_quality.json").write_text(json.dumps(quality,indent=2,default=str))
    print(json.dumps(quality,indent=2,default=str))
    if args.mode == "backtest" and quality.get("fno_sessions_loaded",0) < 50:
        raise SystemExit("FAIL-CLOSED: fewer than 50 F&O sessions loaded; result is not considered a valid backtest.")

if __name__ == "__main__":
    main()
