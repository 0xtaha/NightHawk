# AWS state-bootstrap execution root

This independently managed root creates the encrypted, versioned S3 bucket
and DynamoDB lock table every other AWS Terraform root in this platform
(`environments/aws`, `environments/aws-storage`) uses as its remote backend.
It is not a VPC, EKS, or storage root.

## Local state is deliberate here

Every other AWS root in this repository declares `backend "s3" {}` and
expects an operator-supplied `backend.hcl`. This root cannot do that: before
it runs, no bucket or lock table exists yet to back it. It therefore
initializes and applies with ordinary local state, by design, as the one
deliberate exception to "never use local state for a real deployment." Apply
it once per AWS account; afterward leave it alone. Do not add a
`backend "s3" {}` block here even after the bucket exists - doing so would
make this root responsible for a backend it cannot safely self-reference
during its own first apply.

## Prerequisites

Use Terraform 1.13.5, the included provider lock, and provisioning
credentials for the target AWS account/region. The account/region and a
globally unique prefix must be supplied by the operator.

Creating these resources is billable and requires explicit deployment
authorization. The commands below are operator instructions, not evidence
that an authenticated plan or deployment was run.

## Static validation without AWS access

From this directory:

```text
terraform init -backend=false -input=false
terraform validate
```

Expected result: configuration is valid. This checks neither credentials nor
actual AWS permissions, quotas, or bucket-name availability.

## Authorized initialization and deployment

Create a local reviewed `environment.tfvars` from `terraform.tfvars.example`,
replacing every example account/prefix value. Keep credential material out
of it; use the AWS credential chain/short-lived operator credentials.

From this root, after authorization:

```text
terraform init -input=false
terraform plan -var-file=environment.tfvars -out=bootstrap.tfplan
terraform apply bootstrap.tfplan
terraform output -raw state_bucket
terraform output -raw lock_table
```

Inspect the saved plan before applying. Copy the two output values into the
`bucket` and `dynamodb_table` fields of every other AWS root's
`backend.hcl` (for example `environments/aws/backend.hcl.example` and
`environments/aws-storage/backend.hcl.example`). This root does not chain
into or apply those roots automatically.

## State locking migration

This bootstrap provisions a DynamoDB lock table as specified. Terraform
1.13.5 also supports native S3 lockfiles (`use_lockfile = true`). To migrate
a dependent root safely from DynamoDB locking to native S3 locking:

1. Pause all writers against that root (stop CI applies, warn operators).
2. Back up the root's state (`terraform state pull > backup.tfstate`).
3. Verify every client and the IAM policy used for that root support the
   `.tflock` object (Terraform 1.13+, `s3:PutObject`/`s3:DeleteObject` on the
   state key's `.tflock` suffix).
4. Add `use_lockfile = true` to that root's backend configuration while
   retaining `dynamodb_table` during the transition, then
   `terraform init -reconfigure` with every client.
5. Once every writer has reinitialized with `use_lockfile = true` and no
   writer still depends on the DynamoDB table, remove `dynamodb_table` from
   that root's backend configuration and reinitialize once more.

This bootstrap root's own DynamoDB table and S3 bucket are never deleted or
recreated as part of this migration; only the dependent root's backend
configuration changes. Do not delete the DynamoDB table until every
dependent root has confirmed native locking works end to end.

## Troubleshooting and teardown

The state bucket and lock table are destroy-protected
(`lifecycle.prevent_destroy = true`). No ordinary `terraform destroy` target
is provided here. If this account's state backend must truly be retired,
remove `prevent_destroy` deliberately, confirm no other root still
references the bucket/table, and only then run a scoped destroy - this is
an exceptional, rarely-needed operation, not a routine one.
