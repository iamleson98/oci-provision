#!/usr/bin/env bash
# Deploy the runner stack onto already-provisioned VMs. Designed to run inside
# GitHub Actions (provision.yml / deploy-stack.yml - see docs/CI_DEPLOYMENT.md)
# but works from any machine that provides the same environment variables.
#
# Required env:
#   A1_IP               public IP of the A1 worker VM
#   RUNNER_GITHUB_PAT   PAT the controller uses to register runners
#                       (classic PAT with `repo` scope)
#   GITHUB_REPOSITORIES "owner/repo[,owner/repo2]"
# Optional env:
#   WAKER_IP            public IP of the waker VM (waker is skipped when unset)
#   A1_INSTANCE_ID      OCID of the A1 worker (required for the waker env)
#   RUNNER_PROFILE      always-on | burst (auto-detected from the VM when unset)
#   SSH_PRIVATE_KEY     private key content; when unset, the terraform-generated
#                       key is read from state (`terraform output -raw
#                       ssh_private_key_pem`; requires scripts/tf_init.sh to
#                       have been run in this repo checkout)
#   MIN_RUNNERS, MAX_RUNNERS, RUNNER_MEMORY_GB, RUNNER_LABELS,
#   WAKER_STATUS_TOKEN
set -euo pipefail

A1_IP="${A1_IP:?A1_IP env required}"
RUNNER_GITHUB_PAT="${RUNNER_GITHUB_PAT:?RUNNER_GITHUB_PAT env required}"
GITHUB_REPOSITORIES="${GITHUB_REPOSITORIES:?GITHUB_REPOSITORIES env required}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TF_DIR="$REPO_ROOT/terraform"

# ------------------------------------------------------------------ ssh key
if [ -z "${SSH_PRIVATE_KEY:-}" ]; then
  echo "[deploy] SSH_PRIVATE_KEY not set - using the terraform-generated key from state"
  SSH_PRIVATE_KEY="$(terraform -ch-dir="$TF_DIR" output -raw ssh_private_key_pem)"
fi
if [ -z "$SSH_PRIVATE_KEY" ]; then
  echo "::error::no SSH private key available (state has none because a custom key was provided, and SSH_PRIVATE_KEY is empty)" >&2
  exit 1
fi
KEYFILE="$(mktemp)"
trap 'rm -f "$KEYFILE"' EXIT
printf '%s\n' "${SSH_PRIVATE_KEY//\\n/$'\n'}" > "$KEYFILE"
chmod 600 "$KEYFILE"

ssh_opts=(-i "$KEYFILE" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20)
rsync_e="ssh -i $KEYFILE -o StrictHostKeyChecking=accept-new"
RSYNC_EXCLUDES=(
  --exclude .git --exclude .github --exclude .terraform
  --exclude 'terraform.tfstate*' --exclude hosts.mk
  --exclude '.env' --exclude 'secrets' --exclude 'waker.env'
)

wait_cloudinit() {
  local ip="$1" i
  for i in $(seq 1 40); do
    if ssh "${ssh_opts[@]}" -o ConnectTimeout=10 "ubuntu@$ip" true 2>/dev/null; then
      echo "[deploy] $ip reachable - waiting for cloud-init to finish..."
      ssh "${ssh_opts[@]}" "ubuntu@$ip" 'cloud-init status --wait' >/dev/null
      return 0
    fi
    sleep 15
  done
  echo "::error::could not reach ubuntu@$ip via SSH within 10 minutes" >&2
  return 1
}

# ------------------------------------------------------------- profile + knobs
PROFILE="${RUNNER_PROFILE:-}"
if [ -z "$PROFILE" ]; then
  PROFILE="$(ssh "${ssh_opts[@]}" "ubuntu@$A1_IP" \
    "awk -F= '/^RUNNER_PROFILE=/{print \$2}' /etc/oci-runner/profile 2>/dev/null" || true)"
  PROFILE="${PROFILE:-always-on}"
fi
echo "[deploy] runner profile: $PROFILE"

MIN_RUNNERS="${MIN_RUNNERS:-1}"
MAX_RUNNERS="${MAX_RUNNERS:-}"
if [ -z "$MAX_RUNNERS" ]; then
  if [ "$PROFILE" = "burst" ]; then MAX_RUNNERS=4; else MAX_RUNNERS=2; fi
fi
MEM_GB="${RUNNER_MEMORY_GB:-5}"
if [ "$PROFILE" = "burst" ]; then
  ENABLE_IDLE_STOP=true
  ALWAYS_RESUME=false
else
  ENABLE_IDLE_STOP=false
  ALWAYS_RESUME=true   # self-heal: restart the A1 if Oracle stops it (idle reclaim, crash)
fi
LABELS="${RUNNER_LABELS:-oracle-a1}"

# ------------------------------------------------------- A1 worker: controller
echo "[deploy] === A1 worker ($A1_IP) ==="
wait_cloudinit "$A1_IP"

ssh "${ssh_opts[@]}" "ubuntu@$A1_IP" \
  'sudo mkdir -p /opt/oci-runner && sudo chown ubuntu /opt/oci-runner'
rsync -az --delete -e "$rsync_e" "${RSYNC_EXCLUDES[@]}" \
  "$REPO_ROOT/" "ubuntu@$A1_IP:/opt/oci-runner/"

ssh "${ssh_opts[@]}" "ubuntu@$A1_IP" 'sudo tee /opt/oci-runner/.env >/dev/null' <<EOF
# Managed by scripts/ci_deploy.sh - re-run the Deploy-stack workflow to change
AUTH_MODE=pat
GITHUB_PAT=$RUNNER_GITHUB_PAT
SCOPE_TYPE=repo
GITHUB_REPOSITORIES=$GITHUB_REPOSITORIES
RUNNER_LABELS=$LABELS
MATCH_LABELS=$LABELS
MIN_RUNNERS=$MIN_RUNNERS
MAX_RUNNERS=$MAX_RUNNERS
RUNNER_MEMORY_GB=$MEM_GB
RUNNER_IMAGE=oci-runner/runner:latest
MOUNT_DOCKER_SOCK=true
CONTAINER_PREFIX=gha-runner
POLL_INTERVAL=15
ENABLE_IDLE_STOP=$ENABLE_IDLE_STOP
IDLE_STOP_MINUTES=20
STATUS_BIND=0.0.0.0
STATUS_PORT=8080
EOF
ssh "${ssh_opts[@]}" "ubuntu@$A1_IP" \
  'sudo chmod 600 /opt/oci-runner/.env && sudo bash /opt/oci-runner/deploy/install.sh controller'

# ----------------------------------------------------------- waker (AMD micro)
WAKER_IP="${WAKER_IP:-}"
if [ -n "$WAKER_IP" ] && [ "$WAKER_IP" != "None" ] && [ -n "${A1_INSTANCE_ID:-}" ]; then
  echo "[deploy] === waker ($WAKER_IP) ==="
  wait_cloudinit "$WAKER_IP"

  ssh "${ssh_opts[@]}" "ubuntu@$WAKER_IP" \
    'sudo mkdir -p /opt/oci-runner-waker && sudo chown ubuntu /opt/oci-runner-waker'
  rsync -az --delete -e "$rsync_e" "${RSYNC_EXCLUDES[@]}" \
    "$REPO_ROOT/" "ubuntu@$WAKER_IP:/opt/oci-runner-waker/"

  ssh "${ssh_opts[@]}" "ubuntu@$WAKER_IP" 'sudo tee /opt/oci-runner-waker/waker.env >/dev/null' <<EOF
# Managed by scripts/ci_deploy.sh
AUTH_MODE=pat
GITHUB_PAT=$RUNNER_GITHUB_PAT
SCOPE_TYPE=repo
GITHUB_REPOSITORIES=$GITHUB_REPOSITORIES
MATCH_LABELS=$LABELS
A1_INSTANCE_ID=$A1_INSTANCE_ID
ALWAYS_RESUME=$ALWAYS_RESUME
WAKER_POLL_INTERVAL=20
STATUS_BIND=0.0.0.0
STATUS_PORT=8080
STATUS_TOKEN=${WAKER_STATUS_TOKEN:-}
EOF
  ssh "${ssh_opts[@]}" "ubuntu@$WAKER_IP" \
    'sudo chmod 600 /opt/oci-runner-waker/waker.env && sudo bash /opt/oci-runner-waker/deploy/install.sh waker'
else
  echo "[deploy] waker disabled (WAKER_IP or A1_INSTANCE_ID missing) - skipped"
fi

echo "[deploy] done: profile=$PROFILE pool=${MIN_RUNNERS}-${MAX_RUNNERS} repos=$GITHUB_REPOSITORIES labels=$LABELS"
