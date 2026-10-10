# AWS EKS cluster with IRSA

This module provisions an EKS cluster with explicit access and secrets
encryption, two managed node groups (`stateful`/`stateless`) on launch
templates, the EBS CSI driver addon, an IRSA-capable IAM OIDC provider, and
IRSA roles for the controllers that need AWS permissions. It does not install
any Helm release or Kubernetes-provider
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

## Cluster access

Three inputs have no default; state them:

| Input | Meaning |
| --- | --- |
| `endpoint_public_access` | Whether the API is reachable from outside the VPC. The private endpoint is always enabled |
| `public_access_cidrs` | Ranges allowed to reach the public endpoint. At least one when it is enabled, none when it is disabled. A range covering every address (`0.0.0.0/0`, `::/0`) is refused |
| `cluster_admin_principal_arns` | IAM roles or users granted cluster administration. At least one |

The cluster uses API authentication mode only, and the creating principal's
bootstrap administrator permission is turned off. Access exists only through
one access entry per listed principal, each with the cluster-admin access
policy at cluster scope. A private-only cluster needs a network path into the
VPC that this module does not create.

## Encryption at rest

- **Kubernetes secrets** are envelope-encrypted with a KMS key: the one named
  by `secrets_kms_key_arn`, or one this module creates with rotation enabled.
  The key in use is the `secrets_kms_key_arn` output.
- **Node root volumes** are encrypted through each node group's launch
  template (`gp3`, `root_volume_gb` per group, default 50). They use the
  account's default EBS key, not the secrets key.

## Node security groups

Each node carries the security group EKS creates for the cluster plus every
ID in `node_security_group_ids`, which the root fills from
[`aws-vpc-network`](../aws-vpc-network/README.md)'s contract-derived groups.
Without the cluster's own group a node could not reach the control plane, so
it is always included. The attached list is the `node_security_group_ids`
output.

Node groups reference their launch template by name. Moving an existing node
group onto a launch template replaces its nodes.

## Controller identities

Each role can be assumed only by the one service account named for it, through
the cluster's OIDC provider. Installing the controllers is a later phase; the
`controller_role_arns` output gives the ARN to annotate each service account
with.

| Controller | Input | Enabled | What the role may do |
| --- | --- | --- | --- |
| EBS CSI driver | none | always | The AWS-managed `AmazonEBSCSIDriverPolicy`, bound to `kube-system/ebs-csi-controller-sa` through the addon. The node role carries no volume permissions |
| DNS records | `dns_controller` | opt-in | Change and list records only in the listed hosted zones; list hosted zones |
| Certificate DNS-01 | `certificate_controller` | opt-in | Change `TXT` records only, in the listed hosted zones; follow a change; list zones by name |
| Node autoscaling | `autoscaler_controller` | opt-in | Describe scaling state; resize and terminate only in Auto Scaling groups tagged with this cluster's name |

An enabled DNS or certificate controller with no hosted zone is refused.
IAM for the AWS load balancer controller is not provided: its policy is
published per controller release and no release is pinned yet (phase 6).

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

Expected results: valid configuration and eighteen passing plan-only mocked
runs, covering the pinned cluster/addon versions, node group capacity types,
endpoint exposure and its refusals, access entries, secrets encryption with a
created and a supplied key, encrypted node volumes and attached security
groups, and each controller role's scope. They do not contact AWS or prove
deployment compatibility: nothing here has been applied, so the access
entries, launch templates, addon role binding, and IAM policies are verified
only as planned values.
