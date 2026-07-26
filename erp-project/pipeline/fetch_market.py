#!/usr/bin/env python3
"""Fetch the two market inputs: S&P 500 daily closes and the 10-year Treasury yield.

Treasury yields come from the U.S. Treasury's own daily par yield curve
(the authoritative primary source, 1990-present, and the exact series FRED
republishes as DGS10). Yahoo's ^TNX is kept only as a gap-filler / pre-1990
extension, since it is a derived index quoted in percent.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import pathlib
import sys
import time
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)

TREASURY = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
    "&field_tdr_date_value={year}&page&_format=csv"
)

YAHOO = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
    "?period1=0&period2={end}&interval=1d"
)


def get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def get_retry(url: str, tries: int = 4, timeout: int = 60) -> bytes | None:
    for attempt in range(tries):
        try:
            return get(url, timeout=timeout)
        except Exception as exc:
            if attempt == tries - 1:
                print(f"  ! {url[:70]}...: {exc}", file=sys.stderr)
                return None
            time.sleep(2 * (attempt + 1))
    return None


# --------------------------------------------------------------------------
# 10-year Treasury par yield, from Treasury.gov
# --------------------------------------------------------------------------
def fetch_treasury_10y() -> dict[str, float]:
    """Daily 10-year par yields (percent) keyed by ISO date."""
    out: dict[str, float] = {}
    this_year = dt.date.today().year
    for year in range(1990, this_year + 1):
        body = get_retry(TREASURY.format(year=year))
        if not body:
            continue
        text = body.decode("utf-8-sig", "replace")
        reader = csv.DictReader(io.StringIO(text))
        col = None
        got = 0
        for row in reader:
            if col is None:
                for key in row:
                    if key and key.strip().lower() in ("10 yr", "10yr", "10 year"):
                        col = key
                        break
                if col is None:
                    break
            raw_date, raw_val = row.get("Date"), row.get(col)
            if not raw_date or not raw_val:
                continue
            try:
                mo, day, yr = (int(x) for x in raw_date.split("/"))
                value = float(raw_val)
            except (ValueError, AttributeError):
                continue
            out[dt.date(yr, mo, day).isoformat()] = value
            got += 1
        print(f"  {year}: {got} obs")
    return out


# --------------------------------------------------------------------------
# Yahoo daily series (S&P 500 closes; ^TNX as Treasury fallback)
# --------------------------------------------------------------------------
def fetch_yahoo(symbol: str) -> dict[str, float]:
    end = int(time.time()) + 86_400
    body = get_retry(YAHOO.format(sym=urllib.parse.quote(symbol), end=end), timeout=90)
    if not body:
        return {}
    payload = json.loads(body)
    result = payload["chart"]["result"][0]
    stamps = result["timestamp"]
    closes = result["indicators"]["quote"][0]["close"]
    out: dict[str, float] = {}
    for stamp, close in zip(stamps, closes):
        if close is None:
            continue
        # Yahoo stamps daily bars at the exchange open; convert in UTC-5/-4
        # agnostic fashion by taking the US/Eastern calendar date.
        day = dt.datetime.utcfromtimestamp(stamp) - dt.timedelta(hours=5)
        out[day.date().isoformat()] = float(close)
    # meta carries the freshest regular-market close, which the history array
    # can lag by a session.
    meta = result.get("meta", {})
    price, stamp = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    if price and stamp:
        day = dt.datetime.utcfromtimestamp(stamp) - dt.timedelta(hours=5)
        if day.hour >= 16:  # session closed
            out[day.date().isoformat()] = float(price)
    return out


def main() -> int:
    RAW.mkdir(parents=True, exist_ok=True)

    print("Fetching 10-year Treasury par yields (home.treasury.gov)...")
    tsy = fetch_treasury_10y()
    print(f"  total {len(tsy)} daily observations")

    print("Fetching ^TNX (Yahoo) as gap-filler/pre-1990 extension...")
    tnx = fetch_yahoo("^TNX")
    print(f"  {len(tnx)} observations")

    print("Fetching S&P 500 daily closes (^GSPC)...")
    spx = fetch_yahoo("^GSPC")
    print(f"  {len(spx)} observations")

    if not spx or not (tsy or tnx):
        print("Missing essential market data.", file=sys.stderr)
        return 1

    (RAW / "treasury_10y.json").write_text(json.dumps(tsy, sort_keys=True))
    (RAW / "tnx.json").write_text(json.dumps(tnx, sort_keys=True))
    (RAW / "spx.json").write_text(json.dumps(spx, sort_keys=True))
    print("Saved raw market data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
