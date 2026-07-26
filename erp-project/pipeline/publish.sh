#!/usr/bin/env bash
#
# Publisher for the equity risk premium chart.
#
# Rebuilds the series, VALIDATES it, and only then atomically deploys to the
# web root. If any stage fails, the previously deployed files are left exactly
# as they were: a stale chart is bad, but a broken or wrong chart is worse.
#
#   --daily   (default) refresh market data (Treasury 10y, ^GSPC, ^TNX) and
#             rebuild. Cheap; safe to run every day.
#   --full    also re-scan the Internet Archive for newly published S&P DJI
#             workbook vintages, then reparse. Slow and throttled, so this is
#             intended for a weekly/monthly slot rather than daily.
#
# Deliberately NOT run with `set -e`: each stage's exit code is inspected so a
# failure can be logged with context and the deploy skipped, rather than the
# script dying silently mid-way.

set -uo pipefail

BASE="/home/allofthesewords/erp_pipeline"
WEB="/home/allofthesewords/public_html"
PY="/usr/bin/python3"
LOG="$BASE/logs/publish.log"
MODE="${1:---daily}"

mkdir -p "$BASE/logs"

log() { echo "[$(date -u '+%F %T')] $*" >> "$LOG"; }

log "publisher start mode=$MODE"

cd "$BASE" || { log "ERROR: base dir $BASE missing"; exit 1; }

# ---------------------------------------------------------------- fetch stage
if [ "$MODE" = "--full" ]; then
  log "scanning Internet Archive for new workbook vintages"
  "$PY" pipeline/fetch_vintages.py >> "$LOG" 2>&1
  if [ $? -ne 0 ]; then
    log "WARN: vintage scan failed; continuing with cached vintages"
  else
    "$PY" pipeline/parse_vintages.py >> "$LOG" 2>&1 || {
      log "ERROR: vintage parse failed; nothing deployed"; exit 1; }
  fi
fi

log "fetching market data"
"$PY" pipeline/fetch_market.py >> "$LOG" 2>&1 || {
  log "ERROR: market fetch failed; nothing deployed"; exit 1; }

# ---------------------------------------------------------------- build stage
log "building series"
"$PY" pipeline/build_series.py >> "$LOG" 2>&1 || {
  log "ERROR: build failed; nothing deployed"; exit 1; }

# ------------------------------------------------------------- validate stage
# The gate. validate.py exits non-zero unless all four checks pass, so a data
# regression (wrong spreadsheet column, yield scale error, coverage hole)
# blocks the deploy instead of silently reaching the site.
log "validating"
"$PY" pipeline/validate.py >> "$LOG" 2>&1 || {
  log "ERROR: validation FAILED; previous web output preserved"; exit 1; }

# Frontend smoke test, when node is available. Guards against a dataset that
# validates numerically but the page cannot actually render.
if command -v node >/dev/null 2>&1 && [ -f tests/headless_render.js ]; then
  node tests/headless_render.js >> "$LOG" 2>&1 || {
    log "ERROR: render test FAILED; previous web output preserved"; exit 1; }
fi

# --------------------------------------------------------------- deploy stage
SRC="$BASE/erp-data.json"
[ -s "$SRC" ] || { log "ERROR: $SRC missing or empty; nothing deployed"; exit 1; }

# Sanity-check the shape before overwriting a working file. Cheap insurance
# against deploying a truncated or restructured payload.
"$PY" - "$SRC" <<'PYEOF' >> "$LOG" 2>&1
import json, sys
d = json.load(open(sys.argv[1]))
rows = d.get("rows") or []
assert len(rows) > 5000, f"only {len(rows)} rows"
assert {"d", "erp"} <= set(rows[-1]), "row schema changed"
print(f"  payload ok: {len(rows)} rows, latest {rows[-1]['d']} = {rows[-1]['erp']} bps")
PYEOF
if [ $? -ne 0 ]; then
  log "ERROR: payload shape check failed; previous web output preserved"; exit 1
fi

# Atomic: write alongside then rename, so a reader never sees a half-written file.
cp "$SRC" "$WEB/erp-data.json.tmp" && mv "$WEB/erp-data.json.tmp" "$WEB/erp-data.json"
if [ $? -ne 0 ]; then
  log "ERROR: deploy copy failed"; exit 1
fi
chmod 644 "$WEB/erp-data.json"

LATEST=$("$PY" -c "import json;print(json.load(open('$WEB/erp-data.json'))['meta']['last'])" 2>/dev/null)
log "deployed erp-data.json (latest observation $LATEST)"
log "publisher ok"
exit 0
