#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_FILE="${REMOTE_CONFIG:-"$ROOT/config/remote.env"}"

if [[ -f "$CONFIG_FILE" ]]; then
  # shellcheck source=/dev/null
  source "$CONFIG_FILE"
fi

REMOTE_HOST="${REMOTE_HOST:-}"
REMOTE_PROJECT_DIR="${REMOTE_PROJECT_DIR:-}"
REMOTE_PYTHON="${REMOTE_PYTHON:-python3}"
REMOTE_VENV="${REMOTE_VENV:-}"
OPT_SPACE="${OPT_SPACE:-config/optimization.default.json}"
OPT_STAGE="${OPT_STAGE:-vol_timing}"
OPT_STUDY_ID="${OPT_STUDY_ID:-remote_vol_timing}"
OPT_MAX_TRIALS="${OPT_MAX_TRIALS:-}"
SYNC_DATA="${SYNC_DATA:-0}"
SYNC_RESULTS="${SYNC_RESULTS:-0}"
SSH_OPTS="${SSH_OPTS:-}"
RSYNC_OPTS="${RSYNC_OPTS:--az --delete}"

usage() {
  cat <<'EOF'
Usage:
  scripts/remote_optimization.sh sync
  scripts/remote_optimization.sh smoke
  scripts/remote_optimization.sh run
  scripts/remote_optimization.sh status
  scripts/remote_optimization.sh fetch

Configuration:
  Copy config/remote.example.env to config/remote.env and set REMOTE_HOST
  and REMOTE_PROJECT_DIR, or provide those variables in the environment.

Common overrides:
  OPT_STAGE=stage_name OPT_STUDY_ID=study_id scripts/remote_optimization.sh run
  OPT_MAX_TRIALS=5 scripts/remote_optimization.sh run
  REMOTE_CONFIG=config/remote.prod.env scripts/remote_optimization.sh sync
EOF
}

require_remote_config() {
  if [[ -z "$REMOTE_HOST" || -z "$REMOTE_PROJECT_DIR" ]]; then
    echo "REMOTE_HOST and REMOTE_PROJECT_DIR must be set. Copy config/remote.example.env to config/remote.env." >&2
    exit 2
  fi
}

split_words() {
  local raw="$1"
  # shellcheck disable=SC2206
  SPLIT_WORDS=($raw)
}

ssh_remote() {
  split_words "$SSH_OPTS"
  ssh "${SPLIT_WORDS[@]}" "$REMOTE_HOST" "$@"
}

remote_bash() {
  local command="$1"
  ssh_remote "bash -lc $(quote "$command")"
}

quote() {
  printf "%q" "$1"
}

remote_python_prefix() {
  local command="cd $(quote "$REMOTE_PROJECT_DIR")"
  if [[ -n "$REMOTE_VENV" ]]; then
    if [[ "$REMOTE_VENV" = /* ]]; then
      command="$command && source $(quote "$REMOTE_VENV/bin/activate")"
    else
      command="$command && source $(quote "$REMOTE_PROJECT_DIR/$REMOTE_VENV/bin/activate")"
    fi
  fi
  printf "%s" "$command"
}

sync_project() {
  require_remote_config
  remote_bash "mkdir -p $(quote "$REMOTE_PROJECT_DIR")"

  local excludes=(
    "--exclude=.git/"
    "--exclude=.venv/"
    "--exclude=__pycache__/"
    "--exclude=.pytest_cache/"
    "--exclude=*.pyc"
    "--exclude=*.egg-info/"
    "--exclude=dist/"
    "--exclude=build/"
    "--exclude=.claude/"
    "--exclude=.codex/"
    "--exclude=config/remote.env"
  )
  if [[ "$SYNC_DATA" != "1" ]]; then
    excludes+=("--exclude=/data/")
  fi
  if [[ "$SYNC_RESULTS" != "1" ]]; then
    excludes+=("--exclude=/results/")
  fi

  split_words "$RSYNC_OPTS"
  rsync "${SPLIT_WORDS[@]}" "${excludes[@]}" "$ROOT/" "$REMOTE_HOST:$REMOTE_PROJECT_DIR/"
}

optimization_args() {
  local args=(
    "scripts/run_optimization.py"
    "--space" "$OPT_SPACE"
    "--stage" "$OPT_STAGE"
    "--study-id" "$OPT_STUDY_ID"
  )
  if [[ -n "$OPT_MAX_TRIALS" ]]; then
    args+=("--max-trials" "$OPT_MAX_TRIALS")
  fi
  printf "%q " "${args[@]}"
}

run_remote() {
  require_remote_config
  local log_dir="logs"
  local log_file="$log_dir/optimization_${OPT_STUDY_ID}.log"
  local prefix
  prefix="$(remote_python_prefix)"
  local args
  args="$(optimization_args)"
  remote_bash "$prefix && mkdir -p $(quote "$log_dir") && (nohup $(quote "$REMOTE_PYTHON") $args > $(quote "$log_file") 2>&1 < /dev/null & echo remote_pid=\$!) && echo log=$(quote "$REMOTE_PROJECT_DIR/$log_file")"
}

status_remote() {
  require_remote_config
  local prefix
  prefix="$(remote_python_prefix)"
  local log_file="logs/optimization_${OPT_STUDY_ID}.log"
  remote_bash "$prefix && if [[ -f $(quote "$log_file") ]]; then tail -n 80 $(quote "$log_file"); else echo $(quote "No log file yet: $log_file"); fi"
}

fetch_results() {
  require_remote_config
  mkdir -p "$ROOT/results/optimization"
  split_words "$RSYNC_OPTS"
  rsync "${SPLIT_WORDS[@]}" "$REMOTE_HOST:$REMOTE_PROJECT_DIR/results/optimization/$OPT_STUDY_ID/" "$ROOT/results/optimization/$OPT_STUDY_ID/"
}

action="${1:-}"
case "$action" in
  sync)
    sync_project
    ;;
  smoke)
    sync_project
    OPT_MAX_TRIALS="${OPT_MAX_TRIALS:-1}"
    run_remote
    ;;
  run)
    sync_project
    run_remote
    ;;
  status)
    status_remote
    ;;
  fetch)
    fetch_results
    ;;
  ""|-h|--help|help)
    usage
    ;;
  *)
    echo "Unknown action: $action" >&2
    usage >&2
    exit 2
    ;;
esac
