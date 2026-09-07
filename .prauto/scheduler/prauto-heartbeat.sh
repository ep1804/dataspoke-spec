#!/usr/bin/env bash
# DataSpoke PRauto — hourly heartbeat tick (Hermes cron, no-agent mode).
#
# Runs with the cron job's `workdir` (the repo checkout) as its cwd. Detaches the
# executor and returns immediately so the cron tick never trips the script timeout
# (the executor can dispatch a coding agent for well over an hour). The executor
# owns its own PID lock + idempotency, so overlapping hourly ticks are safe.
# Output is appended to the gitignored heartbeat log; the executor also
# self-reports via GitHub (SSOT).
#
# Canonical source: .prauto/scheduler/prauto-heartbeat.sh
# Installed copy:   $HERMES_HOME/scripts/prauto-heartbeat.sh  (Hermes cron only
#                   runs scripts that resolve inside $HERMES_HOME/scripts/).
set -euo pipefail

REPO="$(pwd -P)"
LOG="$REPO/.prauto/state/heartbeat_cron.log"

# GUI-launched gateways often inherit a minimal PATH; make the CLIs the executor
# needs (gh, git, jq, claude, codex) resolvable regardless of how the gateway
# process was launched.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

nohup bash "$REPO/.prauto/heartbeat.sh" >>"$LOG" 2>&1 &
printf 'prauto heartbeat detached (pid %s) → %s\n' "$!" "$LOG"
