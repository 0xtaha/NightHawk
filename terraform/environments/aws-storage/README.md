# AWS storage execution root

This independently managed root provisions telemetry storage through the
cloud object-storage facade. It is not a complete EKS environment or a
state-bootstrap root.

## Prerequisites

Use Terraform 1.13.5, the included provider lock, an existing EKS/OIDC provider,
and separately bootstrapped encrypted S3 state plus DynamoDB locking.
The AWS account/region, globally unique prefix, service accounts, version policy,
and provisioning credentials must be supplied by the operator.

Creating these resources is billable and requires explicit deployment
authorization. The commands below are operator instructions, not evidence that
an authenticated plan or deployment was run.

## Static validation without AWS access

From this directory:

```text
terraform init -backend=false -input=false
terraform validate
```

Expected result: configuration is valid. This checks neither credentials nor
actual AWS permissions, quotas, bucket-name availability, or OIDC existence.

## Authorized initialization and deployment

Create local reviewed input files from `terraform.tfvars.example` and
`backend.hcl.example`, replacing every example account/provider/state value.
The example service-account names must match the deployed charts, not their
unverified default names. Keep credential material out of both files; use
the AWS credential chain/short-lived operator credentials.

From this root, after authorization:

```text
terraform init -reconfigure -backend-config=backend.hcl
terraform plan -var-file=environment.tfvars -out=storage.tfplan
terraform apply storage.tfplan
terraform output -json storage
```

Inspect the saved plan before applying. Protect plan files and state as sensitive
artifacts even though this module exports no plaintext access keys. The explicit
`account_id` configures the AWS provider's allowed-account guard.

Export the named JSON output to an ignored local path and import it through
the [configuration CLI](../../../docs/02-configuration.md). Annotate each
declared service account with its corresponding `role_arns` entry.
The storage module does not install Helm releases or apply those annotations.

Expected deployment outputs are per-bucket bindings and per-backend IRSA role
ARNs. After a successful authorized deployment, a second plan should be empty;
that acceptance check has not been run here.

## State locking migration

The initial backend example uses DynamoDB locking as specified. Terraform 1.13.5
also supports native S3 lockfiles. To migrate safely, pause writers, back up
state, ensure clients and IAM permissions support the `.tflock` object, then
enable `use_lockfile = true` while retaining `dynamodb_table` during the
transition. Reinitialize clients with `-reconfigure` at the same bucket/key.
Remove DynamoDB configuration only after all writers use S3 locking.
Do not delete or move state/bootstrap resources as part of this transition.

## Troubleshooting and rollback

For IAM, encryption, versioning, and name-collision errors, see the
[adapter guide](../../modules/aws-s3-backends/README.md).
Backend initialization failures require correcting state bucket/key, encryption
permissions, lock-table settings, and credentials; never fall back silently to
local state for a real deployment.

Storage and KMS keys are destroy-protected. Workload rollback/teardown must leave
this root and its state intact. No ordinary destroy target or destructive purge
is provided here; consult the adapter's protection caveats before changing
resource configuration.
