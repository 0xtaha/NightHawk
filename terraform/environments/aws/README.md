# AWS compute execution root

This independently managed root composes the
[`aws-vpc-network`](../../modules/aws-vpc-network/README.md) and
[`aws-eks`](../../modules/aws-eks/README.md) modules into a VPC + EKS
compute environment. It is not the storage root; see
[`aws-storage`](../aws-storage/README.md) for that.

## Prerequisites

Use Terraform 1.13.5, the included provider lock, and the already-applied
[`aws-state-bootstrap`](../aws-state-bootstrap/README.md) root's bucket and
lock table. The AWS account/region, globally unique prefix, and
provisioning credentials must be supplied by the operator.

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
actual AWS permissions, quotas, or subnet/CIDR availability.

To plan the example values against a mocked provider, still without AWS
access:

```text
terraform test -var-file=terraform.tfvars.example
```

Expected result: one passing run. It confirms the example values are accepted
and that every node carries the cluster's security group and the
contract-derived one. It is a plan with mocked values, not evidence of a
deployment.

## Required inputs without defaults

`endpoint_public_access`, `public_access_cidrs`, and
`cluster_admin_principal_arns` must be set; see
[cluster access](../../modules/aws-eks/README.md#cluster-access). The
principal that applies this root gets no cluster access unless it is listed.
`secrets_kms_key_arn` and the three controller inputs are optional; see
[encryption at rest](../../modules/aws-eks/README.md#encryption-at-rest) and
[controller identities](../../modules/aws-eks/README.md#controller-identities).

A plan made from an earlier revision of this root will show the new access
entries, KMS key, launch templates, and controller roles as additions, and
both node groups as replaced because they move onto launch templates. Nothing
from any revision has been applied by this project.

## Apply order

1. Apply [`aws-state-bootstrap`](../aws-state-bootstrap/README.md) once per
   AWS account (local state) and note its `state_bucket`/`lock_table`
   outputs.
2. Apply this root (below).
3. Copy this root's `oidc_provider_arn`/`oidc_issuer_url` outputs into
   [`aws-storage`](../aws-storage/README.md)'s `aws_identity` variable, and
   apply that root. This root does not chain into or apply `aws-storage`
   automatically.

## Authorized initialization and deployment

Create local reviewed input files from `terraform.tfvars.example` and
`backend.hcl.example`, replacing every example account/prefix/state value.
Keep credential material out of both files; use the AWS credential
chain/short-lived operator credentials.

From this root, after authorization:

```text
terraform init -reconfigure -backend-config=backend.hcl
terraform plan -var-file=environment.tfvars -out=aws.tfplan
terraform apply aws.tfplan
terraform output -raw oidc_provider_arn
terraform output -raw oidc_issuer_url
terraform output -json public_subnet_ids
terraform output -json private_subnet_ids
terraform output -json controller_role_arns
```

Inspect the saved plan before applying. Copy the `oidc_provider_arn`/
`oidc_issuer_url` output values verbatim into `aws-storage`'s
`terraform.tfvars` `aws_identity` block; no reshaping is required since the
`aws-eks` module's output contract matches that variable's expected shape
exactly.

## Network contract mapping

`main.tf`'s `network_rules` local literally mirrors `config/network.yaml`'s
AWS-destined rules (`destination` prefixed `aws-`; currently only
`aws-object-storage`). `tests/test_terraform_boundaries.py` compares every
field of every mirrored rule (source, destination, protocol, port, scope)
with the network contract and names the rule and field that differ - update
both when an AWS-destined rule is added to, removed from, or changed in
`config/network.yaml`.

## Troubleshooting and teardown

Backend initialization failures require correcting state bucket/key,
lock-table settings, and credentials; never fall back silently to local
state for a real deployment. Ordinary teardown of this root must not affect
`aws-state-bootstrap` (separate root/state) or `aws-storage` (independently
applied); each root's own teardown documentation governs its own resources.
