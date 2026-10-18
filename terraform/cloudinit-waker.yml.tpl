#cloud-config
# Bootstrap for the waker VM (free AMD E2.1.Micro, Ubuntu 24.04 amd64).
# Deliberately minimal: no Docker, tiny footprint. The waker service is
# installed later by `make deploy-waker`.

package_update: true
package_upgrade: true

packages:
  - curl
  - jq
  - python3
  - python3-venv
  - python3-pip
  - ufw
  - unattended-upgrades
  - ca-certificates

runcmd:
  - ufw default deny incoming
  - ufw default allow outgoing
  - ufw allow 22/tcp comment 'SSH'
  - ufw --force enable
  - systemctl enable --now unattended-upgrades

final_message: "oci-github-runner waker bootstrap done after $UPTIME seconds"
