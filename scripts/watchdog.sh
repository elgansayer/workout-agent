#!/usr/bin/env bash
# watchdog.sh - external liveness guardian for the workout-agent swarm.
#
# Runs on a schedule (every 5 minutes via config/systemd/swarm-watchdog.timer,
# or a cron entry of your own). Checks that the swarm is actually alive and
# making progress, and self-heals if not. All alerts go out via Telegram if
# TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are set.

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/swarm-env.sh"

STATE_DIR="$HOME/.cache/workout_agent_swarm_watchdog"
mkdir -p "$STATE_DIR"

STALL_MINUTES="${WATCHDOG_STALL_MINUTES:-15}"
HEARTBEAT_FILE="/tmp/workout_agent_swarm_watchdog/heartbeat"
TMUX_SESSION="workout_agent_swarm"

log() { echo "[$(date -Iseconds)] $*"; }

send_telegram() {
  local msg="$1"
  [ -z "${TELEGRAM_BOT_TOKEN:-}" ] && return 0
  [ -z "${TELEGRAM_CHAT_ID:-}" ] && return 0
  curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d "chat_id=${TELEGRAM_CHAT_ID}" \
    -d "text=[watchdog] ${msg}" >/dev/null 2>&1 || true
}

recover_full_restart() {
  log "Performing full recovery restart"
  pkill -9 -f "swarmd.py" 2>/dev/null || true
  tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
  rm -f "$SWARM_LOCK_FILE"
  sleep 1
  cd "$SWARM_ROOT" || return 1
  tmux new-session -d -s "$TMUX_SESSION" "$(swarm_venv_python) $SWARM_ROOT/swarmd.py"
  send_telegram "Full recovery restart performed"
}

# --- 1. tmux session alive? ---
if ! tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
  log "tmux session '$TMUX_SESSION' is dead"
  recover_full_restart
  exit 0
fi

# --- 2. recent git commits? (only meaningful once the queue has real work) ---
cd "$SWARM_ROOT" || exit 1
last_commit_epoch=$(git log -1 --format=%ct 2>/dev/null || echo 0)
now_epoch=$(date +%s)
stall_seconds=$(( now_epoch - last_commit_epoch ))
stall_count_file="$STATE_DIR/commit_stall_count"
if [ "$stall_seconds" -gt $(( STALL_MINUTES * 60 )) ]; then
  count=$(( $(cat "$stall_count_file" 2>/dev/null || echo 0) + 1 ))
  echo "$count" > "$stall_count_file"
  log "No commit in ${STALL_MINUTES}m (streak: $count)"
  if [ "$count" -eq 1 ]; then
    send_telegram "No commit in ${STALL_MINUTES} minutes"
  fi
  if [ "$count" -ge 6 ]; then
    log "Commit stall persisted ~30m, forcing recovery"
    recover_full_restart
    echo 0 > "$stall_count_file"
  fi
else
  echo 0 > "$stall_count_file"
fi

# --- 3. heartbeat freshness ---
heartbeat_stall_file="$STATE_DIR/heartbeat_stall_count"
if [ -f "$HEARTBEAT_FILE" ]; then
  hb_epoch=$(stat -c %Y "$HEARTBEAT_FILE" 2>/dev/null || echo 0)
  hb_age=$(( now_epoch - hb_epoch ))
  if [ "$hb_age" -gt $(( STALL_MINUTES * 60 )) ]; then
    count=$(( $(cat "$heartbeat_stall_file" 2>/dev/null || echo 0) + 1 ))
    echo "$count" > "$heartbeat_stall_file"
    log "Heartbeat stale (${hb_age}s old, streak: $count)"
    if [ "$count" -ge 3 ]; then
      log "Heartbeat stall persisted, forcing recovery"
      recover_full_restart
      echo 0 > "$heartbeat_stall_file"
    fi
  else
    echo 0 > "$heartbeat_stall_file"
  fi
else
  log "No heartbeat file yet (swarm may still be starting)"
fi

# --- 4. disk space ---
disk_pct=$(df -P "$SWARM_ROOT" | awk 'NR==2 {gsub("%","",$5); print $5}')
if [ "${disk_pct:-0}" -ge 85 ]; then
  log "Disk usage at ${disk_pct}%, cleaning caches"
  rm -rf /tmp/aider_* "$SWARM_ROOT/.pytest_cache" "$SWARM_ROOT/__pycache__" 2>/dev/null || true
  find "$SWARM_ROOT" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
fi

# --- 5. memory pressure ---
mem_pct=$(free | awk '/Mem:/ {printf "%d", $3/$2*100}')
if [ "${mem_pct:-0}" -ge 95 ]; then
  send_telegram "CRITICAL: memory at ${mem_pct}%"
elif [ "${mem_pct:-0}" -ge 90 ]; then
  log "Memory at ${mem_pct}%, killing stray aider/claude/agy processes"
  pkill -9 -f "aider" 2>/dev/null || true
fi

log "Watchdog check complete (disk=${disk_pct}% mem=${mem_pct}%)"
