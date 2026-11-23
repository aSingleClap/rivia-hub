#!/system/bin/sh
# tailscale_status.sh: OPTIONAL fallback for Step 4.8 (draft, Nov 23, not installed, not run on the tablet).
#
# Only needed if `python rivia_health.py` on the tablet shows Tailscale as "unknown" (Termux can't list the network
# interfaces). Then the tablet's root supervisor, which already reconnects Tailscale, can run this once a minute.
# It writes one tiny file Rivia reads: {"up": true/false, "checked_at": <epoch>}. Rivia never runs this, never gets
# root, and only reads the file. Set TERMUX_HOME and TERMUX_UID to match the tablet before using it.
TERMUX_HOME="${TERMUX_HOME:-/data/data/com.termux/files/home}"
TERMUX_UID="${TERMUX_UID:?set TERMUX_UID to Termux's user id}"
OUT="$TERMUX_HOME/rivia/state/tailscale.json"
mkdir -p "$(dirname "$OUT")"
# Up = some interface has an address in 100.64.0.0/10 (Tailscale's range): 100.64.x.x to 100.127.x.x.
if ip -4 addr 2>/dev/null | grep -Eq 'inet 100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.'; then UP=true; else UP=false; fi
TMP="$OUT.tmp"
printf '{"up": %s, "checked_at": %s}\n' "$UP" "$(date +%s)" > "$TMP"
chown "$TERMUX_UID:$TERMUX_UID" "$TMP" && chmod 644 "$TMP" && mv -f "$TMP" "$OUT"
