#!/usr/bin/env bash
# Ephemeral runner entrypoint: register -> run exactly ONE job -> exit.
# The controller (or plain `docker run`) provides:
#   RUNNER_URL    https://github.com/owner/repo | https://github.com/orgs/org
#   RUNNER_TOKEN  registration token (valid ~1 h, one per registration)
#   RUNNER_LABELS custom labels, comma separated (default: oracle-a1)
#   RUNNER_NAME   unique runner name (default: container hostname)
#   RUNNER_GROUP  optional runner group name
set -euo pipefail

: "${RUNNER_URL:?RUNNER_URL is required}"
: "${RUNNER_TOKEN:?RUNNER_TOKEN is required}"

LABELS="${RUNNER_LABELS:-oracle-a1}"
NAME="${RUNNER_NAME:-$(hostname)}"
WORK="/home/runner/_work"

group_args=()
if [[ -n "${RUNNER_GROUP:-}" ]]; then
  group_args=(--runnergroup "${RUNNER_GROUP}")
fi

echo "[entrypoint] registering ephemeral runner '${NAME}' at ${RUNNER_URL} (labels: ${LABELS})"
./config.sh --unattended \
  --url "${RUNNER_URL}" \
  --token "${RUNNER_TOKEN}" \
  --name "${NAME}" \
  --labels "${LABELS}" \
  --work "${WORK}" \
  --ephemeral \
  "${group_args[@]}"

# Ephemeral runners deregister themselves when the job completes; run.sh exits.
echo "[entrypoint] runner online - waiting for one job"
rc=0
./run.sh || rc=$?

echo "[entrypoint] runner finished with rc=${rc}"
# Defensive cleanup in case registration lingered (e.g. service-side hiccup).
./config.sh remove --token "${RUNNER_TOKEN}" >/dev/null 2>&1 || true
exit "${rc}"
