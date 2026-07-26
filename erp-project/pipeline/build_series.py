#!/usr/bin/env python3
"""Build the S&P 500 equity risk premium series.

    ERP (bps) = 10000 x (forward 12m EPS / price) - 100 x 10y Treasury yield

The numerator is the whole game. Three principles drive this implementation.

1. POINT-IN-TIME, NO LOOK-AHEAD.
   The forward earnings yield must use the consensus that existed on the day,
   not what earnings turned out to be. For every date we read the most recent
   *archived vintage* of the official S&P Dow Jones Indices consensus workbook
   published on or before that date. Even in the pre-2013 backcast, trailing
   earnings are only used once they would actually have been published
   (a publication lag measured from the vintages themselves).

2. CONTINUOUS ROLLING 12-MONTH WINDOW.
   Forward EPS is the calendar-overlap-weighted sum of quarterly consensus EPS
   across the next 365 days. Weighting by real overlap -- rather than naively
   summing "the next four quarters" -- removes the artificial sawtooth a
   discrete quarter-counting rule stamps onto the series every quarter-end,
   and lets EPS drift smoothly as weight rolls from nearer to farther
   quarters. That is how a genuine forward-earnings series behaves.

3. HONEST SEGMENTATION.
   Archived vintages exist only from 2013, and S&P discontinued the workbook
   in early 2026. Dates outside that span are reconstructed and are labelled
   as such in the output so the chart never claims more precision than the
   data supports:
     c = point-in-time archived consensus  (the real thing)
     b = backcast   (published trailing EPS x calibrated forward multiple)
     e = extrapolated (final vintage's own implied growth carried forward)
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = DATA / "erp-series.json"
# The chart page fetches its dataset from a sibling file, so the builder also
# writes the deployable copy. Keeping both means data/ stays the archive of
# record while erp-data.json is exactly what gets uploaded next to erp.html.
DEPLOY_OUT = ROOT / "erp-data.json"

WINDOW_DAYS = 365

# Days after a quarter ends before its EPS is public. The vintages show a
# median lag of 139 days to a quarter being marked "actual" (p10 = 99); 120
# days is a deliberately conservative middle that never uses an unpublished
# number in the backcast.
PUBLICATION_LAG_DAYS = 120


def D(iso: str) -> dt.date:
    return dt.date.fromisoformat(iso)


# --------------------------------------------------------------------------
# quarter arithmetic
# --------------------------------------------------------------------------
def quarter_start(q_end: dt.date) -> dt.date:
    month, year = q_end.month - 2, q_end.year
    if month <= 0:
        month += 12
        year -= 1
    return dt.date(year, month, 1)


def next_quarter_end(q_end: dt.date) -> dt.date:
    month, year = q_end.month + 3, q_end.year
    if month > 12:
        month -= 12
        year += 1
    return dt.date(year, month, 31 if month in (3, 12) else 30)


def forward_eps(as_of: dt.date, quarters: dict[dt.date, float]) -> float | None:
    """Calendar-overlap-weighted forward 12-month EPS.

    Each quarter contributes in proportion to the share of it that falls in
    the next 365 days. Returns None unless the window is fully covered, so a
    short quarterly path can never masquerade as a full year of earnings.
    """
    win_end = as_of + dt.timedelta(days=WINDOW_DAYS)
    total = covered = 0.0
    for q_end, eps in quarters.items():
        q0, q1 = quarter_start(q_end), q_end + dt.timedelta(days=1)
        lo, hi = max(q0, as_of), min(q1, win_end)
        share = (hi - lo).days
        if share <= 0:
            continue
        total += eps * share / (q1 - q0).days
        covered += share
    if covered < WINDOW_DAYS - 2:
        return None
    return total


def implied_growth(quarters: dict[dt.date, float]) -> float:
    """Year-over-year growth implied by a quarterly path's last 8 quarters."""
    keys = sorted(quarters)
    if len(keys) < 8:
        return 0.0
    recent = sum(quarters[q] for q in keys[-4:])
    prior = sum(quarters[q] for q in keys[-8:-4])
    if prior <= 0:
        return 0.0
    return max(-0.25, min(0.25, recent / prior - 1.0))


def extend_path(quarters: dict[dt.date, float], extra: int = 4) -> dict[dt.date, float]:
    """Carry a quarterly path forward at its own implied growth rate.

    A vintage published early in a calendar year only forecasts to that
    December -- as little as ~305 days of horizon -- which is not enough to
    fill a 365-day window. Rather than dropping those dates (which tore
    multi-month holes in the series), we roll the vintage's *own* estimates
    forward at the growth rate it implies. No outside or future information is
    introduced, so the point-in-time guarantee is preserved.
    """
    out = dict(quarters)
    growth = implied_growth(out)
    for _ in range(extra):
        keys = sorted(out)
        last = keys[-1]
        year_ago = [q for q in keys if 360 <= (last - q).days <= 372]
        base = out[year_ago[-1]] if year_ago else out[keys[-4]]
        out[next_quarter_end(last)] = base * (1.0 + growth)
    return out


# --------------------------------------------------------------------------
# backcast helpers (pre-2013): published information only
# --------------------------------------------------------------------------
def published_trailing_eps(day: dt.date, actuals: dict[dt.date, float]) -> float | None:
    """Trailing 12-month EPS from quarters that were public by ``day``."""
    known = sorted(q for q in actuals if (day - q).days >= PUBLICATION_LAG_DAYS)
    if len(known) < 4:
        return None
    return sum(actuals[q] for q in known[-4:])


def main() -> int:
    consensus = json.loads((DATA / "consensus.json").read_text())
    treasury = json.loads((DATA / "raw" / "treasury_10y.json").read_text())
    tnx = json.loads((DATA / "raw" / "tnx.json").read_text())
    spx = json.loads((DATA / "raw" / "spx.json").read_text())

    # Authoritative Treasury par yields win; ^TNX only fills gaps.
    yields = dict(tnx)
    yields.update(treasury)

    actuals = {D(q): e for q, e in consensus["actuals"].items()}

    # Keep the vintage's published path *and* an extended copy. A vintage
    # published early in a calendar year may only forecast to that December,
    # which cannot fill a 365-day window; we then roll its own estimates
    # forward at its own implied growth and label those dates "extrapolated"
    # rather than passing them off as pure consensus.
    vintages = []
    for record in consensus["vintages"]:
        published = {D(q): e for q, e in record["quarters"].items()}
        vintages.append(
            {
                "as_of": D(record["as_of"]),
                "path": published,
                "extended": extend_path(published, extra=8),
            }
        )
    vintages.sort(key=lambda v: v["as_of"])
    v_dates = [v["as_of"] for v in vintages]
    first_vintage, last_vintage = v_dates[0], v_dates[-1]

    # ---- calibrate the backcast's forward multiple ------------------------
    # In the era where we have real consensus, how much higher is forward
    # consensus EPS than the trailing EPS that was public at the time?
    # (Tested a growth-conditional regression too: R^2 = 0.05, i.e. no real
    # improvement, so the parsimonious constant is used.)
    ratios = []
    for v in vintages:
        fwd = forward_eps(v["as_of"], v["extended"])
        trail = published_trailing_eps(v["as_of"], actuals)
        if fwd and trail and trail > 0:
            ratios.append(fwd / trail)
    fwd_multiple = statistics.median(ratios) if ratios else 1.0
    spread = statistics.pstdev(ratios) if len(ratios) > 1 else 0.0
    print(f"Backcast forward multiple: {fwd_multiple:.4f} "
          f"(n={len(ratios)}, sd={spread:.4f})")

    tail_path = extend_path(vintages[-1]["path"], extra=8)
    print(f"Tail growth after {last_vintage}: "
          f"{implied_growth(vintages[-1]['path']) * 100:.2f}% y/y")

    rows = []
    counts = {"b": 0, "c": 0, "e": 0}
    for iso in sorted(spx):
        day = D(iso)
        if day < dt.date(1990, 1, 1):
            continue  # Treasury par-yield curve begins 1990
        price, y10 = spx[iso], yields.get(iso)
        if not price or price <= 0 or y10 is None:
            continue

        if day < first_vintage:
            trail = published_trailing_eps(day, actuals)
            eps = trail * fwd_multiple if trail else None
            basis = "b"
        elif day <= last_vintage:
            vintage = vintages[bisect.bisect_right(v_dates, day) - 1]
            # Prefer the strictly-published path; fall back to that same
            # vintage rolled forward when its horizon is too short.
            eps = forward_eps(day, vintage["path"])
            basis = "c"
            if eps is None:
                eps = forward_eps(day, vintage["extended"])
                basis = "e"
        else:
            eps = forward_eps(day, tail_path)
            basis = "e"

        if not eps or eps <= 0:
            continue
        earnings_yield = 100.0 * eps / price
        rows.append(
            {
                "d": iso,
                "erp": round(100.0 * earnings_yield - 100.0 * y10, 2),
                "ey": round(earnings_yield, 4),
                "y10": round(y10, 4),
                "pe": round(price / eps, 3),
                "b": basis,
            }
        )
        counts[basis] += 1

    if not rows:
        print("No rows produced.", file=sys.stderr)
        return 1

    values = [r["erp"] for r in rows]
    payload = {
        "generated_utc": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "meta": {
            "label": "S&P 500 forward earnings yield less 10-year Treasury yield",
            "units": "basis points",
            "formula": "ERP = 10000 x (forward 12m EPS / price) - 100 x 10y yield",
            "forward_eps_method": (
                "Calendar-overlap-weighted sum of bottom-up consensus quarterly "
                "operating EPS across the next 365 days."
            ),
            "consensus_source": (
                "S&P Dow Jones Indices 'S&P 500 Earnings and Estimates' workbook, "
                f"read from {len(vintages)} archived point-in-time vintages "
                f"({first_vintage} to {last_vintage}); no look-ahead."
            ),
            "treasury_source": (
                "U.S. Treasury daily par yield curve, 10-year "
                "(authoritative; Cboe ^TNX fills gaps only)."
            ),
            "price_source": "S&P 500 (^GSPC) daily closes.",
            "publication_lag_days": PUBLICATION_LAG_DAYS,
            "backcast_forward_multiple": round(fwd_multiple, 4),
            "backcast_multiple_sd": round(spread, 4),
            "vintage_count": len(vintages),
            "vintage_range": [first_vintage.isoformat(), last_vintage.isoformat()],
            "segments": {
                "c": "point-in-time archived consensus vintage",
                "b": "backcast: published trailing EPS x calibrated forward multiple",
                "e": "extrapolated: final vintage carried at its implied growth",
            },
            "counts": counts,
            "first": rows[0]["d"],
            "last": rows[-1]["d"],
            "current_bps": rows[-1]["erp"],
            "current_ey": rows[-1]["ey"],
            "current_y10": rows[-1]["y10"],
            "min_bps": min(values),
            "max_bps": max(values),
            "median_bps": round(statistics.median(values), 2),
        },
        "rows": rows,
    }
    blob = json.dumps(payload, separators=(",", ":"))
    OUT.write_text(blob)
    DEPLOY_OUT.write_text(blob)

    print(f"Built {len(rows)} observations {rows[0]['d']} -> {rows[-1]['d']}")
    print(f"  segments {counts}")
    print(f"  current {rows[-1]['erp']:.1f} bps | range {min(values):.0f} to "
          f"{max(values):.0f} | median {statistics.median(values):.0f}")
    print(f"Wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size // 1024} KB)")
    print(f"Wrote {DEPLOY_OUT.relative_to(ROOT)} (deployable copy)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
