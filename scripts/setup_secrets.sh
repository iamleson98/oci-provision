#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# setup_secrets.sh - write GitHub credentials to both VMs in one shot.
#
# Usage:
#   make setup-secrets                                    # interactive prompts
#   ./scripts/setup_secrets.sh --host A.B.C.D --waker-host E.F.G.H \
#       --pat ghp_xxx --repos myuser/myrepo[,org/repo2]
#
# Non-interactive flags override the prompts. Files written:
#   A1 worker : /opt/oci-runner/.env
#   waker     : /opt/oci-runner-waker/waker.env
# ---------------------------------------------------------------------------
set -euo pipefail

HOST=""
WAKER_HOST=""
PAT=""
REPOS=""
ORG=""
APP_ID=""
APP_INSTALL_ID=""
APP_KEY=""
SSH_OPTS="${SSH_OPTS:--o StrictHostKeyChecking=accept-new}"
KEY="${KEY:-}"

log()  { printf '\033[1;36m[secrets]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[secrets]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[secrets]\033[0m %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --waker-host) WAKER_HOST="$2"; shift 2 ;;
    --pat) PAT="$2"; shift 2 ;;
    --repos) REPOS="$2"; shift 2 ;;
    --org) ORG="$2"; shift 2 ;;
    --app-id) APP_ID="$2"; shift 2 ;;
    --app-install-id) APP_INSTALL_ID="$2"; shift 2 ;;
    --app-key) APP_KEY="$2"; shift 2 ;;
    --ssh-key) KEY="$2"; shift 2 ;;
    *) die "unknown option $1" ;;
  esac
done

[[ -n "$HOST" ]] || die "A1 host required (--host or run via make)"
[[ -n "$PAT" ]] || { read -rsp "GitHub PAT (classic, 'repo' scope): " PAT; echo; }
if [[ -z "$REPOS" && -z "$ORG" ]]; then
  read -rp "Repositories to serve (owner/repo, comma separated): " REPOS
fi
if [[ -z "$REPOS" && -z "$ORG" ]]; then
  read -rp "...or organization name (blank if none): " ORG
fi
[[ -n "$REPOS" || -n "$ORG" ]] || die "need either --repos or --org"

SSH() {
  local h="$1"; shift
  if [[ -n "$KEY" ]]; then ssh $SSH_OPTS -i "$KEY" "ubuntu@$h" "$@"; else ssh $SSH_OPTS "ubuntu@$h" "$@"; fi
}

render_env() {
  if [[ -n "$ORG" ]]; then
    SCOPE_BLOCK="SCOPE_TYPE=org
GITHUB_ORGANIZATION=${ORG}"
  else
    SCOPE_BLOCK="SCOPE_TYPE=repo
GITHUB_REPOSITORIES=${REPOS}"
  fi
  cat <<EOF
AUTH_MODE=pat
GITHUB_PAT=${PAT}
${SCOPE_BLOCK}
RUNNER_LABELS=oracle-a1
MATCH_LABELS=oracle-a1
EOF
}

log "writing /opt/oci-runner/.env on the A1 worker ($HOST)..."
ENV_BLOCK="$(render_env)"
# Merge into any existing .env: drop the keys we own, keep user's other knobs
# (MAX_RUNNERS, RUNNER_MEMORY_GB, ...), then append fresh values.
SSH "$HOST" "sudo mkdir -p /opt/oci-runner && \
  sed -i -E '/^(AUTH_MODE|GITHUB_PAT|SCOPE_TYPE|GITHUB_REPOSITORIES|GITHUB_ORGANIZATION|RUNNER_LABELS|MATCH_LABELS)=/d' /opt/oci-runner/.env 2>/dev/null || true; \
  cat >> /opt/oci-runner/.env && chmod 600 /opt/oci-runner/.env" <<<"$ENV_BLOCK"
SSH "$HOST" "sudo systemctl restart oci-runner.service && sleep 3 && sudo docker ps --filter name=oci-runner-controller --format '{{.Status}}'"

log "controller status above (should be Up). Logs: ssh ubuntu@$HOST 'docker logs -f oci-runner-controller'"

if [[ -n "$WAKER_HOST" ]]; then
  log "writing waker env on $WAKER_HOST..."
  A1_ID="$(SSH "$HOST" "curl -s -H 'Authorization: Bearer Oracle' -H 'X-oracle-identity-authorization: Oracle' http://169.254.169.254/opc/v2/instance/id" 2>/dev/null || true)"
  if [[ -z "$A1_ID" || "$A1_ID" != ocid1.instance.* ]]; then
    warn "could not auto-detect the A1 instance OCID from metadata; leaving placeholder"
    A1_ID="ocid1.instance.FILL_ME_IN"
  fi
  WAKER_ENV="AUTH_MODE=pat
GITHUB_PAT=${PAT}
$( [[ -n "$ORG" ]] && echo "SCOPE_TYPE=org
GITHUB_ORGANIZATION=${ORG}" || echo "SCOPE_TYPE=repo
GITHUB_REPOSITORIES=${REPOS}" )
MATCH_LABELS=oracle-a1
A1_INSTANCE_ID=${A1_ID}
ALWAYS_RESUME=${WAKER_ALWAYS_RESUME:-false}
STATUS_BIND=0.0.0.0
STATUS_PORT=8080
STATUS_TOKEN=${WAKER_STATUS_TOKEN:-}"
  SSH "$WAKER_HOST" "sudo mkdir -p /opt/oci-runner-waker && sudo tee /opt/oci-runner-waker/waker.env >/dev/null && sudo chmod 600 /opt/oci-runner-waker/waker.env" <<<"$WAKER_ENV"
  SSH "$WAKER_HOST" "sudo systemctl restart oci-waker.service && sleep 2 && systemctl is-active oci-waker.service"
  log "waker status above. Logs: ssh ubuntu@$WAKER_HOST 'journalctl -u oci-waker -f'"
fi

log "done. Trigger a workflow in one of the repos and watch:"
log "  ssh ubuntu@$HOST 'docker ps --filter label=oci-runner.managed=1'"
