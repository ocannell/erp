# US Equity Risk Premium — daily reconstruction

`ERP (bps) = 10000 × (forward 12-month EPS ÷ S&P 500 price) − 100 × 10-year Treasury yield`

A daily series from **1990-01-02 to present (9,176 observations)**, rendered as a
Bloomberg/Daily-Shot-style chart. The deliverables are two files: `erp.html` and
`erp-data.json`.

## Why this is a rewrite, not a patch

The previous version derived its forward P/E by running **tesseract OCR over a
Yardeni Research PNG**, then interpolated monthly readings and scaled them by
daily closes. That has three fatal problems: the anchor is a lossy read of a
picture, the history is monthly, and — worst — nothing prevents look-ahead bias.

This version replaces the OCR anchor with the actual consensus data, at daily
frequency, with point-in-time discipline.

### The methodological leap: archived point-in-time vintages

S&P Dow Jones Indices publishes *S&P 500 Earnings and Estimates* — the
authoritative bottom-up Capital IQ analyst consensus. The live workbook returns
403 to scripted clients and, more importantly, only ever shows **today's**
estimates. Estimates get revised, so using today's workbook to compute 2015's
forward yield silently injects knowledge nobody had in 2015.

The fix: the Internet Archive holds **71 historical captures** of that workbook
across its two hosting paths. We downloaded 70 and parse each one as a
*vintage* — a frozen snapshot of what consensus actually was on its own
publication date. Every consensus-era day in the series is computed from the
vintage that was current **on that day**. No look-ahead.

## Series construction

| Segment | Days | Basis |
|---|---|---|
| `c` consensus | 2,049 | The archived vintage in force that day |
| `b` backcast | 5,874 | Pre-2013: published trailing EPS × calibrated forward multiple |
| `e` extrapolated | 1,253 | Vintage's own estimates rolled forward at its own implied growth |

The chart draws each basis in a different colour, so reconstructed stretches are
visually distinct from measured ones rather than being passed off as equivalent.

**Forward EPS** is a *calendar-overlap-weighted* rolling 12-month sum, not a
naive "next four quarters" total — the naive version produces a sawtooth that
jumps every quarter-end. The window must be fully covered or the function
returns `None`, so a short quarterly path can never masquerade as a full year.

**Backcast** (pre-2013, where no vintages exist) uses trailing EPS that had
actually been *published* by that date, enforcing a 120-day publication lag, and
scales it by a forward multiple of **1.1872** calibrated empirically on the
consensus era (n=69, sd=0.0944). A growth-conditional regression was tested and
rejected: R²=0.054, and it did not beat the flat multiple out of sample
(mean abs error 0.0754 vs 0.0757). The simpler estimator wins.

## Data sources

- **Consensus EPS** — S&P DJI *S&P 500 Earnings and Estimates*, 69 unique
  archived vintages (2013-06-06 → 2026-01-30), via Wayback CDX.
- **10-year Treasury** — `home.treasury.gov` daily par yield curve, 1990+
  (primary). Cboe `^TNX` fills gaps only. *(FRED is unreachable from this
  sandbox, so Treasury.gov is used as the authoritative source instead.)*
- **S&P 500 price** — daily `^GSPC` closes.

## Validation — 4/4 passing

Run `python3 pipeline/validate.py`.

1. **EPS reconciliation** — our four extracted quarterly cells summed against
   the workbook's *own printed* 12-month EPS column: **68 workbooks, mean abs
   diff $0.0000**. This is the check that matters most, because latching onto
   the wrong spreadsheet column is the single most dangerous parsing failure
   (an early version matched a rolling-annual column and inflated forward EPS
   ~4×; the parser now rejects any column mentioning "12 MONTH").
2. **Internal identity** — `erp == 100×ey − 100×y10`, max deviation 0.0000 bps.
3. **Independent yield cross-check** — Treasury.gov vs `^TNX`, n=9,131, mean abs
   diff 0.0114 pp.
4. **Plausibility & continuity** — forward P/E band 8.8–27.0, no coverage gaps
   >10 days.

Plus `node tests/headless_render.js`, which executes the page's real inline
script against a stub DOM and recording canvas, asserting the series actually
strokes (27,582 segments), the zero line is dashed red, axes are labelled and
the stat cards populate.

## Known limitation (stated, not hidden)

For 30-Jun-2023 we print **134.1 bps** where The Daily Shot prints **107 bps**.
This is expected and is not an error: Bloomberg and The Daily Shot use
LSEG/IBES or FactSet consensus, whereas this series uses S&P's bottom-up
consensus. Different consensus panels, slightly different levels; the shape and
turning points agree. This is disclosed in the page's methodology section.

## Refresh procedure

```bash
cd erp-project
python3 pipeline/fetch_vintages.py    # new archived workbook vintages (slow, throttled)
python3 pipeline/parse_vintages.py    # vintages -> data/consensus.json
python3 pipeline/fetch_market.py      # Treasury 10y, ^GSPC, ^TNX
python3 pipeline/build_series.py      # -> data/erp-series.json AND erp-data.json
python3 pipeline/validate.py          # must report 4/4
node tests/headless_render.js         # must report PASS
```

`build_series.py` writes the deployable `erp-data.json` next to `erp.html`
automatically, so a refresh needs no manual copy step.

For day-to-day updates only `fetch_market.py` + `build_series.py` are needed;
`fetch_vintages.py` is worth re-running roughly monthly, when S&P posts a new
workbook.

## Files

```
erp.html                     deliverable: canvas chart + nav sidebar
erp-data.json                deliverable: dataset the page fetches
data/erp-series.json         archive-of-record copy of the dataset
data/consensus.json          parsed vintages + final revised actuals
data/raw/                    cached Wayback workbooks and market pulls
pipeline/fetch_vintages.py   Wayback CDX discovery + throttle-safe download
pipeline/sp_vintage.py       multi-layout workbook parser (70/70 parse)
pipeline/parse_vintages.py   vintages -> consensus.json
pipeline/fetch_market.py     Treasury.gov + Yahoo market inputs
pipeline/build_series.py     the analytical core
pipeline/validate.py         4-check validation suite
tests/headless_render.js     frontend smoke test
```
