#!/usr/bin/env python3
"""
Builds data/market.json for the NDX / S&P 500 / Dow terminal.

Keyless: prices come from Yahoo Finance via the `yfinance` package (run from a
GitHub Action, so browser CORS rules do not apply). Index membership comes from
Wikipedia. If Yahoo fails for the three index ETFs, it tries Stooq (keyless) and
then FMP (only if FMP_API_KEY is set as a repo secret).

Run locally:  python scripts/update_data.py
"""
import io
import json
import os
import re
import sys
import time
import datetime as dt

import numpy as np
import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("NDX_OUT", os.path.join(HERE, "..", "data", "market.json"))

# ticker -> index config.  `eq` is the equal-weight ETF used for the
# "cap-weight vs equal-weight" reading (None = not applicable).
INDICES = {
    "QQQM": dict(label="Nasdaq-100", eq="QQQE",
                 wiki="https://en.wikipedia.org/wiki/Nasdaq-100", min_rows=90, max_rows=110),
    "SPY":  dict(label="S&P 500", eq="RSP",
                 wiki="https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", min_rows=480, max_rows=520),
    "DIA":  dict(label="Dow 30", eq=None,
                 wiki="https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average", min_rows=28, max_rows=32),
}
HISTORY_DAYS = 260
UA = {"User-Agent": "Mozilla/5.0 (compatible; ndx-terminal-bot; +https://github.com)"}


# ---------------------------------------------------------------- indicators
def sma(c, n):
    out = np.full(len(c), np.nan)
    if len(c) >= n:
        cs = np.cumsum(np.insert(c, 0, 0.0))
        out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def rsi(c, n=14):
    out = np.full(len(c), np.nan)
    if len(c) <= n:
        return out
    d = np.diff(c)
    g = np.where(d > 0, d, 0.0)
    l = np.where(d < 0, -d, 0.0)
    ag, al = g[:n].mean(), l[:n].mean()
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n, len(d)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + l[i]) / n
        out[i + 1] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def ultimate_oscillator(h, l, c):
    """Larry Williams UO, periods 7/14/28 with weights 4/2/1."""
    n = len(c)
    out = np.full(n, np.nan)
    if n < 29:
        return out
    pc = np.roll(c, 1)
    lo, hi = np.minimum(l, pc), np.maximum(h, pc)
    bp, tr = c - lo, hi - lo
    bp[0] = tr[0] = 0.0
    cb, ct = np.cumsum(bp), np.cumsum(tr)
    idx = np.arange(28, n)

    def avg(k):
        sb, st = cb[idx] - cb[idx - k], ct[idx] - ct[idx - k]
        return np.divide(sb, st, out=np.zeros(len(idx)), where=st != 0)

    out[idx] = 100 * (4 * avg(7) + 2 * avg(14) + avg(28)) / 7
    return out


def r2(x):
    return None if x is None or not np.isfinite(x) else round(float(x), 2)


# ---------------------------------------------------------------- data fetch
def fetch_prices(tickers, period="2y", chunk=80):
    """Return {ticker: DataFrame[High, Low, Close]} using yfinance (keyless)."""
    import yfinance as yf
    out = {}
    tickers = list(tickers)
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        for attempt in range(3):
            try:
                df = yf.download(part, period=period, interval="1d", group_by="ticker",
                                 auto_adjust=False, progress=False, threads=True)
                break
            except Exception as e:  # noqa
                print(f"  yfinance chunk {i} attempt {attempt + 1} failed: {e}", file=sys.stderr)
                time.sleep(5 * (attempt + 1))
                df = None
        if df is None or df.empty:
            continue
        for t in part:
            try:
                cols = ["High", "Low", "Close"] + (["Volume"] if "Volume" in df[t].columns else [])
                sub = df[t][cols].dropna(subset=["High", "Low", "Close"])
                if len(sub) > 30:
                    out[t] = sub
            except Exception:
                pass
        time.sleep(1.5)
    return out


def fallback_etf(sym):
    """Keyless Stooq, then FMP if a key is configured. Returns DataFrame or None."""
    try:
        r = requests.get(f"https://stooq.com/q/d/l/?s={sym.lower()}.us&i=d", headers=UA, timeout=30)
        df = pd.read_csv(io.StringIO(r.text))
        df = df.rename(columns=str.title).set_index("Date")
        df.index = pd.to_datetime(df.index)
        df = df[[c for c in ("High", "Low", "Close", "Volume") if c in df.columns]].dropna(subset=["High", "Low", "Close"]).tail(600)
        if len(df) > 100:
            print(f"  {sym}: using Stooq fallback")
            return df
    except Exception as e:  # noqa
        print(f"  {sym}: Stooq fallback failed: {e}", file=sys.stderr)
    key = os.environ.get("FMP_API_KEY")
    if key:
        try:
            r = requests.get("https://financialmodelingprep.com/stable/historical-price-eod/full",
                             params={"symbol": sym, "apikey": key}, timeout=30)
            data = r.json()
            data = data if isinstance(data, list) else data.get("historical", [])
            df = pd.DataFrame(data).rename(columns=str.title)
            df["Date"] = pd.to_datetime(df["Date"])
            df = df.set_index("Date").sort_index()
            df = df[[c for c in ("High", "Low", "Close", "Volume") if c in df.columns]].dropna(subset=["High", "Low", "Close"]).tail(600)
            if len(df) > 100:
                print(f"  {sym}: using FMP fallback")
                return df
        except Exception as e:  # noqa
            print(f"  {sym}: FMP fallback failed: {e}", file=sys.stderr)
    return None


def fetch_market_caps(tickers, workers=8):
    """Keyless market caps via yfinance. {ticker: cap}. Fail-soft: missing tickers are just absent."""
    import yfinance as yf
    from concurrent.futures import ThreadPoolExecutor

    def one(t):
        try:
            v = yf.Ticker(t).fast_info["market_cap"]
            return t, float(v) if v else None
        except Exception:
            return t, None

    with ThreadPoolExecutor(workers) as ex:
        return {t: v for t, v in ex.map(one, tickers) if v}


def concentration(sym, cons, prices):
    """Top-10 / top-3 weight. Nasdaq-100: approximate (market-cap share; the real index caps some weights).
    Dow: price-weighted, so weight = price share. S&P 500: skipped (500 lookups is too fragile)."""
    tickers = [t for t, _, _ in cons]
    if not tickers:
        return {}
    if sym == "DIA":
        w = {t: float(prices[t]["Close"].iloc[-1]) for t in tickers if t in prices}
        approx = False
    elif sym == "QQQM":
        w = fetch_market_caps(tickers)
        approx = True
    else:
        return {}
    if len(w) < 0.8 * len(tickers):
        return {}
    tot = sum(w.values())
    top = sorted(w.items(), key=lambda kv: kv[1], reverse=True)
    return dict(top10=r2(100 * sum(v for _, v in top[:10]) / tot), top3=r2(100 * sum(v for _, v in top[:3]) / tot),
                topNames=[t for t, _ in top[:5]], topApprox=approx)


# ---- historical "what happened after past signals" (price-only) ----
# Keep these in step with PRESETS in index.html: (oversold trigger, overbought trigger) on the Ultimate Oscillator.
PRESET_RULES = {"mild": (30, 70), "mod": (25, 75), "deep": (20, 80)}
HORIZONS = (21, 63, 126)  # ~1, 3, 6 months of trading days
STATS_SRC = {"QQQM": "QQQ", "SPY": "SPY", "DIA": "DIA"}  # longer-history twins


def _fwd(c, idxs):
    out = {}
    for h in HORIZONS:
        r = np.array([c[i + h] / c[i] - 1 for i in idxs if i + h < len(c)])
        out[str(h)] = dict(n=int(len(r)), med=r2(100 * np.median(r)) if len(r) else None,
                           pos=r2(100 * np.mean(r > 0)) if len(r) else None,
                           worst=r2(100 * r.min()) if len(r) else None)
    return out


def _episodes(mask, gap=21):
    idxs, last = [], -10 ** 9
    for i in np.where(mask)[0]:
        if i - last > gap:
            idxs.append(int(i))
        last = i
    return idxs


def signal_stats(df):
    h, l, c = (df[k].to_numpy(float) for k in ("High", "Low", "Close"))
    uo = ultimate_oscillator(h, l, c)
    valid = np.isfinite(uo)
    base = _fwd(c, list(np.where(valid)[0]))
    rules = {}
    for k, (o, b) in PRESET_RULES.items():
        eo, eb = _episodes(valid & (uo <= o)), _episodes(valid & (uo >= b))
        rules[k] = dict(os=dict(trig=o, episodes=len(eo), fwd=_fwd(c, eo)),
                        ob=dict(trig=b, episodes=len(eb), fwd=_fwd(c, eb)))
    return dict(**{"from": df.index[0].strftime("%Y-%m-%d"), "to": df.index[-1].strftime("%Y-%m-%d")},
                years=round(len(c) / 252, 1), baseline=base, rules=rules)


TICKER_RE = re.compile(r"^[A-Z][A-Z0-9]{0,5}(-[A-Z0-9]{1,2})?$")


def clean_ticker(v):
    """'NASDAQ: AAPL' -> 'AAPL', 'BRK.B' -> 'BRK-B', footnote marks removed. None if it is not a ticker."""
    s = re.sub(r"\[.*?\]", "", str(v)).strip().upper()
    if ":" in s:
        s = s.split(":")[-1].strip()
    s = s.split()[0] if s.split() else ""
    s = s.replace(".", "-")
    return s if TICKER_RE.match(s) else None


def wiki_constituents(cfg):
    """[(ticker, name, sector)] from the index's Wikipedia page. Lenient about table shape."""
    html = requests.get(cfg["wiki"], headers=UA, timeout=30).text
    for t in pd.read_html(io.StringIO(html)):
        t.columns = [" ".join(map(str, c)).strip() if isinstance(c, tuple) else str(c).strip() for c in t.columns]
        tcol = next((c for c in t.columns if any(k in c.lower() for k in ("ticker", "symbol"))), None)
        if tcol is None:
            continue
        ncol = next((c for c in t.columns if any(k in c.lower() for k in ("company", "security", "name"))), None)
        scol = next((c for c in t.columns if "sector" in c.lower()), None) or \
            next((c for c in t.columns if "industry" in c.lower()), None)
        rows, seen = [], set()
        for _, r in t.iterrows():
            tk = clean_ticker(r[tcol])
            if tk and tk not in seen:
                seen.add(tk)
                rows.append((tk, str(r[ncol]).strip() if ncol else "", str(r[scol]).strip() if scol else "Other"))
        if cfg["min_rows"] <= len(rows) <= cfg["max_rows"]:
            return rows
    raise ValueError("no suitable constituents table found")


def nasdaq_api_ndx():
    """Nasdaq's own Nasdaq-100 list (unofficial endpoint). [(ticker, name, sector)]"""
    r = requests.get("https://api.nasdaq.com/api/quote/list-type/nasdaq100",
                     headers={**UA, "Accept": "application/json"}, timeout=30)
    j = r.json()["data"]
    rows = (j.get("data") or j).get("rows", [])
    out = [(clean_ticker(x["symbol"]), x.get("companyName", ""), "Other") for x in rows if clean_ticker(x.get("symbol", ""))]
    if not 85 <= len(out) <= 125:
        raise ValueError(f"unexpected row count {len(out)}")
    return out


# Last-resort lists (only used if every live source fails). May be slightly out of date; any ticker that no
# longer trades is simply skipped.
FALLBACK = {
    "DIA": "AAPL AMGN AMZN AXP BA CAT CRM CSCO CVX DIS GS HD HON IBM JNJ JPM KO MCD MMM MRK MSFT NKE NVDA PG SHW TRV UNH V VZ WMT",
    "QQQM": ("AAPL ABNB ADBE ADI ADP ADSK AEP AMAT AMD AMGN AMZN APP ARM ASML AVGO AXON AZN BIIB BKNG BKR CCEP CDNS CEG CHTR "
             "CMCSA COST CPRT CRWD CSCO CSGP CSX CTAS CTSH DASH DDOG DXCM EA EXC FANG FAST FTNT GEHC GFS GILD GOOG GOOGL HON "
             "IDXX INTC INTU ISRG KDP KHC KLAC LIN LRCX LULU MAR MCHP MDLZ MELI META MNST MRVL MSFT MSTR MU NFLX NVDA NXPI "
             "ODFL ON ORLY PANW PAYX PCAR PDD PEP PLTR PYPL QCOM REGN ROP ROST SBUX SNPS TEAM TMUS TRI TSLA TTD TTWO TXN "
             "VRSK VRTX WBD WDAY XEL ZS"),
}

# Built-in sector labels, used only when the live list has none (so sector views still work offline).
_SEC = {"IT": "Information Technology", "HC": "Health Care", "CD": "Consumer Discretionary", "CS": "Consumer Staples",
        "FN": "Financials", "IN": "Industrials", "EN": "Energy", "MT": "Materials", "UT": "Utilities",
        "CM": "Communication Services", "RE": "Real Estate"}
_SECMAP = {}
for _code, _tk in {
    "IT": "AAPL ADBE ADI ADSK AMAT AMD APP ARM ASML AVGO CDNS CRM CRWD CSCO CTSH DDOG FTNT GFS IBM INTC INTU KLAC LRCX MCHP MRVL MSFT MSTR MU NVDA NXPI ON PANW PLTR QCOM ROP SNPS TEAM TXN WDAY ZS",
    "HC": "AMGN AZN BIIB DXCM GEHC GILD IDXX ISRG JNJ MRK REGN UNH VRTX",
    "CD": "ABNB AMZN BKNG DASH HD LULU MAR MCD MELI NKE ORLY PDD ROST SBUX TSLA",
    "CS": "CCEP COST KDP KHC KO MDLZ MNST PEP PG WMT",
    "FN": "AXP GS JPM PYPL TRV V",
    "IN": "ADP AXON BA CAT CPRT CSX CTAS FAST HON MMM ODFL PAYX PCAR TRI VRSK",
    "EN": "BKR CVX FANG", "MT": "LIN SHW", "UT": "AEP CEG EXC XEL", "RE": "CSGP",
    "CM": "CHTR CMCSA DIS EA GOOG GOOGL META NFLX TMUS TTD TTWO VZ WBD",
}.items():
    for _t in _tk.split():
        _SECMAP[_t] = _SEC[_code]


def fill_sectors(rows):
    """Replace blank/Other sectors with the built-in label when we know the ticker."""
    out = []
    for tk, name, sec in rows:
        if (not sec or sec in ("Other", "nan")) and tk in _SECMAP:
            sec = _SECMAP[tk]
        out.append((tk, name, sec))
    return out


def get_constituents(sym, cfg, prev):
    return fill_sectors(_get_constituents(sym, cfg, prev))


def _get_constituents(sym, cfg, prev):
    """Try Wikipedia, then (Nasdaq-100 only) Nasdaq's API, then the previous run, then the built-in list."""
    attempts = [("Wikipedia", lambda: wiki_constituents(cfg))]
    if sym == "QQQM":
        attempts.append(("Nasdaq API", nasdaq_api_ndx))
    for label, fn in attempts:
        try:
            rows = fn()
            print(f"{sym}: {len(rows)} constituents from {label}")
            return rows
        except Exception as e:  # noqa
            print(f"{sym}: {label} failed ({e})", file=sys.stderr)
    old = prev.get("indices", {}).get(sym, {}).get("watch", [])
    if old:
        print(f"{sym}: reusing {len(old)} tickers from the previous run", file=sys.stderr)
        return [(w["ticker"], w.get("name", ""), w.get("sector", "Other")) for w in old]
    if sym in FALLBACK:
        rows = [(t, "", "Other") for t in FALLBACK[sym].split()]
        print(f"{sym}: using built-in fallback list ({len(rows)} tickers)", file=sys.stderr)
        return rows
    print(f"{sym}: no constituent list available; stock screener and breadth will be empty", file=sys.stderr)
    return []


# ---------------------------------------------------------------- builders
def series_for(df):
    h, l, c = (df[k].to_numpy(float) for k in ("High", "Low", "Close"))
    return dict(c=c, uo=ultimate_oscillator(h, l, c), rsi=rsi(c), s50=sma(c, 50), s200=sma(c, 200))


def internals(df, cons, prices, days):
    """Breadth history (% of members above their 50/200-day average), 52-week highs/lows, ETF volume ratio.
    Uses the same price downloads as everything else. Members are today's list, so older values are approximate."""
    dates = df.index[-days:]
    a50, a200, nh, nl = {}, {}, {}, {}
    for tk, _, _ in cons:
        p = prices.get(tk)
        if p is None:
            continue
        c = p["Close"].astype(float)
        m50, m200 = c.rolling(50), c.rolling(200)
        ok50, ok200 = m50.count() >= 50, m200.count() >= 200
        a50[tk] = (c > m50.mean()).astype(float).where(ok50)
        a200[tk] = (c > m200.mean()).astype(float).where(ok200)
        okh = c.rolling(252, min_periods=200).count() >= 200
        nh[tk] = (c >= c.rolling(252, min_periods=200).max()).astype(float).where(okh)
        nl[tk] = (c <= c.rolling(252, min_periods=200).min()).astype(float).where(okh)
    out = {}
    need = max(1, 0.6 * len(cons))
    for name, d in (("b50", a50), ("b200", a200), ("nh", nh), ("nl", nl)):
        if not d:
            continue
        m = pd.DataFrame(d).reindex(dates)
        pct = m.mean(axis=1) * 100
        pct[m.count(axis=1) < need] = np.nan
        out[name] = pct
    vol = None
    if "Volume" in df.columns:
        v = df["Volume"].astype(float)
        if (v.tail(60) > 0).sum() >= 50:
            vol = v / v.rolling(50, min_periods=30).mean().shift(1)
    return out, vol


def vix_block(vdf):
    """VIX level, 5-day change and where it sits versus the past year. {} if unavailable."""
    try:
        c = vdf["Close"].astype(float).dropna()
        if len(c) < 60:
            return {}
        last = float(c.iloc[-1])
        yr = c.tail(252)
        return dict(vix=r2(last), vix5=r2(last - float(c.iloc[-6])), vixpct=round(float((yr < last).mean() * 100)),
                    vixdate=c.index[-1].strftime("%Y-%m-%d"))
    except Exception as e:  # noqa
        print(f"  VIX skipped ({e})", file=sys.stderr)
        return {}


def build_index(sym, df, cons, prices, eq_df):
    s = series_for(df)
    dates = [d.strftime("%Y-%m-%d") for d in df.index]
    n = len(df)
    start = max(0, n - HISTORY_DAYS)
    history = [dict(d=dates[i], c=r2(s["c"][i]), uo=r2(s["uo"][i]), rsi=r2(s["rsi"][i]),
                    s50=r2(s["s50"][i]), s200=r2(s["s200"][i])) for i in range(start, n)]
    last = n - 1
    peak = float(np.max(s["c"][-252:]))
    try:
        inter, vol = internals(df, cons, prices, len(history))
        for i, row in enumerate(history):
            d = df.index[start + i]
            for k, ser in inter.items():
                if d in ser.index and np.isfinite(ser.loc[d]):
                    row[k] = round(float(ser.loc[d]), 1)
            if vol is not None and np.isfinite(vol.iloc[start + i]):
                row["vx"] = round(float(vol.iloc[start + i]), 2)
    except Exception as e:  # noqa
        print(f"  {sym}: market internals skipped ({e})", file=sys.stderr)

    watch, a50, a200, n50, n200 = [], 0, 0, 0, 0
    for tk, name, sector in cons:
        p = prices.get(tk)
        if p is None:
            continue
        cs = series_for(p)
        c = cs["c"][-1]
        v200 = (c / cs["s200"][-1] - 1) * 100 if np.isfinite(cs["s200"][-1]) else None
        if np.isfinite(cs["s50"][-1]):
            n50 += 1
            a50 += c > cs["s50"][-1]
        if np.isfinite(cs["s200"][-1]):
            n200 += 1
            a200 += c > cs["s200"][-1]
        watch.append(dict(ticker=tk, name=name, sector=sector, uo=r2(cs["uo"][-1]),
                          rsi=r2(cs["rsi"][-1]), vs200=r2(v200)))
    need = max(1, 0.6 * len(cons))
    br50 = round(100 * a50 / n50, 1) if n50 and n50 >= need else None
    br200 = round(100 * a200 / n200, 1) if n200 and n200 >= need else None

    spread = None
    if eq_df is not None and len(eq_df) > 22 and n > 22:
        cap_ret = df["Close"].iloc[-1] / df["Close"].iloc[-22] - 1
        eq_ret = eq_df["Close"].iloc[-1] / eq_df["Close"].iloc[-22] - 1
        spread = r2((cap_ret - eq_ret) * 100)

    index = dict(date=dates[last], price=r2(s["c"][last]), peak=r2(peak), uo=r2(s["uo"][last]),
                 rsi=r2(s["rsi"][last]), sma50=r2(s["s50"][last]), sma200=r2(s["s200"][last]),
                 br50=br50, br200=br200, spread=spread)
    return dict(index=index, history=history, watch=watch)


def main():
    prev = {}
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT))
        except Exception:
            prev = {}

    cons = {sym: get_constituents(sym, cfg, prev) for sym, cfg in INDICES.items()}

    etfs = set(INDICES) | {c["eq"] for c in INDICES.values() if c["eq"]}
    everything = sorted(etfs | {t for lst in cons.values() for t, _, _ in lst} | {"^VIX"})
    print(f"Downloading {len(everything)} tickers…")
    prices = fetch_prices(everything)

    for sym in etfs:
        if sym not in prices:
            fb = fallback_etf(sym)
            if fb is not None:
                prices[sym] = fb

    result = {}
    for sym, cfg in INDICES.items():
        if sym not in prices:
            print(f"{sym}: no price data, keeping previous", file=sys.stderr)
            if sym in prev.get("indices", {}):
                result[sym] = prev["indices"][sym]
            continue
        try:
            result[sym] = build_index(sym, prices[sym], cons[sym], prices, prices.get(cfg["eq"]) if cfg["eq"] else None)
        except Exception as e:  # noqa
            print(f"{sym}: build failed ({e}); keeping previous", file=sys.stderr)
            if sym in prev.get("indices", {}):
                result[sym] = prev["indices"][sym]
            continue
        if "^VIX" in prices:
            result[sym]["index"].update(vix_block(prices["^VIX"]))
        else:
            print(f"{sym}: no VIX data this run", file=sys.stderr)
        try:
            result[sym]["index"].update(concentration(sym, cons[sym], prices))
        except Exception as e:  # noqa
            print(f"{sym}: concentration skipped ({e})", file=sys.stderr)
        print(f"{sym}: as of {result[sym]['index']['date']}, {len(result[sym]['watch'])} stocks, "
              f"breadth50={result[sym]['index']['br50']}")

    fresh = [v for k, v in result.items() if k in prices]
    if not fresh:
        print("No fresh data for any index; leaving file untouched.", file=sys.stderr)
        sys.exit(1)

    newest = max(v["index"]["date"] for v in fresh)
    old_newest = max((v["index"]["date"] for v in prev.get("indices", {}).values()), default="")
    if newest < old_newest:
        print("New data is older than existing data; leaving file untouched.", file=sys.stderr)
        sys.exit(1)

    # Long-history signal stats (3 extra downloads). Reuse the previous stats if this fails.
    stats = prev.get("stats", {})
    try:
        long = fetch_prices(sorted(set(STATS_SRC.values())), period="max")
        for sym, src in STATS_SRC.items():
            if src in long and len(long[src]) > 1000:
                stats[sym] = dict(src=src, **signal_stats(long[src]))
                print(f"{sym}: signal stats from {src}, {stats[sym]['years']} years")
    except Exception as e:  # noqa
        print(f"signal stats skipped ({e})", file=sys.stderr)

    payload = dict(generated=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   source="yfinance (Yahoo Finance) + Wikipedia constituents", indices=result, stats=stats)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"Wrote {OUT} ({os.path.getsize(OUT) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
