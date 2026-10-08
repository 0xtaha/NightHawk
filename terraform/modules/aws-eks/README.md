# AWS EKS cluster with IRSA

This module provisions an EKS cluster, two managed node groups
(`stateful`/`stateless`), the EBS CSI driver addon, and an IRSA-capable IAM
OIDC provider. It does not install any Helm release or Kubernetes-provider
resource: the cluster a Kubernetes/Helm provider would target does not exist
until this same apply completes, so initializing one here is never correct.

## Contract with the existing storage facade

This module's `oidc_provider_arn` and `oidc_issuer_url` outputs are typed
and shaped identically to [`aws-s3-backends`](../aws-s3-backends/README.md)'s
`aws_identity` variable:

```text
aws_identity = {
  oidc_provider_arn = module.eks.oidc_provider_arn
  oidc_issuer_url   = module.eks.oidc_issuer_url
}
```

An operator copies these two output values into the storage root's
`terraform.tfvars` without reshaping them. This module never applies the
storage root itself; see [`environments/aws`](../../environments/aws/README.md)
and [`environments/aws-storage`](../../environments/aws-storage/README.md)
for the documented copy-paste handoff.

## Capacity classes

- `stateful_node_group` is always `ON_DEMAND` capacity. This cannot be
  changed by input - spot-interruptible nodes are never appropriate for
  stateful workloads.
- `stateless_node_group.capacity_type` defaults to `ON_DEMAND` and must be
  explicitly set to `SPOT` to opt in. Per-backend/namespace workload
  placement onto these two capacity classes is a Helm/Kubernetes-manifest
  concern handled by a later phase, not by this module.

## Version pins

`cluster_version` (default `"1.37"`) and `ebs_csi_addon_version` (default
`"v1.66.0-eksbuild.1"`) are tracked by `config/versions.yaml`'s
`kubernetes_platform.eks.version` and `kubernetes_platform.ebs_csi_driver.version`
pins respectively, enforced by `nighthawk check-pins`. AWS does not publish
every addon version for every cluster version; confirm the exact available
`-eksbuildN` suffix for your chosen `cluster_version` with
`aws eks describe-addon-versions --addon-name aws-ebs-csi-driver
--kubernetes-version <cluster_version>` before applying.

## Prerequisites and use

Use Terraform **1.13.5** and the locked AWS provider **6.12.0**. This module
expects a VPC and subnet IDs from [`aws-vpc-network`](../aws-vpc-network/README.md);
provider configuration belongs to the executable root.

## Local checks

From this module directory:

```text
terraform init -backend=false -input=false
terraform validate
terraform test
```

Expected results: valid configuration and three passing plan-only mocked
runs, covering the pinned cluster/addon versions, the default/opt-in node
group capacity types, and rejection of an invalid `capacity_type`. They do
not contact AWS or prove deployment compatibility.
