# AWS VPC and network-contract-derived security groups

This module provisions a multi-AZ VPC (public/private subnets, routing, a
single NAT gateway) and security groups whose rules are generated from a
typed `network_rules` variable, not by reading `config/network.yaml`
directly. It does not install EKS, Helm, or any Kubernetes resource; see the
[`aws-eks` module](../aws-eks/README.md) for that.

## Contract

- `network_rules` mirrors `config/network.yaml`'s schema
  (`id`/`source`/`destination`/`protocol`/`port`/`scope`). **The calling
  root owns mapping `config/network.yaml` into this variable** - this
  module intentionally stays file-system/config-format-free, consistent
  with how `object-storage` receives typed variables instead of reading
  `config/*.yaml` itself.
- A rule with `scope = "loopback"` fails variable validation: that scope
  describes Docker/self-hosted local traffic this module never provisions.
- A rule whose `destination` is not `"aws-s3"` fails variable validation
  today. Extend this module's mapping (a new `aws_vpc_security_group_*_rule`
  resource keyed on the new destination) before adding a new AWS-relevant
  rule to `config/network.yaml`.
- The one currently-supported destination, `"aws-s3"`, is satisfied by a VPC
  Gateway Endpoint for S3 plus a per-source security-group egress rule
  scoped to that endpoint's prefix list - not a public `0.0.0.0/0:443` rule -
  matching the network contract's own purpose text ("S3 via private service
  connectivity"). Rules are created in the direction the contract declares;
  today that is egress only.
- The security groups do nothing until they are attached. The
  [`aws-eks` module](../aws-eks/README.md) attaches them to every node through
  its `node_security_group_ids` input, which the root wires from this
  module's `security_group_ids` output.
- Public subnets are tagged `kubernetes.io/role/elb = 1` and private subnets
  `kubernetes.io/role/internal-elb = 1`, the tags a load balancer integration
  uses to find subnets.

## Outputs

`vpc_id`, `public_subnet_ids`, `private_subnet_ids`,
`security_group_ids` (map keyed by network-contract `source` identity, e.g.
`"signal-backend"`), and `s3_vpc_endpoint_id`.

## Prerequisites and use

Use Terraform **1.13.5** and the locked AWS provider **6.12.0**. Provider
configuration belongs to the executable root, not this module (see
[`environments/aws`](../../environments/aws/README.md)).

## Local checks

From this module directory:

```text
terraform init -backend=false -input=false
terraform validate
terraform test
```

Expected results: valid configuration and three passing plan-only mocked
runs. Tests cover the real `aws-object-storage` network-contract rule with its
protocol and port, the subnet role tags, a rejected `loopback`-scope rule, and
a rejected unsupported destination. They do not contact AWS or prove
deployment compatibility.
