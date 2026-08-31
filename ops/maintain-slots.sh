#!/usr/bin/env bash
# Keep test-drive inventory bookable. Run daily from cron.
#
# TWO STORES, BOTH OF WHICH EXPIRE, AND BOTH OF WHICH MATTER:
#   1. the app database holds the inventory
#   2. Salesforce is the SOURCE OF TRUTH for availability — the app only offers
#      slots Salesforce marks Available
# So a healthy app database with a stale Salesforce window means nothing is
# bookable, which is precisely how 14 Aug 2026 broke mid-demo. Topping up one
# without the other fixes nothing; hence one job that does both, in this order.
#
# ORDER MATTERS. App first, then Salesforce — the Salesforce refresh pushes the
# app's slots outward, so it has to run against an already-topped-up app.
#
# SAFE TO RE-RUN. The top-up only appends days beyond what exists; the Salesforce
# refresh deletes only PAST records and upserts by App_Id__c. Running twice in a
# day changes nothing the second time.
set -euo pipefail

OPS=/opt/elite/ops
LOG=$OPS/maintain-slots.log
CONTAINER=elite-backend-1

exec >>"$LOG" 2>&1
echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') ====="

# Credentials live in the deploy env file, never in this script or in cron.
#
# READ THE KEYS, DO NOT SOURCE THE FILE. `. /opt/elite/.env` looks tidier and
# breaks: SITE_ADDRESS holds two comma-separated hostnames unquoted, so bash
# reads the second one as a command and the whole job dies at line 123 having
# done nothing. A deploy file is configuration, not a shell script, and must not
# be executed just to read three values out of it.
envval() {
  # Last assignment wins, matching how the file is actually layered; the value
  # is taken verbatim after the first "=" and stripped of surrounding quotes.
  sed -n "s/^$1=//p" /opt/elite/.env | tail -1 | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'\$//"
}
SF_DOMAIN=$(envval SF_DOMAIN)
SF_CLIENT_ID=$(envval SF_CLIENT_ID)
SF_CLIENT_SECRET=$(envval SF_CLIENT_SECRET)

for v in SF_DOMAIN SF_CLIENT_ID SF_CLIENT_SECRET; do
  if [ -z "${!v}" ]; then
    echo "ABORT: $v missing from /opt/elite/.env — Salesforce half cannot run."
    exit 1
  fi
done

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "ABORT: $CONTAINER is not running — nothing done."
  exit 1
fi

echo "--- 1/2 app inventory ---"
docker exec -i \
  -e CONFIRM=1 -e TARGET_DAYS="${TARGET_DAYS:-14}" \
  "$CONTAINER" python3 - < "$OPS/topup_slots.py"

echo "--- 2/2 salesforce window ---"
# The refresh script reads SF_CK/SF_CS; the deploy env names them
# SF_CLIENT_ID/SF_CLIENT_SECRET. Mapped here rather than renaming either, so
# neither the script nor the deploy has to change.
docker exec -i \
  -e CONFIRM=1 \
  -e SF_DOMAIN="$SF_DOMAIN" \
  -e SF_CK="$SF_CLIENT_ID" \
  -e SF_CS="$SF_CLIENT_SECRET" \
  -e SF_WINDOW_DAYS="${SF_WINDOW_DAYS:-7}" \
  "$CONTAINER" python3 - < "$OPS/refresh_sf_slots.py"

echo "--- done ---"

# Keep the log bounded. A daily job left alone for a year is a log nobody reads
# and a disk nobody watches; 2000 lines is roughly two months of runs, which is
# as far back as anyone has ever needed to look.
if [ "$(wc -l < "$LOG")" -gt 2000 ]; then
  tail -n 1000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
