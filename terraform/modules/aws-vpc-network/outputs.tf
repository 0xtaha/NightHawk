output "vpc_id" {
  description = "ID of the managed VPC."
  value       = aws_vpc.this.id
}

output "public_subnet_ids" {
  description = "Public subnet IDs, one per spanned availability zone."
  value       = [for subnet in aws_subnet.public : subnet.id]
}

output "private_subnet_ids" {
  description = "Private subnet IDs, one per spanned availability zone."
  value       = [for subnet in aws_subnet.private : subnet.id]
}

output "security_group_ids" {
  description = "Security-group ID per network-contract source identity (e.g. \"signal-backend\")."
  value       = { for source, sg in aws_security_group.workload : source => sg.id }
}

output "s3_vpc_endpoint_id" {
  description = "VPC Gateway Endpoint ID for private S3 access."
  value       = aws_vpc_endpoint.s3.id
}
