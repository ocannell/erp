#!/usr/bin/env python3
"""Parse cached workbook vintages into a compact point-in-time consensus store.

Output: data/consensus.json
  {
    "vintages": [
       {"as_of": "2015-06-11", "close": 2108.86,
        "quarters": {"2015-06-30": 28.51, ...},
        "estimated": ["2015-06-30", ...]},
       ...
    ],
    "actuals": {"1988-03-31": 5.48, ...}   # final realized operating EPS
  }

The `actuals` block is taken from the newest vintage (which carries the fully
revised history back to 1988) and is used only for the pre-2013 backcast and
for calibrating analyst optimism -- never to backfill the modern series.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pipeline.sp_vintage import parse_workbook  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
VINTAGE_DIR = ROOT / "data" / "raw" / "vintages"
OUT = ROOT / "data" / "consensus.json"


def capture_date(stem: str) -> dt.date:
    return dt.date(int(stem[:4]), int(stem[4:6]), int(stem[6:8]))


def main() -> int:
    files = sorted(VINTAGE_DIR.glob("*.bin"))
    if not files:
        print("No cached vintages; run fetch_vintages.py first.", file=sys.stderr)
        return 1

    parsed = []
    newest = None
    failures = []
    for path in files:
        vintage = parse_workbook(path.read_bytes(), fallback_as_of=capture_date(path.stem))
        if vintage is None:
            failures.append(path.stem)
            continue
        forward = vintage.forward_eps()
        # Sanity gate: a plausible forward P/E keeps a mis-parsed sheet from
        # silently poisoning the series.
        if vintage.close and forward:
            implied_pe = vintage.close / forward
            if not (8.0 <= implied_pe <= 40.0):
                failures.append(f"{path.stem}(P/E {implied_pe:.1f})")
                continue
        parsed.append(vintage)
        if newest is None or vintage.as_of > newest.as_of:
            newest = vintage

    if not parsed or newest is None:
        print("No usable vintages parsed.", file=sys.stderr)
        return 1

    # Deduplicate on as_of, keeping the richest snapshot for each date.
    by_date: dict[dt.date, object] = {}
    for vintage in parsed:
        prior = by_date.get(vintage.as_of)
        if prior is None or len(vintage.quarters) > len(prior.quarters):
            by_date[vintage.as_of] = vintage

    payload = {
        "generated_utc": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "vintage_count": len(by_date),
        "vintages": [
            {
                "as_of": v.as_of.isoformat(),
                "close": v.close,
                "quarters": {q.isoformat(): round(e, 4) for q, e in sorted(v.quarters.items())},
                "estimated": sorted(q.isoformat() for q in v.estimated),
            }
            for _, v in sorted(by_date.items())
        ],
        # Final revised quarterly operating EPS (newest vintage = fully actual
        # for every quarter it no longer marks as an estimate).
        "actuals": {
            q.isoformat(): round(e, 4)
            for q, e in sorted(newest.quarters.items())
            if q not in newest.estimated
        },
        "actuals_source_vintage": newest.as_of.isoformat(),
    }

    OUT.write_text(json.dumps(payload, indent=1))
    dates = [v["as_of"] for v in payload["vintages"]]
    print(f"Parsed {len(by_date)} unique vintages ({dates[0]} -> {dates[-1]})")
    print(f"Realized quarterly EPS: {len(payload['actuals'])} quarters "
          f"({min(payload['actuals'])} -> {max(payload['actuals'])})")
    if failures:
        print(f"Skipped {len(failures)}: {failures}", file=sys.stderr)
    print(f"Wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
