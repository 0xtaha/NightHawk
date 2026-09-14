# AWS telemetry object storage

## Scope and prerequisites

This module provisions S3 buckets, per-backend KMS keys, and per-backend IRSA
roles/policies. It does not create EKS, the OIDC provider, VPC endpoints, Kubernetes
service accounts, or Terraform state-bootstrap resources.

Requirements:

- Terraform 1.13.5 and the locked `hashicorp/aws` provider 6.12.0.
- A configured AWS provider and an existing EKS IAM OIDC provider in that account.
- A globally unique 3-32 character bucket prefix.
- Explicit backend namespace/service-account mappings matching the eventual
  Helm releases, including any separate ruler/compactor service accounts.
- Separately authorized AWS credentials before any real plan/apply.

Use the [facade](../object-storage/README.md) rather than binding deployment
code directly to this adapter.

## Managed resources

| Backend | Logical buckets |
| --- | --- |
| Mimir | `mimir-blocks`, `mimir-ruler`, `mimir-alertmanager` |
| Loki | `loki-chunks`, optionally `loki-ruler` |
| Tempo | `tempo-traces` |
| Pyroscope | `pyroscope-profiles` |

Only configured backends receive resources. Every bucket has public access
blocked, bucket-owner-enforced ownership, default SSE-KMS encryption, and a TLS
deny policy. Bucket policies reject explicit non-KMS encryption, SSE-C, and
explicit SSE-KMS requests that omit the assigned key or select another key.
Clients can omit encryption headers and use the configured bucket default.

Each backend has a distinct rotating KMS key and IRSA role. Multiple components
of the same backend can share its role, but a namespace/service-account pair
cannot be assigned to different backends.

IRSA trust requires the exact issuer, AWS account/partition, `sts.amazonaws.com`
audience, and enumerated `system:serviceaccount:<namespace>:<name>` subjects.
Policies allow only assigned buckets, normal object/multipart operations, and
KMS data-key generation/decryption through regional S3 with assigned bucket
encryption contexts. They do not grant bucket administration, unrestricted
listing, or `DeleteObjectVersion`.

The KMS key policy's account principal enables IAM delegation for that key.
Its `Resource: "*"` means that key in a KMS resource policy, not all account
keys. Workload identity policies name the specific backend key ARN.
Terraform's provisioning identity is separate and requires its own administrative
permissions; no operator credentials are created or exported by this module.

## Retention and versioning

Backend retention remains authoritative. Lifecycle never expires current
telemetry by age and never moves queryable objects into archival storage.
Incomplete multipart uploads are aborted after seven days.

Versioning is off unless explicitly enabled. When enabling it, specify a
positive whole `noncurrent_expiration_days`; no implicit backup retention is
chosen. Example policy, **not a production recommendation**:

```hcl
versioning = {
  enabled                    = true
  noncurrent_expiration_days = 2
}
```

With versioning, backend deletion creates a delete marker; noncurrent versions
remain recoverable until lifecycle cleanup after the configured delay. Expired
delete markers are then cleaned up. S3 lifecycle processing is asynchronous:
this policy is not evidence of deletion within an exact deadline or from backups.
Operators must include this extra window in deletion requirements.

Changing an already versioned bucket to suspended does not remove existing
versions. Before disabling versioning, perform an explicitly authorized audit
and cleanup of retained versions, or keep versioning and its lifecycle policy
enabled. The module cannot infer historical object versions from configuration.

## Validation

Initialize the adapter and facade without remote backends:

```text
terraform -chdir=terraform/modules/aws-s3-backends init -backend=false -input=false
terraform -chdir=terraform/modules/object-storage init -backend=false -input=false
```

From the repository root in PowerShell, using the existing Python environment:

```powershell
$env:NIGHTHAWK_TERRAFORM = (Get-Command terraform).Source
.\.venv\Scripts\python.exe -m unittest tests.test_config tests.terraform.test_storage_contract
```

Alternatively set `NIGHTHAWK_TERRAFORM` to an isolated Terraform executable's
absolute path. No global PATH modification is required. On Linux, use the
virtual environment's `bin/python`.

The integration tests run both native Terraform test files and consume their
actual JSON mock-plan outputs through the Python validator and renderer. They
check the interface, including optional ruler storage and China partition
addressing, without an authenticated plan or an AWS apply.

Expected: 51 Python tests pass, including four Terraform integration checks.
The nested Terraform suites contain 14 adapter runs and four facade runs,
all plan-only with mocked AWS providers. Resource safeguards, IRSA mapping,
KMS/bucket isolation, version lifecycle, and invalid inputs are checked.
These counts describe the current suite, not production acceptance.

`terraform test` can also run independently from either module directory.
Provider initialization downloads public provider packages and verifies their
signed checksums. Provider locks are included for each executable validation root.

## Troubleshooting

- OIDC precondition failure: verify account, partition, issuer ID and provider ARN.
  Do not broaden subjects or replace `StringEquals` with a wildcard condition.
- Missing noncurrent policy: supply a reviewed delay before enabling versioning.
- Service-account conflict: give each backend distinct accounts; do not reuse an
  all-powerful collector account for backend storage.
- S3/KMS access denied after deployment: check service-account annotations, trust
  audience/subjects, and encryption headers. Explicit SSE-KMS requests must use
  the key ARN, not an alias or an omitted key ID.
- Bucket name collision: choose a unique prefix before initial provisioning.
  Renaming a populated bucket is a migration, not an in-place update.

## Teardown, rollback, and remaining acceptance

Buckets use `force_destroy = false`; buckets and KMS keys also use static
`prevent_destroy = true`. Keep storage in its own Terraform root, outside
ordinary workload teardown. An ordinary destroy of the managed configuration
fails rather than deleting even empty buckets or scheduling key deletion.
There is no implemented purge shortcut.

Terraform protection depends on keeping the resource configuration present.
Do not remove the module/block to bypass it. Account-level deletion controls,
backups, and reviewed operational access remain necessary. Rolling back a
workload should leave storage and identities intact; reverting policy changes
requires reviewing a new plan before apply.

Unverified environment acceptance still includes authenticated plans, actual
IRSA assumption, allowed/denied S3 and KMS operations, private connectivity,
backend compaction/retention, backup restoration, and an empty second plan.
