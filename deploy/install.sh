#!/usr/bin/env bash
# Install the oci-github-runner stack on a freshly bootstrapped VM.
#
# Usage (normally driven by `make deploy` / `make deploy-waker`):
#   sudo bash deploy/install.sh controller   # on the A1 worker
#   sudo bash deploy/install.sh waker        # on the AMD micro waker
#
# The repository must already be rsynced to:
#   controller -> /opt/oci-runner
#   waker      -> /opt/oci-runner-waker
set -euo pipefail

ROLE="${1:-}"
ROOT="/opt/oci-runner"
WAKER_ROOT="/opt/oci-runner-waker"

log()  { printf '\033[1;36m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[install]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[install]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root: sudo bash deploy/install.sh $ROLE"

case "$ROLE" in
  controller) ;;
  waker) ;;
  *) die "usage: sudo bash deploy/install.sh [controller|waker]" ;;
esac

install_controller() {
  command -v docker >/dev/null || die "docker not installed - cloud-init should have done this"
  cd "$ROOT"

  log "building runner image (native arch, pinned actions/runner version)..."
  # BuildKit auto-sets TARGETARCH, but pass it explicitly so even a legacy
  # builder picks the right agent (arm64 on A1, amd64 on x86_64 hosts).
  ARCH="$(dpkg --print-architecture 2>/dev/null || uname -m | sed -e 's/x86_64/amd64/' -e 's/aarch64/arm64/')"
  docker build -q --build-arg TARGETARCH="${ARCH}" -t oci-runner/runner:latest runner-image/

  log "building controller image..."
  docker compose -f deploy/compose.yml build controller

  mkdir -p "$ROOT/secrets" deploy
  touch "$ROOT/secrets/.gitkeep"

  if [[ ! -f "$ROOT/.env" ]]; then
    cp .env.example "$ROOT/.env"
    chmod 600 "$ROOT/.env"
    warn "created $ROOT/.env from the template - EDIT IT NOW (GITHUB_PAT / GITHUB_REPOSITORIES)."
  else
    log "keeping existing $ROOT/.env"
  fi

  # Profile written by cloud-init (always-on | burst)
  PROFILE="$(awk -F= '/^RUNNER_PROFILE=/{print $2}' /etc/oci-runner/profile 2>/dev/null || echo always-on)"
  log "runner profile: ${PROFILE}"

  if [[ "$PROFILE" == "burst" ]]; then
    mkdir -p /etc/oci-runner
    touch /etc/oci-runner/idle-stop.enabled
    install -m 644 deploy/idle-stop.service /etc/systemd/system/oci-runner-idle-stop.service
    install -m 644 deploy/idle-stop.timer /etc/systemd/system/oci-runner-idle-stop.timer
    systemctl daemon-reload
    systemctl enable --now oci-runner-idle-stop.timer
    log "burst profile: idle auto-poweroff timer enabled"
  else
    rm -f /etc/oci-runner/idle-stop.enabled /etc/systemd/system/oci-runner-idle-stop.{service,timer}
    systemctl disable --now oci-runner-idle-stop.timer 2>/dev/null || true
  fi

  install -m 644 deploy/oci-runner.service /etc/systemd/system/oci-runner.service
  systemctl daemon-reload
  systemctl enable oci-runner.service

  if grep -qE '^GITHUB_PAT=.+\S' "$ROOT/.env" 2>/dev/null \
     || grep -qE '^AUTH_MODE=app' "$ROOT/.env" 2>/dev/null; then
    systemctl restart oci-runner.service
    log "controller started (docker compose up -d --wait)"
  else
    warn "controller NOT started yet: fill in $ROOT/.env, then run:"
    warn "  sudo systemctl restart oci-runner.service"
  fi

  log "controller install done. Useful commands:"
  log "  docker logs -f oci-runner-controller     # controller logs"
  log "  docker ps --filter label=oci-runner.managed=1   # live runners"
  log "  ssh -N -L 8080:localhost:8080 \$(hostname -I|awk '{print \$1}')  # status page tunnel"
}

install_waker() {
  command -v python3 >/dev/null || die "python3 missing (cloud-init should have installed it)"
  mkdir -p "$WAKER_ROOT"
  # The whole repo is rsynced; we only need controller/ sources here.
  cd "$WAKER_ROOT"

  if [[ ! -d venv ]]; then
    log "creating python venv (this can take a couple of minutes on 1/8 OCPU)..."
    python3 -m venv venv
  fi
  log "installing python deps (oci, requests, pyjwt)..."
  ./venv/bin/pip install --quiet --upgrade pip >/dev/null 2>&1 || true
  ./venv/bin/pip install --quiet -r controller/waker-requirements.txt

  if [[ ! -f "$WAKER_ROOT/waker.env" ]]; then
    cp deploy/waker.env.example "$WAKER_ROOT/waker.env"
    chmod 600 "$WAKER_ROOT/waker.env"
    warn "created $WAKER_ROOT/waker.env from the template - EDIT IT NOW."
    warn "Required: A1_INSTANCE_ID (terraform output a1_instance_id) + GitHub credentials."
  else
    log "keeping existing $WAKER_ROOT/waker.env"
  fi

  install -m 644 deploy/waker.service /etc/systemd/system/oci-waker.service
  systemctl daemon-reload
  systemctl enable oci-waker.service

  if grep -qE '^A1_INSTANCE_ID=ocid1' "$WAKER_ROOT/waker.env" 2>/dev/null; then
    systemctl restart oci-waker.service
    log "waker started"
  else
    warn "waker NOT started yet: set A1_INSTANCE_ID in $WAKER_ROOT/waker.env, then:"
    warn "  sudo systemctl restart oci-waker.service"
  fi

  log "waker logs: journalctl -u oci-waker -f"
}

case "$ROLE" in
  controller) install_controller ;;
  waker)      install_waker ;;
esac
