# ----------------------------------------------------------------------------
# Required
# ----------------------------------------------------------------------------
variable "region" {
  description = "OCI region, e.g. us-ashburn-1, eu-frankfurt-1, ap-singapore-1. IMPORTANT: your home region usually has the best Always Free A1 availability."
  type        = string
  default     = ""
}

variable "compartment_ocid" {
  description = "Compartment OCID for compute/network resources. Defaults to the tenancy root compartment when empty."
  type        = string
  default     = ""
}

# ----------------------------------------------------------------------------
# SSH access to the VMs
# ----------------------------------------------------------------------------
# Priority: ssh_public_key (content, CI-friendly) > ssh_public_key_path (local
# file) > auto-generated dedicated keypair (default in CI; the private key is
# exposed as the sensitive output `ssh_private_key_pem` and stored in the
# private, versioned state bucket).
variable "ssh_public_key" {
  description = "SSH public key CONTENT (e.g. ssh-ed25519 AAAA... comment). Takes precedence over ssh_public_key_path. Leave empty to let terraform generate a keypair."
  type        = string
  default     = ""
}

variable "ssh_public_key_path" {
  description = "Path to an SSH public key file on the machine running terraform (local usage). Ignored when ssh_public_key is set. When both are empty a keypair is generated."
  type        = string
  default     = ""
}

# ----------------------------------------------------------------------------
# Auth for the OCI provider (only needed when auth = "ApiKey")
# ----------------------------------------------------------------------------
variable "auth" {
  description = "OCI provider auth method: ApiKey | SecurityToken (Cloud Shell) | InstancePrincipal."
  type        = string
  default     = "ApiKey"
}

variable "tenancy_ocid" {
  type    = string
  default = ""
}

variable "user_ocid" {
  type    = string
  default = ""
}

variable "fingerprint" {
  type    = string
  default = ""
}

variable "private_key" {
  description = "OCI API signing key CONTENT (the PEM itself). Preferred in CI; takes precedence over private_key_path."
  type        = string
  default     = ""
}

variable "private_key_path" {
  description = "Path to the OCI API signing key PEM file (local usage)."
  type        = string
  default     = ""
}

# ----------------------------------------------------------------------------
# Runner worker VM (Ampere A1)
# ----------------------------------------------------------------------------
variable "runner_profile" {
  description = <<-EOT
    Cost profile of the A1 worker VM:

      always-on : 2 OCPU / 12 GB running 24/7. Fits inside the post-June-2026
                  Always Free allowance (744 h x 2 OCPU = 1488 <= 1500 OCPU-h)
                  on ANY account type. Zero billing risk. Recommended default.
      burst     : 4 OCPU / 24 GB, started/stopped on demand by the waker VM.
                  Uses at most ~375 h/month of runtime (1500 OCPU-h / 4), so
                  it stays free as long as the VM is only awake for CI.
                  Better throughput, ~2 min wake-up latency.
  EOT
  type        = string
  default     = "always-on"

  validation {
    condition     = contains(["always-on", "burst"], var.runner_profile)
    error_message = "runner_profile must be 'always-on' or 'burst'."
  }
}

variable "a1_ocpus" {
  description = "OCPUs for the A1 worker. Defaults are derived from runner_profile; override only if you know your account's limits (free-tier accounts: keep <= 2, or the instance gets stopped)."
  type        = number
  default     = 0 # 0 -> derive from profile
}

variable "a1_memory_gbs" {
  description = "Memory (GB) for the A1 worker. Defaults are derived from runner_profile."
  type        = number
  default     = 0 # 0 -> derive from profile
}

variable "a1_boot_volume_gb" {
  description = "Boot volume (GB) for the A1 worker. Always Free includes 200 GB of total block storage across the tenancy; the waker uses 50 GB (API minimum), so 147 GB uses the envelope to the fullest with a small safety margin (theoretical max: 150 GB)."
  type        = number
  default     = 147
}

variable "availability_domain_index" {
  description = "Index of the availability domain to place the A1 worker in (0-based). Change this if you hit 'Out of capacity'."
  type        = number
  default     = 0
}

variable "admin_cidr" {
  description = "CIDR allowed to reach SSH (and the status port when status_public=true). Restrict to your IP, e.g. '203.0.113.5/32'. '0.0.0.0/0' works but is discouraged (key-only auth + fail2ban compensate)."
  type        = string
  default     = "0.0.0.0/0"
}

# ----------------------------------------------------------------------------
# Waker VM (AMD micro) - optional but recommended
# ----------------------------------------------------------------------------
variable "enable_waker" {
  description = "Create the free AMD E2.1.Micro 'waker' VM: keeps a 24/7 status page, starts the A1 worker when jobs are queued (self-healing + burst mode)."
  type        = bool
  default     = true
}

variable "status_public" {
  description = "Expose the waker status page (port 8080) to admin_cidr instead of SSH-tunnel-only. The page is token-protected when STATUS_TOKEN is set."
  type        = bool
  default     = false
}

variable "create_waker_iam" {
  description = "Let Terraform create the Dynamic Group + Policy that lets the waker start/stop the A1 worker via instance principals. Requires admin rights (default on new accounts)."
  type        = bool
  default     = true
}

# ----------------------------------------------------------------------------
# Over-provision protection
# ----------------------------------------------------------------------------
variable "overprovision_max_ocpus" {
  description = <<-EOT
    Hard ceiling on TOTAL live Ampere A1 OCPUs allowed in the compartment.
    Terraform refuses to create the A1 worker when live A1 OCPUs plus the
    planned worker would exceed this value (plan-time precondition, so apply
    never runs). Default 4 = the post-June-2026 Always Free tenancy envelope
    (PAYG accounts keep 4 OCPU / 24 GB; free-only accounts are forcibly stopped
    above 2 OCPU / 12 GB - set 2 there for extra safety). Raise it only if you
    deliberately want to pay for A1 capacity.
  EOT
  type        = number
  default     = 4

  validation {
    condition     = var.overprovision_max_ocpus >= 1 && var.overprovision_max_ocpus <= 8
    error_message = "overprovision_max_ocpus must be between 1 and 8."
  }
}

# ----------------------------------------------------------------------------
# Misc
# ----------------------------------------------------------------------------
variable "instance_name_prefix" {
  description = "Display name prefix for created instances."
  type        = string
  default     = "gh-runner"
}

variable "preserve_boot_volume" {
  description = "Keep the A1 boot volume after 'terraform destroy' (data survives; volume counts against the 200 GB free quota until manually deleted)."
  type        = bool
  default     = false
}
