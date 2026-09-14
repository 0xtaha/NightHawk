# Cloud object-storage interface

This facade implements **AWS S3 only**. Unsupported vendor values fail validation;
they do not silently select AWS. Docker and self-hosted Kubernetes storage is
SeaweedFS on local disks/PVCs, owned by deployment orchestration, not Terraform.

## Contract

The `storage` output matches the platform's storage binding schema:

```text
{
  provider: "aws",
  bindings: {
    logical-bucket-name: {
      protocol, endpoint, region, bucket, force_path_style,
      tls: { enabled, ca_secret_ref },
      identity: { type, ref },
      capabilities: { versioning, lifecycle, workload_identity }
    }
  }
```

`identity.type` is `irsa`; `identity.ref` is a role ARN, never a static access
key. `role_arns` exposes the same backend identities for Kubernetes annotations.
Export the named output with `terraform output -json storage` and use the
Python CLI's `--storage-output` option.

The facade forwards validated workload and storage settings to the
[AWS adapter](../aws-s3-backends/README.md). Its `aws_identity` input is
deliberately AWS-specific. A future vendor must implement its own identity,
encryption, lifecycle, and backend protocol integration while preserving
the output contract; a non-S3 provider is not just an endpoint replacement.

## Prerequisites and use

Use Terraform **1.13.5** and the locked AWS provider **6.12.0**.
Terraform 1.13.5 supports plan-time mock values, allowing tests to keep
`prevent_destroy` enabled without creating state that requires test teardown.
Provider configuration belongs to the executable root, not this module.

The [AWS storage root](../../environments/aws-storage/README.md) provides an
example with explicit account/region, remote backend configuration, and inputs.
It requires existing EKS/OIDC and state-bootstrap resources.

## Local checks

From this module directory:

```text
terraform init -backend=false -input=false
terraform validate
terraform test
```

Expected results: valid configuration and four passing plan-only mocked runs.
Tests cover AWS selection and rejection of Azure, GCP, and local storage.
They do not contact AWS or prove deployment compatibility.

For cross-language contract checks, follow the integration-test command in the
[AWS adapter guide](../aws-s3-backends/README.md).

## Failure and teardown behavior

Invalid inputs fail before an apply. Terraform never provisions SeaweedFS here.
The adapter protects managed buckets and encryption keys from ordinary destroy;
read its teardown restrictions before changing or removing module configuration.
