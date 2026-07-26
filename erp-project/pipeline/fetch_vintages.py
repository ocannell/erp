#!/usr/bin/env python3
"""Download every archived vintage of the S&P DJI 'S&P 500 EPS estimates' workbook.

Why vintages? A single current workbook tells you what earnings *turned out* to
be. A chart of the forward earnings yield must instead use what the consensus
*expected at the time*. The Internet Archive holds ~75 point-in-time snapshots
of the official S&P Dow Jones Indices workbook, which together give a
look-ahead-free history of bottom-up consensus forward EPS.

Raw bytes are cached under data/raw/vintages/ so re-runs are cheap and the
build stays reproducible offline.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "vintages"
INDEX = ROOT / "data" / "raw" / "vintage_index.json"

# Both historical hosting paths of the same official workbook.
TARGETS = [
    "spglobal.com/spdji/en/documents/additional-material/sp-500-eps-est.xlsx",
    "us.spindices.com/documents/additional-material/sp-500-eps-est.xlsx",
]

CDX = (
    "https://web.archive.org/cdx/search/cdx?url={url}&output=json"
    "&limit=800&fl=timestamp,original,statuscode&collapse=digest"
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)


def get(url: str, timeout: int = 90) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def list_snapshots() -> list[dict]:
    """Query the Wayback CDX index for successful captures of each path.

    The CDX endpoint is flaky (504s under load), so each target is retried with
    backoff, and results are merged with any previously saved index so a partial
    outage never shrinks our vintage coverage.
    """
    out: dict[str, dict] = {}
    if INDEX.exists():  # keep previously discovered captures
        try:
            for rec in json.loads(INDEX.read_text()):
                out[rec["timestamp"]] = rec
        except Exception:
            pass

    for target in TARGETS:
        for attempt in range(4):
            try:
                raw = get(CDX.format(url=target), timeout=150)
                rows = json.loads(raw.decode("utf-8", "replace"))
                for ts, original, status in rows[1:]:
                    if status == "200":
                        out[ts] = {"timestamp": ts, "original": original}
                break
            except Exception as exc:  # pragma: no cover - network
                if attempt == 3:
                    print(f"  ! CDX failed for {target}: {exc}", file=sys.stderr)
                else:
                    time.sleep(5 * (attempt + 1))
    return sorted(out.values(), key=lambda r: r["timestamp"])


def snapshot_url(rec: dict) -> str:
    # 'id_' asks the archive for the original bytes, unrewritten.
    return f"https://web.archive.org/web/{rec['timestamp']}id_/{rec['original']}"


def fetch_one(rec: dict) -> tuple[str, bool, str]:
    ts = rec["timestamp"]
    dest = RAW / f"{ts}.bin"
    if dest.exists() and dest.stat().st_size > 20_000:
        return ts, True, "cached"
    # archive.org throttles aggressively; back off generously.
    for attempt in range(5):
        try:
            data = get(snapshot_url(rec))
            if len(data) < 20_000:
                raise ValueError(f"too small ({len(data)}B)")
            dest.write_bytes(data)
            return ts, True, f"{len(data)//1024}KB"
        except Exception as exc:
            if attempt == 4:
                return ts, False, str(exc)[:70]
            time.sleep(5 * (attempt + 1))
    return ts, False, "unreachable"


def main() -> int:
    RAW.mkdir(parents=True, exist_ok=True)
    print("Querying Wayback CDX index for official S&P workbook vintages...")
    snaps = list_snapshots()
    if not snaps:
        print("No snapshots found.", file=sys.stderr)
        return 1
    INDEX.write_text(json.dumps(snaps, indent=1))
    print(f"  {len(snaps)} archived captures "
          f"({snaps[0]['timestamp'][:8]} -> {snaps[-1]['timestamp'][:8]})")

    ok = fail = 0
    # Serial, politely paced: the archive refuses parallel bursts.
    with cf.ThreadPoolExecutor(max_workers=2) as pool:
        for ts, good, note in pool.map(fetch_one, snaps):
            if good:
                ok += 1
            else:
                fail += 1
                print(f"  ! {ts}: {note}", file=sys.stderr)
    print(f"Downloaded/cached {ok} vintages ({fail} failed).")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
