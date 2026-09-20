#!/usr/bin/env bash
# Report what Odysseus actually sees for mail folders and calendar sync.
#
# Prints folder names, the Sent folder's message count, connected calendars
# and a live sync result. Names and counts only — no message bodies, no
# tokens, nothing from .env.
#
# Usage:  ./scripts/diagnose-sync.sh [base-url]

set -uo pipefail
BASE="${1:-http://localhost:7000}"
COOKIE="${ODYSSEUS_COOKIE:-}"

say() { printf '\n\033[1m── %s\033[0m\n' "$1"; }

curl_api() {
  if [ -n "$COOKIE" ]; then
    curl -fsS --max-time 30 -H "Cookie: $COOKIE" "$@"
  else
    curl -fsS --max-time 30 "$@"
  fi
}

jqq() { python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null || echo "(could not parse)"; }

say "1. Mail folders as the server reports them"
FOLDERS=$(curl_api "$BASE/api/email/folders") || { echo "request failed — is Odysseus running at $BASE, and are you logged in? (see note at the end)"; exit 1; }
echo "$FOLDERS" | jqq 'json.dumps(d.get("folders"), ensure_ascii=False)'
echo "source:      $(echo "$FOLDERS" | jqq 'd.get("sync",{}).get("source","?")')"
echo "provisional: $(echo "$FOLDERS" | jqq 'd.get("provisional", False)')"
echo "→ On Office 365 the sent folder must appear as 'Sent Items'."
echo "  If you see a bare 'Sent' and provisional=True, the real list was never fetched."

say "2. What the Sent folder actually returns"
SENT=$(echo "$FOLDERS" | jqq 'next((f for f in (d.get("folders") or []) if "sent" in f.lower()), "Sent")')
echo "asking for: $SENT"
ENC=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$SENT")
LIST=$(curl_api "$BASE/api/email/list?folder=$ENC&limit=5") || echo "(request failed)"
echo "total:  $(echo "$LIST" | jqq 'd.get("total","?")')"
echo "error:  $(echo "$LIST" | jqq 'd.get("error","none")')"
echo "newest: $(echo "$LIST" | jqq 'json.dumps([e.get("subject","")[:60] for e in (d.get("emails") or [])[:3]], ensure_ascii=False)')"

say "3. Connected Microsoft calendars"
MSG=$(curl_api "$BASE/api/calendar/config/microsoft") || echo "(request failed)"
echo "app configured: $(echo "$MSG" | jqq 'd.get("configured","?")')"
echo "accounts:       $(echo "$MSG" | jqq 'json.dumps([{"label":a.get("label"),"connected":a.get("connected")} for a in (d.get("accounts") or [])], ensure_ascii=False)')"

say "4. Calendars known locally"
curl_api "$BASE/api/calendar/calendars" | jqq 'json.dumps([{"name":c.get("name"),"source":c.get("source")} for c in (d.get("calendars") or [])], ensure_ascii=False)'

say "5. Forcing a two-way sync now"
SYNC=$(curl_api -X POST "$BASE/api/calendar/sync?direction=both") || echo "(request failed)"
echo "$SYNC" | jqq 'json.dumps(d, ensure_ascii=False)[:900]'

say "6. Recent sync/send lines from the container log"
docker compose logs --tail=400 2>/dev/null \
  | grep -iE "sent copy|append|microsoft calendar|graph|msgraph|sync failed|reconnect the calendar" \
  | tail -25 || echo "(no matching lines — or docker compose is not reachable from here)"

cat <<'NOTE'

──
If step 1 failed with 401/403, the API needs your session cookie:
open Odysseus in the browser, press F12 → Application → Cookies, copy the
session cookie, then re-run as:

  ODYSSEUS_COOKIE='session=<value>' ./scripts/diagnose-sync.sh
NOTE
