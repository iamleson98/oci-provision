#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# doctor.sh - health check for the runner worker VM. Run ON the VM:
#   ssh ubuntu@<a1-ip> 'bash /opt/oci-runner/scripts/doctor.sh'
# or from your laptop:  make doctor
# ---------------------------------------------------------------------------
set -uo pipefail

PASS=0; WARN=0; FAIL=0
ok()   { printf '  \033[1;32m[ ok ]\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
warn() { printf '  \033[1;33m[warn]\033[0m %s\n' "$*"; WARN=$((WARN+1)); }
bad()  { printf '  \033[1;31m[fail]\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }

echo "== oci-github-runner doctor =="

echo "-- system --"
[[ $(id -u) -eq 0 ]] || { echo "run with sudo for full checks"; SUDO=sudo; } 
SUDO="${SUDO:-sudo}"
UP=$(uptime -p 2>/dev/null); ok "uptime: ${UP:-?}"
MEM=$(free -h | awk '/Mem:/{print $3"/"$2}'); ok "memory used: ${MEM}"
SWAP=$(free -h | awk '/Swap:/{print $2}'); [[ "${SWAP:-0B}" != "0B" ]] && ok "swap: ${SWAP}" || warn "no swap configured (cloud-init should add 4 GB)"
DISK=$(df -h / | awk 'NR==2{print $4" free ("$5" used)"}'); ok "root fs: ${DISK}"
DISKP=$(df -P / | awk 'NR==2{gsub("%","",$5); print $5}')
(( ${DISKP:-0} > 85 )) && bad "disk usage ${DISKP}% - run: docker system prune -af --filter 'label!=oci-runner.keep'" || ok "disk usage ${DISKP}%"

echo "-- docker --"
docker info >/dev/null 2>&1 && ok "docker daemon reachable" || bad "docker daemon unreachable"
RUNNERS=$(docker ps --filter label=oci-runner.managed=1 -q | wc -l)
ok "runner containers running: ${RUNNERS}"
IMG=$(docker images oci-runner/runner -q | wc -l)
[[ "$IMG" -gt 0 ]] && ok "runner image built" || bad "runner image missing (deploy/install.sh)"
CTRL=$(docker ps --filter name=oci-runner-controller --format '{{.Status}}')
[[ -n "$CTRL" ]] && ok "controller container: ${CTRL}" || bad "controller container not running"
DOCKER_DF=$(docker system df 2>/dev/null | awk 'NR==2{print $0}')
[[ -n "$DOCKER_DF" ]] && ok "docker usage: $(echo "$DOCKER_DF" | cut -c1-60)..."

echo "-- controller --"
HEALTH=$(curl -fsS -m 5 http://127.0.0.1:8080/healthz 2>/dev/null || echo unreachable)
[[ "$HEALTH" == "ok" ]] && ok "controller healthz: ok" || bad "controller healthz: ${HEALTH}"
if command -v jq >/dev/null; then
  STATE=$(curl -fsS -m 5 http://127.0.0.1:8080/api/status 2>/dev/null || echo '{}')
  QUEUED=$(jq -r '[.queued[]] | add // 0' <<<"$STATE")
  BUSY=$(jq -r '.runners_busy // 0' <<<"$STATE")
  DESIRED=$(jq -r '.desired // 0' <<<"$STATE")
  ok "autoscaler: desired=${DESIRED} busy=${BUSY} queued=${QUEUED}"
  AUTH=$(jq -r '.auth_mode // "?"' <<<"$STATE"); ok "auth mode: ${AUTH}"
fi

echo "-- security --"
$SUDO ufw status 2>/dev/null | grep -q "Status: active" && ok "ufw active" || warn "ufw not active"
$SUDO systemctl is-active fail2ban >/dev/null 2>&1 && ok "fail2ban active" || warn "fail2ban inactive"
$SUDO grep -q "^PasswordAuthentication no" /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null \
  && ok "ssh password auth disabled" || warn "verify SSH is key-only"
[[ -f /etc/apt/apt.conf.d/20auto-upgrades ]] && ok "unattended-upgrades present" || warn "unattended-upgrades missing"

echo "-- binfmt (multi-arch docker builds) --"
if ls /proc/sys/fs/binfmt_misc/qemu-x86_64 >/dev/null 2>&1; then
  ok "qemu-user-static registered (amd64 emulation available)"
else
  warn "no qemu binfmt: `docker build --platform linux/amd64` will fail (sudo apt install qemu-user-static binfmt-support)"
fi

echo
echo "summary: ${PASS} ok, ${WARN} warnings, ${FAIL} failures"
[[ $FAIL -eq 0 ]]
