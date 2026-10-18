#!/usr/bin/env bash
# terraform init with the native `oci` backend (state + locking in OCI Object
# Storage). Designed for GitHub Actions; works from any machine with the same
# environment variables (docs/CI_DEPLOYMENT.md).
#
# Required env:
#   OCI_NAMESPACE    object storage namespace (printed by the Bootstrap workflow)
#   OCI_REGION       e.g. us-ashburn-1
#   OCI_TENANCY_OCID / OCI_USER_OCID / OCI_FINGERPRINT
#   OCI_PRIVATE_KEY      PEM content (literal \n escapes are fine) - or -
#   OCI_PRIVATE_KEY_FILE path to the PEM file
# Optional env:
#   TF_STATE_BUCKET  default: oci-provision-tfstate
#   TF_STATE_KEY     default: oci-provision.tfstate
set -euo pipefail

: "${OCI_NAMESPACE:?OCI_NAMESPACE env required (run the Bootstrap workflow first)}"
: "${OCI_REGION:?OCI_REGION env required}"
: "${OCI_TENANCY_OCID:?OCI_TENANCY_OCID env required}"
: "${OCI_USER_OCID:?OCI_USER_OCID env required}"
: "${OCI_FINGERPRINT:?OCI_FINGERPRINT env required}"

if [ -z "${OCI_PRIVATE_KEY_FILE:-}" ]; then
  : "${OCI_PRIVATE_KEY:?OCI_PRIVATE_KEY or OCI_PRIVATE_KEY_FILE env required}"
  OCI_PRIVATE_KEY_FILE="$(mktemp /tmp/oci_api_key.XXXXXX.pem)"
  printf '%s\n' "${OCI_PRIVATE_KEY//\\n/$'\n'}" > "$OCI_PRIVATE_KEY_FILE"
  chmod 600 "$OCI_PRIVATE_KEY_FILE"
fi

cd "$(dirname "$0")/../terraform"

terraform init -input=false -no-color \
  -backend-config="bucket=${TF_STATE_BUCKET:-oci-provision-tfstate}" \
  -backend-config="key=${TF_STATE_KEY:-oci-provision.tfstate}" \
  -backend-config="namespace=${OCI_NAMESPACE}" \
  -backend-config="region=${OCI_REGION}" \
  -backend-config="tenancy_ocid=${OCI_TENANCY_OCID}" \
  -backend-config="user_ocid=${OCI_USER_OCID}" \
  -backend-config="fingerprint=${OCI_FINGERPRINT}" \
  -backend-config="private_key_path=${OCI_PRIVATE_KEY_FILE}" \
  -backend-config="auth=APIKey"
