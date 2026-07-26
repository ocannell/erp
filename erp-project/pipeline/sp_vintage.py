"""Parse point-in-time S&P Dow Jones Indices 'S&P 500 EPS estimates' workbooks.

Each archived vintage of the workbook is a *point-in-time* snapshot of the
bottom-up Capital IQ / S&P consensus: quarterly operating EPS, with future
quarters marked as estimates. Reading many vintages lets us rebuild a forward
earnings series with no look-ahead bias.
"""
from __future__ import annotations

import datetime as dt
import gzip
import io
import re
from dataclasses import dataclass, field

import pandas as pd

SHEET = "ESTIMATES&PEs"


def decompress(raw: bytes) -> bytes:
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    return raw


def _to_quarter_end(value) -> dt.date | None:
    """Coerce a workbook row label into a quarter-end date."""
    if isinstance(value, (dt.datetime, pd.Timestamp)):
        d = pd.Timestamp(value).date()
    elif isinstance(value, str):
        m = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", value)
        if m:
            mo, day, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
            try:
                d = dt.date(yr, mo, day)
            except ValueError:
                return None
        else:
            m = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
            if not m:
                return None
            d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    else:
        return None
    # Quarter ends only (Mar/Jun/Sep/Dec, last days of month)
    if d.month in (3, 6, 9, 12) and d.day >= 28:
        return d
    return None


@dataclass
class Vintage:
    """One point-in-time snapshot of the S&P consensus workbook."""

    as_of: dt.date
    close: float | None
    # quarter-end date -> operating EPS for that quarter
    quarters: dict = field(default_factory=dict)
    # quarter-ends that were still estimates at as_of
    estimated: set = field(default_factory=set)

    def forward_eps(self, ref: dt.date | None = None) -> float | None:
        """Sum of the next four quarterly EPS values after ``ref``.

        This is the standard 'forward 12-month EPS' definition: the four
        upcoming quarters of bottom-up consensus as known at ``as_of``.
        """
        ref = ref or self.as_of
        future = sorted(q for q in self.quarters if q > ref)
        if len(future) < 4:
            return None
        vals = [self.quarters[q] for q in future[:4]]
        if any(v is None for v in vals):
            return None
        return float(sum(vals))


def parse_workbook(raw: bytes, fallback_as_of: dt.date | None = None) -> Vintage | None:
    """Extract quarterly operating EPS + estimate flags from a workbook vintage."""
    raw = decompress(raw)
    if raw[:4] not in (b"PK\x03\x04", b"\xd0\xcf\x11\xe0"):
        return None
    engine = "openpyxl" if raw[:4] == b"PK\x03\x04" else "xlrd"
    try:
        df = pd.read_excel(io.BytesIO(raw), sheet_name=SHEET, header=None, engine=engine)
    except Exception:
        return None

    ncol = df.shape[1]

    # --- locate the quarterly table header -------------------------------
    # The block always starts with a "QUARTER" / "END" label in column A,
    # followed by per-share earnings columns. Column *wording* changed over
    # the years ("OPERATING EARNINGS PER SHR" in modern books; plain
    # "EARNINGS PER SHR (ests are bottom up)" in 2013/2014 books), so we key
    # off the stable column-A label instead.
    header_row = None
    for i in range(len(df)):
        a = str(df.iat[i, 0]).strip().upper()
        if a.startswith("QUARTER"):
            row_txt = " ".join(str(x).upper() for x in df.iloc[i].tolist()[: min(ncol, 12)])
            if "EARNINGS" in row_txt:
                header_row = i
                break
    if header_row is None:
        return None

    # --- which column holds bottom-up *quarterly operating* EPS? ---------
    # Build the full stacked header text for each column, then score it.
    # Critical subtlety: the same sheet also carries a "12 MONTH EARNINGS
    # PER SHR / OPERATING" column. Matching "OPERATING" alone silently picks
    # that rolling-annual column and inflates forward EPS ~4x, so any column
    # whose header mentions "12 MONTH" is rejected outright.
    header_block = range(header_row, min(header_row + 7, len(df)))
    op_col = None
    best = -1
    for c in range(1, min(ncol, 12)):
        text = " ".join(
            str(df.iat[r, c]) for r in header_block if not pd.isna(df.iat[r, c])
        ).upper()
        if "12 MONTH" in text or "12 MO" in text:
            continue  # rolling annual column, not a quarter
        if "PER SHR" not in text:
            continue  # not an EPS column (e.g. PRICE, P/E)
        if "TOP DOWN" in text or "AS REPORTED" in text:
            continue  # wrong earnings basis
        score = 0
        if "BOTTOM UP" in text:
            score += 2
        if "OPERATING" in text:
            score += 1
        if score > best:
            best, op_col = score, c
    if op_col is None:
        op_col = 2

    # --- as-of date and index close ---
    as_of = fallback_as_of
    close = None
    for i in range(max(0, header_row - 25), header_row):
        for c in range(min(ncol, 12)):
            label = str(df.iat[i, c])
            low = label.lower()
            # Modern vintages: a "Date" label. Older ones: "Data as of the close of:".
            if re.match(r"^\s*date\b", low) or "as of the close" in low:
                for cc in range(c + 1, min(ncol, 12)):
                    d = df.iat[i, cc]
                    if isinstance(d, (dt.datetime, pd.Timestamp)):
                        as_of = pd.Timestamp(d).date()
                        break
                    if isinstance(d, str):
                        m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", d)
                        if m:
                            try:
                                as_of = dt.date(
                                    int(m.group(3)), int(m.group(1)), int(m.group(2))
                                )
                            except ValueError:
                                pass
                            break
            if "close of" in low:
                for cc in range(c + 1, min(ncol, 12)):
                    v = df.iat[i, cc]
                    if isinstance(v, (int, float)) and not pd.isna(v) and v > 100:
                        close = float(v)
                        break
    # Sanity-check the workbook's self-reported date against the archive
    # capture date. Some vintages carry a typo (e.g. one 2022 book says
    # "1/6/2021"); a snapshot can never predate its own content date, and
    # S&P publishes within ~90 days of a capture.
    if fallback_as_of is not None:
        if as_of is None or not (
            dt.timedelta(days=0) <= (fallback_as_of - as_of) <= dt.timedelta(days=120)
        ):
            as_of = fallback_as_of
    if as_of is None:
        return None

    # --- walk rows: quarter-end label -> operating EPS, tracking EST/ACTUAL ---
    quarters: dict = {}
    estimated: set = set()
    in_estimates = False
    for i in range(header_row + 1, len(df)):
        first = df.iat[i, 0]
        label = str(first).upper()
        if "ESTIMATE" in label and not any(ch.isdigit() for ch in label):
            in_estimates = True
            continue
        if "ACTUAL" in label and not any(ch.isdigit() for ch in label):
            in_estimates = False
            continue
        qe = _to_quarter_end(first)
        if qe is None:
            continue
        val = df.iat[i, op_col] if op_col < ncol else None
        if isinstance(val, str):
            try:
                val = float(val.replace(",", "").strip())
            except ValueError:
                val = None
        if not isinstance(val, (int, float)) or pd.isna(val):
            continue
        val = float(val)
        if not (0 < val < 500):
            continue
        if qe not in quarters:
            quarters[qe] = val
            if in_estimates:
                estimated.add(qe)

    if len(quarters) < 8:
        return None
    return Vintage(as_of=as_of, close=close, quarters=quarters, estimated=estimated)
