# -----------------------------------------------------------------------------
# Locals
# -----------------------------------------------------------------------------
locals {
  compartment = var.compartment_ocid != "" ? var.compartment_ocid : var.tenancy_ocid

  # SSH key resolution: content var > path var > generated keypair
  provided_ssh_key = var.ssh_public_key != "" ? trimspace(var.ssh_public_key) : (
    var.ssh_public_key_path != "" ? trimspace(file(pathexpand(var.ssh_public_key_path))) : ""
  )
  generate_ssh_key = local.provided_ssh_key == ""
  ssh_public_key   = local.generate_ssh_key ? trimspace(tls_private_key.ssh[0].public_key_openssh) : local.provided_ssh_key

  # Derived shape for the A1 worker based on profile (explicit vars win)
  a1_ocpus   = var.a1_ocpus > 0 ? var.a1_ocpus : (var.runner_profile == "burst" ? 4 : 2)
  a1_memory  = var.a1_memory_gbs > 0 ? var.a1_memory_gbs : (var.runner_profile == "burst" ? 24 : 12)
}

# Dedicated SSH keypair, generated when none is provided (the CI flow). The
# private key is exported as the sensitive output `ssh_private_key_pem` and
# lives only in the private, versioned state bucket + workflow artifacts.
resource "tls_private_key" "ssh" {
  count     = local.generate_ssh_key ? 1 : 0
  algorithm = "ED25519"
}

# -----------------------------------------------------------------------------
# Availability domains + images
# -----------------------------------------------------------------------------
data "oci_identity_availability_domains" "ads" {
  compartment_id = local.compartment
}

# Latest Canonical Ubuntu 24.04 platform image for aarch64 (Ampere A1)
data "oci_core_images" "ubuntu_arm" {
  compartment_id           = local.compartment
  operating_system         = "Canonical Ubuntu"
  operating_system_version = "24.04"
  shape                    = "VM.Standard.A1.Flex"

  filter {
    name   = "display_name"
    values = ["^Canonical-Ubuntu-24\\.04-2.*"]
    regex  = true
  }

  sort_by    = "TIMECREATED"
  sort_order = "DESC"
}

# Latest Canonical Ubuntu 24.04 platform image for x86 (AMD micro waker)
data "oci_core_images" "ubuntu_x86" {
  count = var.enable_waker ? 1 : 0

  compartment_id           = local.compartment
  operating_system         = "Canonical Ubuntu"
  operating_system_version = "24.04"
  shape                    = "VM.Standard.E2.1.Micro"

  filter {
    name   = "display_name"
    values = ["^Canonical-Ubuntu-24\\.04-2.*"]
    regex  = true
  }

  sort_by    = "TIMECREATED"
  sort_order = "DESC"
}

# -----------------------------------------------------------------------------
# Network
# -----------------------------------------------------------------------------
resource "oci_core_vcn" "this" {
  compartment_id = local.compartment
  cidr_blocks    = ["10.0.0.0/16"]
  display_name   = "${var.instance_name_prefix}-vcn"
  dns_label      = "ghrunner"
}

resource "oci_core_internet_gateway" "this" {
  compartment_id = local.compartment
  vcn_id         = oci_core_vcn.this.id
  display_name   = "${var.instance_name_prefix}-igw"
  enabled        = true
}

resource "oci_core_route_table" "this" {
  compartment_id = local.compartment
  vcn_id         = oci_core_vcn.this.id
  display_name   = "${var.instance_name_prefix}-rt"

  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_internet_gateway.this.id
  }
}

resource "oci_core_security_list" "this" {
  compartment_id = local.compartment
  vcn_id         = oci_core_vcn.this.id
  display_name   = "${var.instance_name_prefix}-sl"

  ingress_security_rules {
    description = "SSH from admin"
    protocol    = "6" # TCP
    source      = var.admin_cidr
    source_type = "CIDR_BLOCK"
    tcp_options {
      min = 22
      max = 22
    }
  }

  dynamic "ingress_security_rules" {
    for_each = var.status_public && var.enable_waker ? [1] : []
    content {
      description = "Waker status page from admin"
      protocol    = "6"
      source      = var.admin_cidr
      source_type = "CIDR_BLOCK"
      tcp_options {
        min = 8080
        max = 8080
      }
    }
  }

  # ICMP for path MTU discovery
  ingress_security_rules {
    protocol    = "1"
    source      = "0.0.0.0/0"
    source_type = "CIDR_BLOCK"
    icmp_options {
      type = 3
      code = 4
    }
  }

  egress_security_rules {
    destination      = "0.0.0.0/0"
    destination_type = "CIDR_BLOCK"
    protocol         = "all"
  }
}

resource "oci_core_subnet" "this" {
  compartment_id             = local.compartment
  vcn_id                     = oci_core_vcn.this.id
  cidr_block                 = "10.0.1.0/24"
  display_name               = "${var.instance_name_prefix}-subnet"
  route_table_id             = oci_core_route_table.this.id
  security_list_ids          = [oci_core_security_list.this.id]
  prohibit_public_ip_on_vnic = false
  dns_label                  = "runners"
}

# -----------------------------------------------------------------------------
# A1 worker instance
# -----------------------------------------------------------------------------
resource "oci_core_instance" "a1" {
  availability_domain = data.oci_identity_availability_domains.ads.availability_domains[var.availability_domain_index]["name"]
  compartment_id      = local.compartment
  display_name        = "${var.instance_name_prefix}-a1-${var.runner_profile}"
  shape               = "VM.Standard.A1.Flex"

  shape_config {
    ocpus         = local.a1_ocpus
    memory_in_gbs = local.a1_memory
  }

  source_details {
    source_type             = "image"
    source_id               = data.oci_core_images.ubuntu_arm.images[0]["id"]
    boot_volume_size_in_gbs = var.a1_boot_volume_gb
    boot_volume_vpus_per_gb = 10 # balanced performance, free-tier eligible
  }

  launch_options {
    is_pv_encryption_in_transit_enabled = true
  }

  create_vnic_details {
    subnet_id        = oci_core_subnet.this.id
    assign_public_ip = true
    display_name     = "${var.instance_name_prefix}-a1-vnic"
    hostname_label   = "runner"
  }

  metadata = {
    ssh_authorized_keys = local.ssh_public_key
    user_data = base64encode(
      templatefile("${path.module}/cloudinit-a1.yml.tpl", {
        runner_profile = var.runner_profile
      })
    )
  }

  # Cut the management/monitoring agents down to zero CPU noise on a small VM
  agent_config {
    are_all_plugins_disabled = true
    is_management_disabled   = true
    is_monitoring_disabled   = true
  }

  preserve_boot_volume = var.preserve_boot_volume

  freeform_tags = {
    "project"         = "oci-github-runner"
    "profile"         = var.runner_profile
    "always_free"     = "true"
    "bill_protection" = "size-within-always-free"
  }
}

# -----------------------------------------------------------------------------
# Waker instance (free AMD micro, no hour limits)
# -----------------------------------------------------------------------------
resource "oci_core_instance" "waker" {
  count = var.enable_waker ? 1 : 0

  availability_domain = data.oci_identity_availability_domains.ads.availability_domains[var.availability_domain_index]["name"]
  compartment_id      = local.compartment
  display_name        = "${var.instance_name_prefix}-waker"
  shape               = "VM.Standard.E2.1.Micro"

  source_details {
    source_type             = "image"
    source_id               = data.oci_core_images.ubuntu_x86[0].images[0]["id"]
    boot_volume_size_in_gbs = 47 # minimum allowed
    boot_volume_vpus_per_gb = 10
  }

  launch_options {
    is_pv_encryption_in_transit_enabled = true
  }

  create_vnic_details {
    subnet_id        = oci_core_subnet.this.id
    assign_public_ip = true
    display_name     = "${var.instance_name_prefix}-waker-vnic"
    hostname_label   = "waker"
  }

  metadata = {
    ssh_authorized_keys = local.ssh_public_key
    user_data           = base64encode(file("${path.module}/cloudinit-waker.yml.tpl"))
  }

  agent_config {
    are_all_plugins_disabled = true
    is_management_disabled   = true
    is_monitoring_disabled   = true
  }

  freeform_tags = {
    "project"     = "oci-github-runner"
    "always_free" = "true"
  }
}

# -----------------------------------------------------------------------------
# IAM: let the waker (via instance principals) start/stop ONLY the A1 worker
# -----------------------------------------------------------------------------
resource "oci_identity_dynamic_group" "waker" {
  count = var.enable_waker && var.create_waker_iam ? 1 : 0

  compartment_id = var.tenancy_ocid != "" ? var.tenancy_ocid : local.compartment
  description    = "Waker VM for oci-github-runner (can start/stop the A1 worker)"
  name           = "${var.instance_name_prefix}-waker-dg"
  matching_rule  = "Any {instance.id = '${oci_core_instance.waker[0].id}'}"
}

resource "oci_identity_policy" "waker" {
  count = var.enable_waker && var.create_waker_iam ? 1 : 0

  compartment_id = var.tenancy_ocid != "" ? var.tenancy_ocid : local.compartment
  description    = "Waker VM may control the lifecycle of the A1 runner worker only"
  name           = "${var.instance_name_prefix}-waker-policy"

  statements = [
    "Allow dynamic-group ${oci_identity_dynamic_group.waker[0].name} to read instance-family in tenancy",
    "Allow dynamic-group ${oci_identity_dynamic_group.waker[0].name} to manage instance-family in tenancy where target.instance.id = '${oci_core_instance.a1.id}'",
  ]
}
