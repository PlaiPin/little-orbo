#!/usr/bin/env bash
set -eo pipefail
: "${K1_ROS_SETUP:?Set the actual vendor ROS setup path}"
: "${K1_INSTALL:?Set the installed integration directory}"
: "${K1_STATE:?Set an owner-private persistent state directory}"
source "$K1_ROS_SETUP"
exec "$K1_INSTALL/.venv/bin/python" "$K1_INSTALL/worker.py" \
  --config "$K1_STATE/worker.json" --socket "$K1_STATE/worker.sock" \
  --database "$K1_STATE/commands.db"
