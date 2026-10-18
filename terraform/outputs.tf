output "a1_public_ip" {
  description = "Public IP of the A1 runner worker VM."
  value       = oci_core_instance.a1.public_ip
}

output "a1_instance_id" {
  description = "OCID of the A1 worker instance (used by the waker)."
  value       = oci_core_instance.a1.id
}

output "a1_shape" {
  description = "Shape summary of the A1 worker."
  value       = "VM.Standard.A1.Flex ${local.a1_ocpus} OCPU / ${local.a1_memory} GB (${var.runner_profile})"
}

output "waker_public_ip" {
  description = "Public IP of the waker VM (status page + wake service). Empty when the waker is disabled."
  value       = var.enable_waker ? oci_core_instance.waker[0].public_ip : ""
}

output "waker_instance_id" {
  description = "OCID of the waker instance."
  value       = var.enable_waker ? oci_core_instance.waker[0].id : ""
}

output "ssh_public_key_used" {
  description = "The SSH public key installed on both VMs."
  value       = local.ssh_public_key
}

output "ssh_private_key_pem" {
  description = "SSH PRIVATE key for both VMs (only non-empty when terraform generated the keypair, i.e. neither ssh_public_key nor ssh_public_key_path was provided). CI reads it from state to deploy the runner stack; humans can fetch it via the 'Get SSH key' workflow. Treat as a secret."
  value       = local.generate_ssh_key ? tls_private_key.ssh[0].private_key_openssh : ""
  sensitive   = true
}

output "ssh_a1" {
  description = "SSH command for the A1 worker."
  value       = "ssh ubuntu@${oci_core_instance.a1.public_ip}"
}

output "ssh_waker" {
  description = "SSH command for the waker."
  value       = var.enable_waker ? "ssh ubuntu@${oci_core_instance.waker[0].public_ip}" : ""
}

output "status_page" {
  description = "Where to watch the runner fleet status."
  value       = var.status_public && var.enable_waker ? "http://${oci_core_instance.waker[0].public_ip}:8080 (token-protected)" : "ssh -N -L 8080:localhost:8080 ubuntu@${var.enable_waker ? oci_core_instance.waker[0].public_ip : oci_core_instance.a1.public_ip} -> http://localhost:8080"
}
