#!/system/bin/sh
# RIVIA root supervisor (Magisk late-start service: /data/adb/service.d/rivia-supervisor.sh).
# Runs as root OUTSIDE Termux, so it survives Android killing Termux (force-stop, phantom
# process killer, low memory). Every 30s:
#   - if Termux's sshd or the rivia.sh watchdog is gone, ask Termux (RUN_COMMAND) to run
#     ~/bin/rivia-recover, which starts sshd + `rivia.sh start` (at most once per 2 min);
#   - if the Tailscale VPN has no 100.x address for 2 checks, ask Tailscale to connect.
# At boot it also re-applies the battery/background exemptions and phantom-killer off.
# Log: /data/adb/rivia-supervisor.log. Stop: kill "$(cat /data/adb/rivia-supervisor.pid)".
# Remove: delete this file (and the pid/log) and reboot.
T=/data/data/com.termux/files
LOG=/data/adb/rivia-supervisor.log
PIDF=/data/adb/rivia-supervisor.pid
[ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null && exit 0   # already running
(
    log() { echo "$(date '+%F %T') $*" >> "$LOG"; }
    until [ "$(getprop sys.boot_completed)" = 1 ]; do sleep 5; done
    TUID=$(stat -c %u "$T/home")
    log "supervisor up (pid $(cat "$PIDF"), termux uid $TUID)"
    for p in com.termux com.termux.boot com.tailscale.ipn; do
        dumpsys deviceidle whitelist +$p > /dev/null 2>&1
        cmd appops set $p RUN_ANY_IN_BACKGROUND allow 2> /dev/null
        cmd appops set $p RUN_IN_BACKGROUND allow 2> /dev/null
    done
    device_config set_sync_disabled_for_tests persistent 2> /dev/null
    device_config put activity_manager max_phantom_processes 2147483647 2> /dev/null
    settings put global settings_enable_monitor_phantom_procs false 2> /dev/null
    echo rivia_supervisor > /sys/power/wake_lock 2> /dev/null   # keep the CPU awake for these checks
    sleep 90   # give Termux:Boot the first go after a reboot
    last=0; tlast=0; tdown=0
    while true; do
        now=$(date +%s)
        s=up; w=up
        pgrep -u "$TUID" -x sshd > /dev/null || s=DOWN
        pgrep -u "$TUID" -f 'rivia.sh watch-loop' > /dev/null || w=DOWN
        if [ "$s$w" != upup ] && [ $((now - last)) -ge 120 ]; then
            log "sshd=$s watchdog=$w; asking Termux to run rivia-recover"
            am start-foreground-service --user 0 -n com.termux/com.termux.app.RunCommandService \
                -a com.termux.RUN_COMMAND --es com.termux.RUN_COMMAND_PATH "$T/home/bin/rivia-recover" \
                --es com.termux.RUN_COMMAND_WORKDIR "$T/home" --ez com.termux.RUN_COMMAND_BACKGROUND true \
                >> "$LOG" 2>&1
            last=$now
        fi
        if ip -4 addr show 2> /dev/null | grep -q 'inet 100\.'; then tdown=0; else tdown=$((tdown + 1)); fi
        if [ "$tdown" -ge 2 ] && [ $((now - tlast)) -ge 300 ]; then
            log "no Tailscale 100.x address; asking Tailscale to connect"
            am broadcast --user 0 -n com.tailscale.ipn/.IPNReceiver -a com.tailscale.ipn.CONNECT_VPN >> "$LOG" 2>&1
            tlast=$now
        fi
        [ "$(stat -c %s "$LOG" 2> /dev/null || echo 0)" -gt 262144 ] && mv -f "$LOG" "$LOG.1"
        sleep 30
    done
) < /dev/null > /dev/null 2>&1 &
echo $! > "$PIDF"
