# Architecture

## Status and scope

NightHawk is a standalone, tenant-aware Grafana observability platform for
metrics (Mimir), logs (Loki), traces (Tempo), and profiles (Pyroscope), collected
with Alloy and explored in Grafana.

This document defines the implementation contract. It is not evidence of a
running or production-validated deployment. Component versions, compatibility,
and acceptance results must be recorded before a deployment is declared supported.

The repository's `Plan.md`, with the storage amendments below, is the specification. Existing Linux machines are
the self-hosted infrastructure boundary; provisioning virtual machines is not
included. No numeric quickstart startup target has been supplied.

## Deployment profiles

See the [Mermaid architecture and monitoring-flow diagrams](03-diagrams.md)
for visual overviews of these profiles and the telemetry lifecycle.

| Profile | Compute | Telemetry storage | Availability | Terraform root(s) |
| --- | --- | --- | --- | --- |
| Docker development | Local Docker Compose | Private SeaweedFS on local named volumes | Single-node, non-HA | `environments/docker` (zero-resource placeholder; Compose-owned) |
| Docker production | Explicitly configured Linux host | Private SeaweedFS on local disks | Single-node, non-HA | `environments/docker` (zero-resource placeholder; Compose-owned) |
| Self-hosted development | Existing Linux node, k3s | SeaweedFS on local PVCs | Reduced footprint, non-HA | `environments/self-hosted-k8s` (zero-resource placeholder; Ansible/Helm-owned) |
| Self-hosted production | Existing Linux nodes, k3s | SeaweedFS on replicated local PVC-backed storage | Requires validated disks and distinct failure domains | `environments/self-hosted-k8s` (zero-resource placeholder; Ansible/Helm-owned) |
| AWS production | Multi-AZ EKS managed node groups | Separate component S3 buckets | Requires validated replicas, capacity, and failure domains | `environments/aws-state-bootstrap`, `environments/aws` (VPC+EKS), `environments/aws-storage` |

The Docker profile is implemented as a static Compose file over a rendered
configuration directory and started with `nighthawk quickstart-docker`; see
the [quickstart](00-quickstart.md) and [Docker Compose](07-docker-compose.md).
It has been run under rootless Podman, where tenant isolation, redaction,
override loading, and persistence across restarts were observed. Docker
Engine, retention deletion, and the production single-node profile are not
verified.

Compose uses monolithic backends and does not introduce Kafka. Kubernetes uses
official Grafana charts and includes Strimzi-managed Kafka when required by the
selected backend versions and storage architectures. Kafka topics, permissions,
replication, recovery, and resource budgets are part of that profile.

Self-hosted bootstrap disables k3s's bundled Flannel, Traefik, and ServiceLB
before installing Cilium, MetalLB, Longhorn, a pinned Traefik, and cert-manager.
Preflight checks must reject impossible replica, disk, address-pool, or network
configurations. The initial supported OS matrix must be verified, not inferred.

AWS provisioning owns VPCs, managed EKS node groups, EBS CSI, S3, KMS, IAM,
and required DNS/controller permissions. Stateful workloads use on-demand
capacity; optional spot capacity is limited to suitable stateless workloads.
Use IRSA rather than static AWS keys. Install Helm releases only after cluster
creation succeeds; Terraform must not initialize Kubernetes providers against
a cluster that does not yet exist.

This is implemented as four independently applied Terraform environment
roots under `terraform/environments/`: `aws-state-bootstrap` (the encrypted
S3 state bucket and DynamoDB lock table every other AWS root's backend
depends on), `aws-storage` (per-component S3 buckets via the
`object-storage` facade), `aws` (the VPC, EKS cluster, managed node groups,
and OIDC/IRSA wiring, composing the `aws-vpc-network` and `aws-eks`
modules), and two zero-resource placeholder roots, `self-hosted-k8s` and
`docker`, that exist only to carry a pinned `terraform`/`aws` provider
version for the compatibility matrix since those profiles' local
infrastructure is Ansible/Helm-owned, not Terraform-owned.

## Data and trust boundaries

```text
application / node / cluster / remote collector
                  |
           collection-time redaction
                  |
      HTTPS or OTLP gRPC / remote collector mTLS
                  |
         authenticated tenant gateway
                  |
    private signal-specific backend services
                  |
       private component object storage

customer user -> TLS -> Grafana organization -> query gateway -> backend
```

Clients cannot select arbitrary backend tenants. The gateway binds credentials
and remote collector certificate identities to authorized tenant/datastream
pairs, separates ingestion from query privileges, and rejects missing, unknown,
conflicting, or spoofed tenant headers. Raw ingestion and query endpoints are
never published directly.

The gateway is implemented as Traefik plus a NightHawk forward-auth service.
Traefik terminates TLS, verifies a client certificate when one is presented,
and routes only the paths in a single route table; there is no catch-all
route, so backend administrative, deletion, and rule-management endpoints are
unreachable. For every routed request it asks the auth service, which derives
the backend tenant ID from the credential and returns it as `X-Scope-OrgID`.
A client may omit that header; a supplied header must equal the bound ID. The
auth service must be reachable only from Traefik. See
[gateway](05-gateway.md) for the decision order, the trust lifecycle, and
what has not yet been observed at runtime.

Each customer has a Grafana organization, created and reconciled through
Grafana's HTTP API by `nighthawk provision-grafana`
([tenant provisioning](06-tenant-provisioning.md)). Each datastream has independently
provisioned data sources and a stable backend tenant ID. Dashboards, alerting,
and source credentials are organization-scoped. Anonymous access and cross-tenant
query federation are disabled. A Grafana platform administrator is a trusted
operator, not a customer isolation boundary.

Default-deny Kubernetes policies explicitly allow DNS, gateway traffic, object
storage, Kafka, ring/gossip, control-plane requirements, and exporters. Compose
uses private networks and localhost-only gateway publication by default.
Backend TLS/mTLS is enabled where supported; any plaintext backend link must be
listed with its private-network restrictions rather than described as encrypted.

## Shared configuration

The typed Python configuration renderer is the common source of deployment
inputs. It validates platform, tenant, version, and network contracts before
producing backend settings, tenant overrides, gateway routes, Grafana
provisioning, and host/infrastructure inputs.

Implemented outputs: per-tenant runtime overrides for Mimir, Loki, Tempo, and
Pyroscope; the Traefik gateway configuration and route table; the Grafana
desired state; one collector configuration per datastream and profile;
each backend's own configuration for the Docker profile; and, as separate
secret-bearing steps, the gateway auth policy and the local object storage
identities. Backend configuration for Kubernetes and host/infrastructure
inputs are not rendered yet.

Each tenant/datastream pair declares:

- A stable globally unique backend ID, distinct from display names.
- Enabled signals and explicit retention for every enabled signal.
- Ingestion/query limits and approved collection/redaction policy. The
  ingestion budget is enforced by every backend. `query_concurrency` is not
  enforceable per tenant by any pinned backend and is reported as unenforced
  rather than rendered.
- Credential references with permissions limited to authorized backend IDs.

Changing a backend ID is a data migration, not a rename. Validation rejects
duplicate IDs, ambiguous delimiters, unsupported durations, absent secret
references, and unauthorized credential mappings. There is no implicit
production retention policy.

The network contract owns ports, protocols, purpose, direction, and scope.
Firewall inputs and port documentation are generated from it. Stateful security
groups and stateless NACLs require different return-traffic rules.

Generated decrypted configuration is ignored by Git, created with restricted
permissions, and cleaned up explicitly. Production requires operator-provided
age recipients and trust/DNS inputs. SOPS with age is the implemented secret
lifecycle: `nighthawk doctor` verifies the `sops`/`age` binaries against the
pinned versions in `config/versions.yaml` before any secrets command runs,
`nighthawk generate-recipient` creates age key pairs for non-production use,
`nighthawk encrypt-secret` and `nighthawk rotate-secret` manage SOPS-encrypted
files under `secrets/`, and `nighthawk materialize-secrets` /
`nighthawk clean-secrets` decrypt a validated platform document's referenced
secrets into `.materialized-secrets/` (owner-only permissions) and remove them
again. Production-profile documents reject locally auto-generated age
recipients unless explicitly confirmed. Examples contain references, not
working credentials.

## Retention and object storage

Backend retention is authoritative. Every tenant/datastream pair receives
explicit runtime overrides and the appropriate compactor/deletion workers.
Mimir retention is per tenant, not per series. Loki supports tenant/stream
policies. Tempo retention must use the selected version's tenant compaction
configuration.

Pyroscope is a release gate: prove that the selected supported storage mode
implements independent per-tenant retention. Do not apply a v1-only retention
option to v2 storage. If no supported mode satisfies the requirement, obtain an
explicit architecture decision before substituting a different behavior.

Use separate buckets and least-privilege identities for each signal backend,
including any additional ruler/alert buckets required by its version. Bucket
lifecycle must not remove live data early or move queryable blocks to Glacier.
Archival/export is a separate workflow. Account for noncurrent object versions
and backups when claiming deletion; rendered retention values alone do not prove
GDPR deletion.

### Storage amendments

The approved local-storage implementation is SeaweedFS S3 on local disks/PVCs,
not direct backend filesystem storage. No cloud tiering or offload is enabled.
MinIO Community is no longer a dependency: its original upstream repository is
unmaintained. SeaweedFS is Apache-2.0 licensed and supplies official images and
a Helm chart. API support is not proof of compatibility with the telemetry stack.

Compose persists master state, object volumes, and filer metadata. Production
Kubernetes also requires replicated masters and volumes, explicit failure-domain
placement, and shared HA filer metadata on local PVCs. Multiple S3 frontends
alone do not make metadata or object storage highly available. Native SeaweedFS
Write permission includes deletion; narrower role separation needs tested
operation-specific policies.

For self-hosted production Kubernetes, the operator chose one HA PostgreSQL
cluster shared by SeaweedFS filer metadata and Grafana, with separate databases
and credentials. Budget database capacity and connections for both workloads.
This reduces resource usage but couples their availability; database failure
can affect both object access and dashboards. Backup/restore acceptance must
cover filer metadata together with object volumes, not just the Grafana database.
AWS uses S3 and therefore does not need the SeaweedFS metadata database.

In the Compose stack SeaweedFS serves S3 over TLS only, with one identity per
signal backend restricted to that backend's buckets; cross-bucket and
anonymous access were observed to be refused.

Compose initialization owns local buckets and identities; Helm/Ansible
orchestration owns their self-hosted Kubernetes equivalents. Terraform never
competes to manage these local resources.

Cloud storage uses a Terraform `object-storage` facade with an AWS S3 adapter.
AWS is the only implemented cloud target in this scope; unsupported vendors
must fail explicitly. The interface exposes per-component bucket bindings:
protocol, endpoint, region, bucket, addressing style, TLS/trust configuration,
identity references, and capabilities. It does not export plaintext keys.
Future native non-S3 services require backend-specific adapters rather than an
endpoint change. AWS workloads use IRSA.

State bootstrap is separate, encrypted,
protected, and excluded from normal teardown; initially use compatible S3 state
and DynamoDB locking with a documented native S3 locking migration.

## Collection and operational requirements

Split node and cluster Alloy discovery to prevent duplicate metrics. Bound
queues and retries and expose collector/backend self-monitoring. Keep optional
privileged/eBPF profiling separate from minimally privileged collection.
Application SDKs, kube-state-metrics, and device exporters remain necessary
where Alloy cannot collect a signal itself.

A collector serves exactly one datastream. `alloy-configs/<profile>/` holds
hand-written sources that forward only to fixed redaction receivers;
`nighthawk render-collector` adds the generated OTLP receiver, redaction, and
gateway delivery for one datastream, so nothing reaches the gateway without
passing redaction. The collector authenticates with its datastream's
ingestion credential and never sets a tenant header. See
[collection](04-collection.md).

Apply verified collection-time redaction to logs, OTLP/resource attributes,
and metric labels. Drop sensitive data by default. Document limits of hashing
and profiling payload sanitization rather than promising universal removal.

Multi-replica Grafana requires shared PostgreSQL and supported unified-alerting
HA configuration. HPA is only enabled for verified horizontally scalable
components; AWS autoscaling includes its required metrics/controller services.
Resource budgets include Kafka, storage, databases, replication, and throughput.

Alerts use a configurable webhook contact point, with a local test receiver.
Production requires an actual endpoint and credentials. Backend outages/no-data
need explicit handling; an external dead-man endpoint is opt-in.

## Validation and evidence

Static validation and unit tests establish configuration correctness, not
production readiness. Runtime acceptance must demonstrate:

- Ingest/query/correlate all four signals and survive expected restarts.
- Isolation between two customers with two datastreams each, including spoofed
  headers, write-only query attempts, and bad/revoked certificates.
- Retention selection and actual deletion after supported processing windows.
- Absence of sensitive fixtures in received and queried telemetry.
- Alert delivery, network allow/deny rules, rotation, and backup/restore.
- Empty second Terraform plans and idempotent second Ansible runs.
- Object-store multipart completion/abort, paginated and empty-prefix listing,
  byte ranges, missing objects, cross-bucket denial, read-only access, metadata
  failover, volume repair/rebalance, and restoration.

Record local checks, unexecuted CI definitions, and blocked runtime/host/cloud
checks separately. Security delivery includes secret and vulnerability scans,
SBOMs, and signature verification against explicit trusted identities; document
provenance for unsigned upstream artifacts.

Remote changes, cloud/DNS resources, Docker daemon setup, billable validation,
and destructive purges need separate authorization. Ordinary teardown preserves
data and never destroys state-bootstrap resources.

## Compatibility research

Candidate releases found in upstream research are Mimir 3.2.1, Tempo 3.0.3,
Pyroscope 2.3.1, and SeaweedFS 4.47 (chart 4.47.0). These are not a tested
compatibility matrix or a completed image-digest lock.

Pyroscope 2.3.1 v2 implements tenant `retention_period` overrides and wires them
into metastore cleanup. That field is hidden from generated configuration
documentation. The operator approved using this version-pinned option with an
explicit compatibility caveat and mandatory runtime deletion acceptance tests.
Retain that caveat and test policy selection and actual deletion.
The v1 `compactor_blocks_retention_period` is not the v2 setting.

References:

- [Pyroscope v2 retention implementation](https://github.com/grafana/pyroscope/blob/v2.3.1/pkg/metastore/index/cleaner/retention/retention.go)
- [Pyroscope tenant override tests](https://github.com/grafana/pyroscope/blob/v2.3.1/pkg/validation/retention_overrides_test.go)
- [Tempo 3 deployment modes](https://github.com/grafana/tempo/blob/v3.0.3/docs/sources/tempo/reference-tempo-architecture/deployment-modes.md)
- [Mimir 3.2.1](https://github.com/grafana/mimir/releases/tag/mimir-3.2.1)
- [SeaweedFS 4.47](https://github.com/seaweedfs/seaweedfs/releases/tag/4.47)
- [SeaweedFS chart](https://github.com/seaweedfs/seaweedfs/tree/4.47/k8s/charts/seaweedfs)
- [MinIO maintenance status](https://github.com/minio/minio)
