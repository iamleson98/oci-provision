terraform {
  # >= 1.12 required for the native `oci` backend (state + locking in OCI
  # Object Storage). GitHub Actions installs via setup-terraform (~> 1.16.0).
  required_version = ">= 1.12.0"

  required_providers {
    oci = {
      source  = "oracle/oci"
      version = ">= 5.0, < 6.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = ">= 4.0"
    }
  }

  # Remote state in OCI Object Storage so GitHub Actions can provision and
  # roll back with no laptop involved. All values are injected at
  # `terraform init` time by scripts/tf_init.sh (GitHub secrets -> env):
  #   bucket / key / namespace / region / tenancy_ocid / user_ocid /
  #   fingerprint / private_key_path / auth=APIKey
  # State locking is built in (lock object in the same bucket).
  # The bootstrap workflow creates the bucket with versioning enabled.
  backend "oci" {}
}

provider "oci" {
  auth             = var.auth
  region           = var.region
  tenancy_ocid     = var.tenancy_ocid
  user_ocid        = var.user_ocid
  fingerprint      = var.fingerprint
  private_key      = var.private_key != "" ? var.private_key : null
  private_key_path = var.private_key == "" && var.private_key_path != "" ? var.private_key_path : null
}
