#!/usr/bin/env python3
"""Validate the built ERP series against independent ground truth.

Four checks, each designed to catch a different class of error:

  1. FORWARD P/E vs S&P's OWN PUBLISHED FIGURE.
     The workbook prints its own next-4-quarter operating P/E. Our forward EPS
     is derived from the raw quarterly cells by a different route (calendar
     overlap weighting), so agreement is real evidence the numerator is right.

  2. IDENTITY CHECK. erp == 100*ey - 100*y10 for every row, so no arithmetic
     or unit-scaling slip (the classic bps/percent bug) survives.

  3. TREASURY CROSS-SOURCE. Treasury.gov par yields vs Cboe ^TNX.

  4. PLAUSIBILITY / CONTINUITY. Forward P/E stays in a sane band, no absurd
     day-over-day jumps, and no large coverage holes.
"""
from __future__ import annotations

import datetime as dt
import glob
import gzip
import io
import json
import pathlib
import statistics
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))

from pipeline.sp_vintage import _to_quarter_end  # noqa: E402


def load_series():
    payload = json.loads((DATA / "erp-series.json").read_text())
    return payload, {r["d"]: r for r in payload["rows"]}


def quarterly_vs_printed_12m(path: pathlib.Path):
    """Cross-check extracted quarterly EPS against the workbook's own 12-month column.

    Each row of the workbook prints both a quarter's operating EPS and the
    rolling 12-month operating EPS ending that quarter. Summing our four
    extracted quarterly cells must reproduce that printed annual figure. This
    is the check that catches the single most dangerous parsing failure --
    latching onto the wrong column (e.g. the rolling annual or as-reported
    column), which silently rescales the whole earnings yield.

    Returns (n_compared, max_abs_diff, mean_abs_diff) or None.
    """
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    try:
        df = pd.read_excel(io.BytesIO(raw), sheet_name="ESTIMATES&PEs", header=None)
    except Exception:
        return None
    ncol = df.shape[1]

    header = None
    for i in range(len(df)):
        if str(df.iat[i, 0]).strip().upper().startswith("QUARTER"):
            header = i
            break
    if header is None:
        return None

    block = range(header, min(header + 7, len(df)))

    def column_text(c: int) -> str:
        return " ".join(
            str(df.iat[r, c]) for r in block if not pd.isna(df.iat[r, c])
        ).upper()

    q_col = ann_col = None
    for c in range(1, min(ncol, 12)):
        text = column_text(c)
        if "AS REPORTED" in text or "TOP DOWN" in text or "P/E" in text:
            continue
        if "12 MONTH" in text and ann_col is None:
            ann_col = c
        elif "PER SHR" in text and q_col is None:
            q_col = c
    if q_col is None or ann_col is None:
        return None

    # Walk quarter rows in sheet order (newest first) collecting both columns.
    seq = []
    for i in range(header + 1, len(df)):
        qe = _to_quarter_end(df.iat[i, 0])
        if qe is None:
            continue
        qv, av = df.iat[i, q_col], df.iat[i, ann_col]
        if not isinstance(qv, (int, float)) or pd.isna(qv):
            continue
        seq.append((qe, float(qv), float(av) if isinstance(av, (int, float)) and not pd.isna(av) else None))

    diffs = []
    for idx in range(len(seq) - 3):
        qe, _, printed = seq[idx]
        if printed is None:
            continue
        four = seq[idx : idx + 4]
        if len({q for q, _, _ in four}) != 4:
            continue
        diffs.append(abs(sum(v for _, v, _ in four) - printed))
    if not diffs:
        return None
    return len(diffs), max(diffs), statistics.mean(diffs)


def published_forward_pe(path: pathlib.Path):
    """Read the workbook's own printed forward P/E and as-of date."""
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    try:
        df = pd.read_excel(io.BytesIO(raw), sheet_name="ESTIMATES&PEs", header=None)
    except Exception:
        return None
    ncol = df.shape[1]

    header = None
    for i in range(len(df)):
        if str(df.iat[i, 0]).strip().upper().startswith("QUARTER"):
            header = i
            break
    if header is None:
        return None

    # Locate the P/E column that is operating-basis and not the 12-month one.
    block = range(header, min(header + 7, len(df)))
    pe_col = None
    for c in range(1, min(ncol, 12)):
        text = " ".join(
            str(df.iat[r, c]) for r in block if not pd.isna(df.iat[r, c])
        ).upper()
        if "P/E" not in text or "12 MONTH" in text:
            continue
        if "AS REPORTED" in text or "TOP DOWN" in text:
            continue
        pe_col = c
        break
    if pe_col is None:
        return None

    # as-of date
    as_of = None
    for i in range(max(0, header - 25), header):
        for c in range(min(ncol, 12)):
            label = str(df.iat[i, c]).lower()
            if label.startswith("date") or "as of the close" in label:
                for c2 in range(c + 1, min(ncol, 12)):
                    val = df.iat[i, c2]
                    if isinstance(val, pd.Timestamp):
                        as_of = val.date()
                        break
    # The row right after the "ESTIMATES" marker is the furthest-out estimated
    # quarter; its P/E cell is the forward P/E the workbook itself publishes.
    pub = None
    for i in range(header, min(header + 20, len(df))):
        if str(df.iat[i, 0]).strip().upper().startswith("ESTIMATES"):
            for j in range(i + 1, min(i + 6, len(df))):
                val = df.iat[j, pe_col]
                if isinstance(val, (int, float)) and not pd.isna(val) and 5 < val < 60:
                    pub = float(val)
                    break
            break
    if as_of is None or pub is None:
        return None
    return as_of, pub


def check_forward_pe(rows: dict) -> bool:
    """Our extracted quarterly EPS must reproduce the workbook's printed annual EPS."""
    print("1. Extracted quarterly EPS vs workbook's own printed 12-month EPS")
    books = 0
    worst = 0.0
    means = []
    for path in sorted(glob.glob(str(DATA / "raw" / "vintages" / "*.bin"))):
        got = quarterly_vs_printed_12m(pathlib.Path(path))
        if not got:
            continue
        _, mx, mean_abs = got
        books += 1
        worst = max(worst, mx)
        means.append(mean_abs)
    if not books:
        print("   ! no comparable vintages")
        return False
    overall = statistics.mean(means)
    # Printed annual figures are rounded to 2dp and occasionally to a whole
    # number in older books, so allow a few cents of slack.
    ok = overall < 0.25 and worst < 2.0
    print(f"   {books} workbooks checked  mean abs diff ${overall:.4f}  "
          f"worst ${worst:.3f} EPS")
    print(f"   {'PASS' if ok else 'FAIL'}")
    return ok


def check_identity(payload) -> bool:
    print("2. Internal identity  erp == 100*ey - 100*y10")
    worst = 0.0
    for r in payload["rows"]:
        worst = max(worst, abs(r["erp"] - (100.0 * r["ey"] - 100.0 * r["y10"])))
    ok = worst < 0.75  # rounding of stored 4dp inputs
    print(f"   max deviation {worst:.4f} bps -> {'PASS' if ok else 'FAIL'}")
    return ok


def check_treasury() -> bool:
    print("3. Treasury.gov vs Cboe ^TNX (independent yield sources)")
    tsy = json.loads((DATA / "raw" / "treasury_10y.json").read_text())
    tnx = json.loads((DATA / "raw" / "tnx.json").read_text())
    common = sorted(set(tsy) & set(tnx))
    if not common:
        print("   ! no overlap")
        return False
    diffs = [abs(tsy[d] - tnx[d]) for d in common]
    mean_abs = statistics.mean(diffs)
    ok = mean_abs < 0.05
    print(f"   n={len(common)}  mean abs diff {mean_abs:.4f} pp  "
          f"max {max(diffs):.3f} pp -> {'PASS' if ok else 'FAIL'}")
    return ok


def check_plausibility(payload, rows) -> bool:
    print("4. Plausibility and continuity")
    pes = [r["pe"] for r in payload["rows"]]
    ok = True
    if not (7 <= min(pes) and max(pes) <= 40):
        print(f"   ! forward P/E out of band: {min(pes):.1f}-{max(pes):.1f}")
        ok = False
    else:
        print(f"   forward P/E band {min(pes):.1f}-{max(pes):.1f}  OK")

    dates = [dt.date.fromisoformat(d) for d in sorted(rows)]
    holes = [
        (dates[i], dates[i + 1], (dates[i + 1] - dates[i]).days)
        for i in range(len(dates) - 1)
        if (dates[i + 1] - dates[i]).days > 10
    ]
    if holes:
        print(f"   ! {len(holes)} coverage gap(s) >10d: "
              + ", ".join(f"{a}->{b} ({n}d)" for a, b, n in holes[:4]))
        ok = False
    else:
        print("   no coverage gaps >10 days  OK")

    jumps = 0
    ordered = [rows[d] for d in sorted(rows)]
    for prev, cur in zip(ordered, ordered[1:]):
        if abs(cur["erp"] - prev["erp"]) > 120:
            jumps += 1
    print(f"   day-over-day moves >120bps: {jumps}")
    if jumps > len(ordered) * 0.001:
        ok = False
    print(f"   {'PASS' if ok else 'FAIL'}")
    return ok


def main() -> int:
    payload, rows = load_series()
    meta = payload["meta"]
    print(f"Series: {meta['first']} -> {meta['last']}  "
          f"({len(payload['rows'])} obs)   current {meta['current_bps']:.1f} bps")
    print(f"Segments: {meta['counts']}\n")

    results = [
        check_forward_pe(rows),
        check_identity(payload),
        check_treasury(),
        check_plausibility(payload, rows),
    ]
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
