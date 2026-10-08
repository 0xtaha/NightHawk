output "state_bucket" {
  description = "Encrypted, versioned S3 bucket name for other roots' backend.hcl."
  value       = aws_s3_bucket.state.bucket
}

output "lock_table" {
  description = "DynamoDB lock table name for other roots' backend.hcl."
  value       = aws_dynamodb_table.locks.name
}
