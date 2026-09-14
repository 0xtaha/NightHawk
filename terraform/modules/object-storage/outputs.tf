output "storage" {
  description = "Stable storage binding contract. No access keys or plaintext credentials."
  value       = module.aws.storage
}

output "role_arns" {
  description = "Backend workload identity references for Kubernetes provisioning."
  value       = module.aws.role_arns
}
