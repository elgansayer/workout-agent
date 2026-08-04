#!/usr/bin/env bash
# swarm-env.sh - shared environment setup for swarmctl and watchdog.sh.
# Sourced, not executed directly.

export SWARM_ROOT="${SWARM_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export PATH="$HOME/.local/bin:$PATH"

# aider is installed in an isolated `uv tool` Python 3.12 environment (this
# project's own venv is Python 3.14, which numpy/scipy/aider's dependency
# chain doesn't yet publish wheels for) - `uv tool install` already links
# its shim into ~/.local/bin, so no extra PATH entry is needed here, but we
# keep this comment so a future re-install doesn't "fix" it into the venv.

if [ -f "$SWARM_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$SWARM_ROOT/.env"
  set +a
fi

if [ -f "$SWARM_ROOT/.env.swarm" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$SWARM_ROOT/.env.swarm"
  set +a
fi

export SWARM_STATE_FILE="${SWARM_STATE_FILE:-/tmp/workout_agent_swarm_state.json}"
export SWARM_LOCK_FILE="${SWARM_LOCK_FILE:-/tmp/workout_agent_swarm_coordination.lock}"
export TASKS_ROOT="$SWARM_ROOT/.tasks"

swarm_task_stats() {
  for state in pending active stuck completed; do
    count=$(find "$TASKS_ROOT/$state" -maxdepth 1 -name '*.task' 2>/dev/null | wc -l)
    printf "%s=%s " "$state" "$count"
  done
  echo
}

swarm_venv_python() {
  if [ -x "$SWARM_ROOT/.venv/bin/python3" ]; then
    echo "$SWARM_ROOT/.venv/bin/python3"
  else
    command -v python3
  fi
}
