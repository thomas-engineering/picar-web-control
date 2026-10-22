#!/usr/bin/env bash
# Sample the Pi for N seconds and print one summary: CPU load, the server's CPU use, the data
# waiting in the server's sockets, throttling flags, new undervoltage events and TCP
# retransmissions. Not meant to be run by hand: lag_test.py sends it over ssh.
#   ssh <pi> 'bash -s -- 30' < tools/pi_probe.sh
set -u
N=${1:-30}
PID=$(pgrep -f "[s]erver.run" | head -1)
U0=$(sudo -n dmesg 2>/dev/null | grep -c Undervoltage)
R0=$(awk '/^Tcp:/{getline; print $13}' /proc/net/snmp)
cpu=$(mktemp); queue=$(mktemp); flags=$(mktemp)
cpu_times() { awk '/^cpu /{print $2+$3+$4, $2+$3+$4+$5+$6+$7+$8}' /proc/stat; }
for i in $(seq 1 "$N"); do
    a=($(cpu_times)); p1=$(awk '{print $14+$15}' "/proc/$PID/stat")
    sleep 1
    b=($(cpu_times)); p2=$(awk '{print $14+$15}' "/proc/$PID/stat")
    echo "$((100 * (b[0] - a[0]) / (b[1] - a[1]))) $((p2 - p1))" >> "$cpu"
    # Send-Q: bytes the kernel still holds for the client, a sign of a backed-up link
    ss -tn state established '( sport = :8000 )' | awk 'NR>1{print $2}' | sort -n | tail -1 >> "$queue"
    [ $((i % 5)) -eq 0 ] && vcgencmd get_throttled | cut -d= -f2 >> "$flags"
done
awk '{s+=$1; if($1>m)m=$1; p+=$2; if($2>pm)pm=$2; n++}
     END{printf "total CPU avg %.0f%% max %d%% | server avg %.0f%% of one core max %d%%\n", s/n, m, p/n, pm}' "$cpu"
awk '{s+=$1; if($1>m)m=$1; n++} END{printf "socket Send-Q avg %.0f KB max %.0f KB\n", s/n/1024, m/1024}' "$queue"
echo "throttled flags: $(sort -u "$flags" | tr '\n' ' ')| new undervoltage events: $(( $(sudo -n dmesg 2>/dev/null | grep -c Undervoltage) - U0 ))"
echo "TCP segments retransmitted: $(( $(awk '/^Tcp:/{getline; print $13}' /proc/net/snmp) - R0 ))"
rm -f "$cpu" "$queue" "$flags"
