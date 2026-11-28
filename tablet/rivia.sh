#!/data/data/com.termux/files/usr/bin/bash
# rivia.sh: run RIVIA's MCP server on the tablet (Termux).
#
# One-time setup (Termux):
#   bash ~/storage/shared/rivia/rivia.sh install-cli
#   (puts `rivia` on PATH as $PREFIX/bin/rivia). Or:
#   echo "alias rivia='bash ~/storage/shared/rivia/rivia.sh'" >> ~/.bashrc && source ~/.bashrc
#
# Then:
#   rivia start     start the server, the tunnel and the watchdog in the background
#   rivia stop      stop all three
#   rivia status    is it running, does it answer locally and at mcp.rivia.observer
#   rivia test      run both test files (do this after editing rivia_auth.py)
#   rivia sched selftest   run the planner checks (must pass after any planner change)
#   rivia sched google-login  sign in to Google (Calendar read + write, and Drive file backup)
#   rivia sched events <day>  print primary-calendar events (today, tomorrow, weekday, or YYYY-MM-DD)
#   rivia sched signin-check  check Google sign-in now (same check as the auto_write_time timer)
#   rivia sched morning-tick  run the morning tick now: sign-in, auto-write, propose tomorrow, backup
#   rivia sched evening-refresh  re-plan tomorrow at/after 21:00 ET; supersede a stale proposal
#   rivia sched settings [day]  print schedule settings (and the 17/15 seed if it has run)
#   rivia sched start-fresh    unlock a new, empty scheduler.db (init-db); a second run is a no-op
#   rivia sched rides          ride count in scheduler.db
#   rivia sched restore <file>  merge rides from a Drive copy (scheduler.db or scheduler.json); never drops any
#   rivia sched backup         upload scheduler.db to Drive (same file each time); --force to push anyway
#   rivia sched pin <place> <lat> <lng>  store a place pin (Termux); refuses pins whose zones would overlap
#   rivia sched places         every place: name, pin, zone size, core or custom, removed
#   rivia sched seed-places    store the core places' names (and pins) in the settings store; safe to re-run
#   rivia sched add-place "<name>" <lat> <lng> [meters]  add one of your own places (e.g. Giant)
#   rivia sched rename-place <place> "<name>"   change a place's spoken name
#   rivia sched remove-place <place>   remove one of your own places (core places can't be removed)
#   rivia sched zone <place> <meters|bigger|smaller>   set a place's zone size (50-400 m)
#   rivia sched exclude-ride <ride_id> "<reason>"   stop learning from a bad ride (never deletes it)
#   rivia sched include-ride <ride_id>   undo exclude-ride
#   rivia sched call <tool> [json]  call an app-door tool locally (no phone)
#   rivia sched approve <id>  approve a pending standing change
#   rivia sched reject <id>   reject a pending standing change
#   rivia logs      last lines of the server and tunnel logs
#   rivia body      her body signals: the last 5 readings (show), or take one now (once)
#   rivia body dials  the shadow dials (stress, confidence, caution, the pull she'd feel): last 5 rows
#   rivia body plan   Step 5 in shadow: how well her plans matched your days, last 7 scored days
#   rivia trust       Step 6 in shadow: trust, your satisfaction, flags, unlocks, last judged days and actions
#   rivia trust kill  Step 6 off switch: every unlock off, trust starts over (`rivia trust unkill` clears it)
#   rivia reading   Step 7 in shadow: messages per situation, word list and Flash status, patterns, would-asks
#   rivia style     Step 8 in shadow: her would-be style, the steady-then-calmer plan, what she learned
#   rivia appraisal Step 3 in shadow: events, her would-be mood beside her real one, caution flags
#   rivia revoke    kill switch: revoke every app and admin token (sign in again in the app after)
#   rivia restart   stop + start (do this after editing any .py file)
#   rivia setup     make new secrets in ~/.rivia/.env (only if you have none)
#   rivia token     make the Rivia app's token (replaces the old one)
#   rivia admin-token  make the admin-door token (7 days; saved under ~/.rivia, not printed)
#   rivia install-cli  put `rivia` on PATH ($PREFIX/bin on Termux, else ~/.local/bin)

CODE="$HOME/storage/shared/rivia"   # the code you edit
VENV="$HOME/rivia-venv"             # Python packages (must stay in Termux home)
DATA="$HOME/rivia"                  # auth.db and logs (private, not shared storage)
LOGS="$DATA/logs"
PORT=8787
PUBLIC="https://mcp.rivia.observer/health"
WATCH_S="${RIVIA_WATCH_S:-60}"      # watchdog checks this often (seconds)
export PYTHONDONTWRITEBYTECODE=1    # no __pycache__ clutter in the shared folder

mkdir -p "$LOGS"

die() { echo "rivia: $*" >&2; exit 1; }

load_venv() {
    [ -f "$VENV/bin/activate" ] || die "no venv at $VENV. Rebuild it (see README, 'Rebuild the venv')."
    # shellcheck disable=SC1091
    . "$VENV/bin/activate"
}

load_env() {
    local f
    for f in "$HOME/.rivia/.env" "$HOME/rivia/.env"; do
        if [ -f "$f" ]; then
            set -a; . "$f"; set +a
            return 0
        fi
    done
    die "no .env in ~/.rivia or ~/rivia. Run: rivia setup"
}

alive() {  # pidfile -> running AND still our process (a reused pid doesn't count)
    local p want
    [ -f "$1" ] && p="$(cat "$1")" && kill -0 "$p" 2>/dev/null || return 1
    case "$1" in
        *server.pid) want=rivia_server.py ;; *tunnel.pid) want=cloudflared ;; *watch.pid) want=watch-loop ;; *) return 0 ;;
    esac
    tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -q "$want"
}

healthy() { curl -fsS -m 5 -o /dev/null "$1" 2>/dev/null; }  # URL answers 2xx within 5s

stray_servers() { pgrep -f '^[^ ]*python[^ ]* rivia_server\.py' ; }  # every rivia_server.py we run

kill_strays() {  # a rivia_server.py not in server.pid (e.g. stopped/hung) would still hold :$PORT
    local p
    [ -n "$(stray_servers)" ] || return 0
    for p in $(stray_servers); do
        echo "server: killing stray rivia_server.py (pid $p)"; kill -CONT "$p" 2>/dev/null; kill "$p" 2>/dev/null
    done
    sleep 3
    for p in $(stray_servers); do kill -9 "$p" 2>/dev/null; done
}

kill_pid() {  # pidfile: stop it even if hung (SIGSTOPped or wedged), then drop the pidfile
    local p
    if alive "$1"; then
        p="$(cat "$1")"; kill -CONT "$p" 2>/dev/null; kill "$p" 2>/dev/null; sleep 3
        kill -0 "$p" 2>/dev/null && kill -9 "$p" 2>/dev/null
    fi
    rm -f "$1"
}

get() {  # get URL -> prints body or ERROR
    python - "$1" <<'EOF'
import sys, urllib.request
try:
    print(urllib.request.urlopen(sys.argv[1], timeout=8).read().decode()[:200])
except Exception as e:
    print("ERROR", type(e).__name__, e)
EOF
}

start() {
    load_venv; load_env
    [ -f "$CODE/rivia_server.py" ] || die "can't see $CODE/rivia_server.py. Run termux-setup-storage."
    termux-wake-lock 2>/dev/null
    # Watchdog first, so a server or tunnel that fails to start below gets retried.
    if ! alive "$DATA/watch.pid"; then
        nohup bash "$0" watch-loop > /dev/null 2>&1 &
        echo $! > "$DATA/watch.pid"
        echo "watchdog: started (checks every ${WATCH_S}s)"
    fi
    if alive "$DATA/server.pid"; then
        echo "server: already running"
    else
        cd "$CODE" || die "can't cd to $CODE"
        kill_strays
        nohup python rivia_server.py --public >> "$LOGS/server.log" 2>&1 &
        echo $! > "$DATA/server.pid"
        sleep 4
        alive "$DATA/server.pid" || { tail -n 20 "$LOGS/server.log"; die "server didn't start (log above)"; }
        echo "server: started"
    fi
    if alive "$DATA/tunnel.pid"; then
        echo "tunnel: already running"
    else
        command -v cloudflared >/dev/null || die "cloudflared isn't installed (pkg install cloudflared)"
        # RIVIA_TUNNEL in .env names the tunnel; empty means ~/.cloudflared/config.yml decides.
        # --metrics pins cloudflared's local status page (127.0.0.1 only) to one port, so the body-signal
        # reader (emotion spec Step 4.1) always finds its edge-connection count there. RIVIA_TUNNEL_METRICS
        # in .env overrides it.
        nohup cloudflared tunnel --metrics "${RIVIA_TUNNEL_METRICS:-127.0.0.1:20241}" run ${RIVIA_TUNNEL:+"$RIVIA_TUNNEL"} \
            >> "$LOGS/tunnel.log" 2>&1 &
        echo $! > "$DATA/tunnel.pid"
        sleep 6
        alive "$DATA/tunnel.pid" || { tail -n 20 "$LOGS/tunnel.log"; die "tunnel didn't start (log above)"; }
        echo "tunnel: started"
    fi
    status
}

# Restarts the server or tunnel if either dies, the server if local /health fails
# RIVIA_WATCH_FAILS (3) checks in a row (hung), and the tunnel if public /health
# fails that often while local is fine. Started by `start`, stopped by `stop`.
watch_loop() {
    local lf=0 pf=0 max="${RIVIA_WATCH_FAILS:-3}" w="$LOGS/watch.log"
    while true; do
        sleep "$WATCH_S"
        if ! alive "$DATA/server.pid" || ! alive "$DATA/tunnel.pid"; then
            echo "$(date '+%F %T') server or tunnel down; restarting" >> "$w"
            ( start ) >> "$w" 2>&1; lf=0; pf=0; continue
        fi
        if healthy "http://127.0.0.1:$PORT/health"; then lf=0; else lf=$((lf + 1)); fi
        if [ "$lf" -ge "$max" ]; then
            echo "$(date '+%F %T') local /health failed $lf times; restarting server" >> "$w"
            kill_pid "$DATA/server.pid"; ( start ) >> "$w" 2>&1; lf=0; pf=0; continue
        fi
        [ "$lf" -eq 0 ] || continue
        if healthy "$PUBLIC"; then pf=0; else pf=$((pf + 1)); fi
        if [ "$pf" -ge "$max" ]; then
            echo "$(date '+%F %T') public /health failed $pf times; restarting tunnel" >> "$w"
            kill_pid "$DATA/tunnel.pid"; ( start ) >> "$w" 2>&1; pf=0
        fi
    done
}

stop() {
    local p
    for p in watch tunnel server; do
        if alive "$DATA/$p.pid"; then kill "$(cat "$DATA/$p.pid")" && echo "$p: stopped"; else echo "$p: wasn't running"; fi
        rm -f "$DATA/$p.pid"
    done
    termux-wake-unlock 2>/dev/null
}

status() {
    load_venv
    alive "$DATA/server.pid" && echo "server: running" || echo "server: NOT running"
    alive "$DATA/tunnel.pid" && echo "tunnel: running" || echo "tunnel: NOT running"
    alive "$DATA/watch.pid" && echo "watchdog: running" || echo "watchdog: NOT running"
    echo "local:  $(get "http://127.0.0.1:$PORT/health")"
    echo "public: $(get "$PUBLIC")"
}

# Planner / sched helpers. Resolve code next to this script (repo tablet/ on a
# dev box, or ~/storage/shared/rivia when the script is deployed there).
sched_code_and_python() {
    # Sets $code and $py, then cds to the code dir. Prefer the folder this
    # script lives in (run it with bash; shared storage is noexec).
    local here
    here="$(cd "$(dirname "$0")" && pwd)"
    if [ -f "$here/rivia_planner.py" ]; then
        code="$here"
    elif [ -f "$CODE/rivia_planner.py" ]; then
        code="$CODE"
    else
        die "can't see rivia_planner.py next to $0 or in $CODE"
    fi
    if [ -x "$VENV/bin/python" ]; then
        # shellcheck disable=SC1091
        . "$VENV/bin/activate"
        py=python
    elif command -v python3 >/dev/null; then
        py=python3
    elif command -v python >/dev/null; then
        py=python
    else
        die "no python3 (and no venv at $VENV)"
    fi
    cd "$code" || die "can't cd to $code"
}

sched_selftest() {
    sched_code_and_python
    "$py" rivia_planner.py selftest
}

sched_gcal() {
    sched_code_and_python
    if [ -z "${RIVIA_GOOGLE_CLIENT_ID:-}" ] || [ -z "${RIVIA_GOOGLE_CLIENT_SECRET:-}" ]; then
        load_env
    fi
    "$py" rivia_gcal.py "$@"
}

sched() {
    case "${1:-}" in
        selftest) sched_selftest ;;
        google-login) sched_gcal "$@" ;;
        events)
            [ -n "${2:-}" ] || die 'usage: rivia sched events <day>'
            sched_gcal "$@"
            ;;
        signin-check) sched_gcal "$@" ;;
        backup) sched_gcal "$@" ;;
        settings|settings-log)
            sched_code_and_python
            "$py" rivia_sched.py "$@"
            ;;
        rides)
            sched_code_and_python
            "$py" rivia_sched.py rides
            ;;
        restore)
            [ -n "${2:-}" ] || die 'usage: rivia sched restore <file>'
            sched_code_and_python
            "$py" rivia_sched.py restore "$2"
            ;;
        start-fresh)
            sched_code_and_python
            "$py" rivia_sched.py start-fresh
            ;;
        pin)
            [ -n "${2:-}" ] && [ -n "${3:-}" ] && [ -n "${4:-}" ] || die 'usage: rivia sched pin <place> <lat> <lng>'
            sched_code_and_python
            "$py" rivia_sched.py pin "$2" "$3" "$4"
            ;;
        places|seed-places|add-place|rename-place|remove-place|zone|exclude-ride|include-ride)
            sched_code_and_python
            "$py" rivia_sched.py "$@"
            ;;
        call)
            [ -n "${2:-}" ] || die 'usage: rivia sched call <tool> [json]'
            sched_code_and_python
            args="${3:-}"
            [ -n "$args" ] || args='{}'
            "$py" rivia_sched.py call "$2" "$args"
            ;;
        approve)
            [ -n "${2:-}" ] || die 'usage: rivia sched approve <change_id>'
            sched_code_and_python
            "$py" rivia_sched.py approve "$2"
            ;;
        reject)
            [ -n "${2:-}" ] || die 'usage: rivia sched reject <change_id>'
            sched_code_and_python
            "$py" rivia_sched.py reject "$2"
            ;;
        morning-tick)
            sched_code_and_python
            "$py" rivia_sched.py morning-tick
            ;;
        evening-refresh)
            sched_code_and_python
            "$py" rivia_sched.py evening-refresh
            ;;
        *) die 'usage: rivia sched selftest | google-login | events <day> | signin-check | backup | settings [day] | settings-log | rides | restore <file> | start-fresh | pin <place> <lat> <lng> | places | seed-places | add-place | rename-place | remove-place | zone | exclude-ride | include-ride | call <tool> [json] | approve <id> | reject <id> | morning-tick | evening-refresh' ;;
    esac
}

# Put `rivia` on PATH. Writes a bash wrapper (not a symlink: shared storage
# is noexec on the tablet). Idempotent. Termux: $PREFIX/bin; else ~/.local/bin.
install_cli() {
    local here dest dir
    here="$(cd "$(dirname "$0")" && pwd)"
    [ -f "$here/rivia.sh" ] || die "can't see $here/rivia.sh"
    if [ -n "${PREFIX:-}" ]; then
        dir="$PREFIX/bin"
    else
        dir="$HOME/.local/bin"
    fi
    mkdir -p "$dir"
    dest="$dir/rivia"
    # Shared storage is mounted noexec on Android, so a symlink to the copy
    # there fails with "Permission denied". Write a small wrapper that runs
    # rivia.sh through bash instead (safe to re-run; replaces an old symlink).
    rm -f "$dest"
    printf '#!/usr/bin/env bash\nexec bash "%s/rivia.sh" "$@"\n' "$here" > "$dest"
    chmod 755 "$dest"
    echo "installed $dest (runs bash $here/rivia.sh)"
    case ":$PATH:" in
        *":$dir:"*) ;;
        *) echo "note: $dir is not on PATH yet; add it (Termux already has \$PREFIX/bin)." ;;
    esac
}

case "${1:-}" in
    start)   start ;;
    stop)    stop ;;
    restart) stop; sleep 1; start ;;
    status)  status ;;
    watch-loop) watch_loop ;;
    test)    load_venv
             here="$(cd "$(dirname "$0")" && pwd)"
             if [ -f "$here/test_rivia_auth.py" ]; then testdir="$here"; else testdir="$CODE"; fi
             cd "$testdir" || exit 1
             for t in test_rivia_auth.py test_public_e2e.py test_sched.py test_gcal.py test_signin_check.py test_settings.py test_restore.py test_drive_backup.py test_places.py test_arrival.py test_admin_door.py test_approval.py test_hub.py test_hub_mcp.py test_write_day.py test_app_login.py test_core_split.py test_emotion.py test_changelog.py test_acceptance.py test_inject.py test_emotion_app.py test_layers.py test_decay.py test_dial.py test_fade_log.py test_drift_limits.py tests/test_label_layers.py tests/test_no_fear.py test_step2_acceptance.py test_drift.py test_drift_weekly.py test_fixed_logging.py test_body_signals.py test_body_collect.py test_normal.py test_stress.py test_confidence.py test_body_cap.py test_caution.py test_health.py test_body_shadow.py test_plan_score.py test_gap.py test_trend.py test_trend_mood.py test_goal_limits.py test_usage.py test_progress.py test_progress_settings.py test_predict.py test_satisfaction.py test_area_confidence.py test_trust.py test_trust_settings.py test_trust_app.py test_trust_shadow.py test_reading.py test_reading_flash.py test_reading_settings.py test_reading_app.py test_style.py test_style_settings.py test_style_app.py test_appraisal.py test_appraisal_live.py test_expression.py test_appraisal_settings.py test_appraisal_app.py test_toggles.py test_output_text.py test_line_wait.py; do
                 if python "$t" > "$LOGS/test.log" 2>&1; then echo "$t: PASS"
                 else echo "$t: FAILED"; tail -n 15 "$LOGS/test.log"; exit 1; fi
             done ;;
    logs)    echo "== watchdog =="; tail -n 5 "$LOGS/watch.log" 2>/dev/null
             echo "== boot =="; tail -n 5 "$LOGS/boot.log" 2>/dev/null
             echo "== server =="; tail -n 25 "$LOGS/server.log" 2>/dev/null
             echo "== tunnel =="; tail -n 15 "$LOGS/tunnel.log" 2>/dev/null ;;
    revoke)  load_venv; cd "$CODE" && python rivia_auth.py revoke-all ;;
    setup)   load_venv; mkdir -p "$HOME/.rivia"; chmod 700 "$HOME/.rivia"
             [ -f "$HOME/.rivia/.env" ] && die "~/.rivia/.env already exists. Delete it first if you really want new secrets."
             umask 077; cd "$CODE" || exit 1
             if python rivia_auth.py setup > "$HOME/.rivia/.env.new"; then
                 mv "$HOME/.rivia/.env.new" "$HOME/.rivia/.env"
                 echo "Saved ~/.rivia/.env (RIVIA_JWT_KEY). Add the Google keys and RIVIA_LOGIN_EMAIL to it."
             else rm -f "$HOME/.rivia/.env.new"; die "setup failed; nothing saved"; fi ;;
    token)   load_venv; load_env; cd "$CODE" && python rivia_auth.py mint-token app "${2:-365}" ;;
    admin-token) load_venv; load_env; cd "$CODE" && python rivia_auth.py mint-token admin "${2:-7}" ;;
    sched)   sched "${@:2}" ;;
    drift)   load_venv; cd "$CODE" && python rivia_drift_weekly.py "${@:2}" ;;
    body)    load_venv; cd "$CODE"
             if [ "${2:-}" = "dials" ]; then python rivia_body_shadow.py
             elif [ "${2:-}" = "plan" ]; then python rivia_progress.py
             else python rivia_body_collect.py "${@:2}"; fi ;;
    trust)   load_venv; cd "$CODE" && python rivia_trust_shadow.py "${@:2}" ;;
    reading) load_venv; cd "$CODE" && python rivia_reading.py "${@:2}" ;;
    style)   load_venv; cd "$CODE" && python rivia_style.py "${@:2}" ;;
    appraisal) load_venv; cd "$CODE" && python rivia_appraisal.py "${@:2}" ;;
    install-cli) install_cli ;;
    *)       sed -n '2,51p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
