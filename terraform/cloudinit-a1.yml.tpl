#cloud-config
# Bootstrap for the A1 GitHub Actions runner worker (Ubuntu 24.04 arm64).
# Rendered by terraform templatefile: ${runner_profile} is substituted at apply time.

package_update: true
package_upgrade: true

packages:
  - curl
  - jq
  - git
  - rsync
  - ufw
  - fail2ban
  - unattended-upgrades
  - qemu-user-static
  - binfmt-support
  - ca-certificates

write_files:
  - path: /etc/docker/daemon.json
    content: |
      {
        "log-driver": "json-file",
        "log-opts": { "max-size": "50m", "max-file": "3" },
        "live-restore": true
      }
  - path: /etc/oci-runner/profile
    permissions: "0644"
    content: |
      RUNNER_PROFILE=${runner_profile}
  - path: /etc/sysctl.d/90-oci-runner.conf
    content: |
      vm.swappiness = 10
      vm.vfs_cache_pressure = 50
  - path: /etc/fail2ban/jail.local
    content: |
      [sshd]
      enabled = true
      maxretry = 5
      bantime  = 3600

runcmd:
  # Docker (official script installs docker-ce, cli, buildx and compose plugins; arm64 native)
  - curl -fsSL https://get.docker.com | sh
  - systemctl enable --now docker

  # Swap as an OOM safety net (4 GB)
  - |
    if ! swapon --show | grep -q /swapfile; then
      fallocate -l 4G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=4096
      chmod 600 /swapfile
      mkswap /swapfile
      swapon /swapfile
      grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    fi
  - sysctl --system

  # Firewall: SSH only (OCI security list is a second layer)
  - ufw default deny incoming
  - ufw default allow outgoing
  - ufw allow 22/tcp comment 'SSH'
  - ufw --force enable

  # Unattended security upgrades
  - systemctl enable --now unattended-upgrades

  # Weekly deep clean of unused docker data (belt-and-braces next to the controller pruner)
  - |
    cat >/etc/cron.d/oci-runner-prune <<'EOF'
    0 4 * * 0 root /usr/bin/docker system prune -af --filter "label!=oci-runner.keep" --filter "until=168h" >/dev/null 2>&1
    EOF

final_message: "oci-github-runner A1 worker bootstrap done (${runner_profile} profile) after $UPTIME seconds"
