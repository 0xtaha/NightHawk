# NightHawk Grafana observability platform

## Objective and current state

Build the standalone LGTM+ platform at the NightHawk repository root, implementing the attached brief across Docker Compose, self-hosted Kubernetes, and AWS EKS. This is an implementation plan only; no repository files have been changed.

The repository contains only a two-line README and has a clean working tree. There is no existing application or deployment to migrate. The branch has been renamed to `wp8fyqi-cariad-grafana-observability-platform`.

On the latest retry, the user explicitly enabled read-only worktree inspection and session-plan updates. Worktree commands and session-plan reads succeed. Repository editing is not yet verified: the previous implementation attempt could run `git status`, but its first `apply_patch` was denied. The repository still contains only the README and has no pending changes.

The Windows host has Python and WSL with an Ubuntu distribution. A read-only check inside Ubuntu confirmed Python 3 is available, but Docker, Terraform, Ansible, ansible-lint, Helm, Make, kubectl, SOPS, and age are unavailable. Use Ubuntu WSL for Linux-compatible validation with isolated, pinned tools after implementation approval. Cloud credentials, DNS ownership, target machines, and a container daemon have not been verified.

## Access and execution boundaries

- Current planning access: read worktree files, run read-only worktree/WSL commands, and read/update this session plan and its task checklist.
- After approval of this revised plan: the user authorizes repository edits, local validation, and installation of missing validation tools in an isolated project/session environment. Prefer a Python virtual environment and a local binary directory; do not modify global PATH or install system-wide packages.
- Recommended handoff: **interactive implementation**, not autopilot/fleet. Previous mode transitions were followed by permission denials; the root cause has not been established. Do not assume that a conversational approval changes the runtime's permission controls.
- Keep implementation paused during this plan retry. After explicit implementation approval, make the first repository patch small: create `docs/01-architecture.md`, then inspect the file and diff. This is useful implementation work, not a throwaway permission-probe file. Only after that edit succeeds should module skeletons, dependency setup, or parallel work begin.
- Install dependencies only after adding the relevant dependency manifest or encountering a missing-tool failure in the selected validation command. Pin versions and verify downloaded tool checksums or signatures.
- System-wide changes, Docker daemon setup, remote-host changes, AWS deployments, and billable resources require separate permission. Installing a Docker client is not evidence that a usable container runtime exists.
- Begin with architecture and contracts, then implement independent workstreams against frozen interfaces. Validate and integrate each increment before proceeding; do not produce unconnected component scaffolds and call the platform complete.
- If permissions are denied again, report the specific operation and request access before retrying. Do not bypass a denial through a different tool or child session.
- If the user is unavailable when access is requested, preserve the blocked state. Do not report implementation as complete or repeat a mode transition that has not restored the missing permission.
- At completion, distinguish verified local checks, CI definitions that have not run, and environment acceptance checks blocked on separately authorized infrastructure.

## Confirmed decisions

- Repository scope: standalone platform at the repository root.
- Self-hosted infrastructure: existing Linux nodes, bootstrapped with k3s, Cilium, MetalLB, and Longhorn. VM creation through Proxmox, vSphere, or another provider is out of scope.
- Authentication: TLS gateway with per-tenant credentials and mTLS for remote collectors; raw backend ingestion and query services remain private.
- Retention: customized by tenant and datastream, with explicit values for metrics, logs, traces, and profiles. There is no implicit production retention default.
- Isolation: each tenant/datastream pair maps to a unique backend tenant ID, allowing backend tenant-level retention to implement independent datastream policies.
- Production architecture: include Kafka-compatible infrastructure when required by supported backend versions. Local Compose remains monolithic and Kafka-free.

## Architecture and implementation conventions

### Deployment profiles

1. **Docker:** monolithic Mimir, Loki, Tempo, and Pyroscope; Alloy; Grafana; MinIO; and an authenticated TLS gateway. Named persistent volumes, private internal networks, readiness checks, restart policies, and automated bucket provisioning. The local quickstart uses generated credentials and a local CA; it never exposes unauthenticated backend ports. Localhost-only access is the default. Production single-node use requires explicit public/private endpoint configuration and is documented as non-HA.
2. **Self-hosted Kubernetes:** Ansible installs k3s with its bundled Flannel, Traefik, and ServiceLB disabled before installing Cilium, MetalLB, Longhorn, a pinned Traefik release, and cert-manager. Support a single-node development profile and a separately sized multi-node production profile. Production stateful replicas and storage must span distinct nodes; preflight rejects impossible replica/disk/IP-pool configurations.
3. **AWS:** Terraform provisions a VPC across availability zones, EKS managed node groups, EBS CSI, S3, KMS/IAM integration, private service connectivity, and remote state prerequisites. Use on-demand capacity for stateful workloads and optional spot capacity for suitable stateless workloads. Traefik and cert-manager provide a consistent TLS ingress model, backed by an AWS load balancer and automated DNS. Use IRSA rather than static AWS credentials.

Use official Grafana charts, with a small platform chart for shared policies, gateways, provisioning resources, and integration fixtures. Use a version-pinned Strimzi-managed Kafka deployment on Kubernetes where selected backend architectures require it, with separate topics and access permissions per consumer backend. Kafka replication, disks, failure domains, monitoring, and recovery are part of the production profile, not an undocumented dependency.

### Shared configuration contracts

- Add `config/versions.yaml`, `config/platform.schema.json`, `config/tenants.example.yaml`, `config/network.yaml`, and environment examples.
- Use a small typed, tested configuration renderer/orchestrator to produce environment-specific backend settings, gateway routes, tenant overrides, Grafana provisioning, Ansible inputs, and Terraform inputs where needed. Deterministic output, strict validation, actionable errors, and no silent fallback values.
- Keep non-secret templates and examples in Git. Generated decrypted configuration stays in explicitly ignored directories with restrictive permissions and cleanup.
- Each tenant/datastream entry defines a stable backend ID, credential references, enabled signals, explicit retention for each enabled signal, ingestion/query limits, and approved collection/redaction policy.
- Treat backend ID changes as migrations, not renames. Detect duplicate IDs, delimiter ambiguity, unsupported durations, missing secret references, and credential mappings spanning unauthorized tenants.
- The network contract owns ports, protocols, purpose, direction, and scope. Generate firewall inputs and the documentation port table from it; CI checks consumers against the contract.
- Retain recognizable `terraform/`, `ansible/`, `helm/values/`, `docker-compose/`, `alloy-configs/`, `grafana/`, and `docs/` paths from the brief. Do not add fictitious Docker Helm values or empty Terraform modules solely to match an illustrative tree.

### Security and tenant boundaries

- Use SOPS + age as the baseline secret workflow for all environments, including automation for generation, encryption, decryption, rotation, and restricted runtime materialization. Production requires operator-provided age recipients and configured trust/DNS inputs; these are prerequisites, not click-ops.
- Implement a shared gateway authentication policy that binds credentials and remote collector certificate identity to permitted backend tenant IDs. Reject unknown, missing, conflicting, or spoofed tenant headers rather than trusting `X-Scope-OrgID` from clients.
- Separate ingestion-only collector credentials from query/provisioning credentials. Validate both HTTP and gRPC gateway behavior; support OTLP HTTP as a documented collector transport.
- Provision a Grafana organization per customer tenant, with datastream-specific data sources and organization-scoped dashboards, alerts, and credentials. Disable anonymous access. Verify that non-admin users cannot query another customer's sources or change trusted tenant mappings.
- Use Kubernetes default-deny NetworkPolicies with explicit DNS, gateway, object storage, Kafka, ring/gossip, control-plane, and exporter allowances. Keep object stores, distributor ports, and query endpoints private.
- Use backend TLS/mTLS where the chosen versions support it. Document actual plaintext exceptions and their network restrictions rather than claiming universal mTLS.
- Separate minimally privileged Alloy workloads from optional privileged/eBPF profiling. Instrumented SDKs, kube-state-metrics, and device exporters remain necessary where Alloy cannot obtain the signal itself.
- Apply collection-time PII redaction to logs, OTLP attributes, resource attributes, and metric labels. Drop sensitive data by default; use only verified supported transformations, with documented limits of hashing and profiling payload sanitization.
- Implement vulnerability scanning, secret scanning, SBOM generation, and Cosign signing/verification. Verify upstream signatures against explicit trusted identities where available; document provenance review and attestations for unsigned upstream artifacts rather than silently skipping verification.

### Retention and storage correctness

- Generate runtime tenant overrides for every tenant/datastream pair and enable the appropriate compactor/deletion workers.
- Verify retention keys against the exact pinned versions. Mimir supports per-tenant rather than per-series retention; Loki supports tenant/stream policies; Tempo has tenant compaction overrides.
- Pyroscope storage generation is a compatibility gate: current reference documentation marks `compactor_blocks_retention_period` as v1-storage-only. Select a supported version/storage mode with demonstrable tenant retention; do not apply a v1 option to v2 or claim unsupported behavior. If no supported mode meets the requirement, pause for an explicit architecture decision before implementing a substitute.
- Test policy selection and actual deletion, accounting for compaction, block boundaries, delete delays, versioned objects, and backups. Do not equate a rendered retention value with verified GDPR deletion.
- Provision separate object storage buckets and least-privilege identities for Mimir, Loki, Tempo, and Pyroscope, plus additional rule/alert buckets required by selected versions.
- Backend retention is authoritative. Bucket lifecycle must not delete live data earlier or move active queryable blocks into inaccessible archival tiers. Glacier is for a separate archival/export workflow, not a transparent live-storage optimization.
- Account for S3 noncurrent object versions and backups in deletion policies; keep Terraform state lifecycle separate from telemetry lifecycle.
- AWS state uses a separately bootstrapped S3 backend and DynamoDB locking as requested, with compatible Terraform pins, encryption, protection, and a documented migration to native S3 locking. State resources are never destroyed by ordinary environment teardown.

## Implementation todos

### 1. Establish architecture, contracts, and compatibility

Start repository implementation with `docs/01-architecture.md` and Terraform module skeletons as requested, then complete them component by component.

Resolve and pin a tested compatibility matrix covering Terraform/providers, Ansible collections, OS distributions/architectures, Kubernetes/k3s, Helm charts, images/digests, Kafka/Strimzi, Cilium, Longhorn, MetalLB, Traefik, cert-manager, SOPS, and age. Add provider/chart lockfiles to the repository. Confirm image distribution/licensing and MinIO server/client availability, including a pinned source-build path if required; do not silently substitute another storage product.

Create validated platform/tenant/network schemas, examples, renderer, secret workflow, prerequisites checks, and shared test fixtures. Record supported combinations and migration constraints, including Pyroscope retention and Kafka requirements.

### 2. Implement Terraform infrastructure and state bootstrap

Add complete modules for `aws-vpc-network`, `aws-eks`, `aws-s3-backends`, and `minio-backend`, each with variables, outputs, validation, and concise explanations of non-obvious decisions.

Add a separate AWS state-bootstrap root plus `environments/aws`, `environments/self-hosted-k8s`, and `environments/docker`. Backend configuration belongs to each executable root; avoid an ineffective top-level-only `backend.tf`.

For Docker and self-hosted nodes, use Ansible for host-local resources. Terraform manages explicitly available outside-cluster resources; no invented provider-dependent firewall/VM resources or fake empty applies. Define MinIO ownership so Terraform and Ansible never compete to manage the same bucket or identity.

Implement per-component IRSA policies, S3 lifecycle safeguards, security groups derived from the shared network contract, EKS access configuration, EBS encryption, and necessary DNS/controller permissions. Keep infrastructure provisioning separate from Helm installation to avoid provider initialization races against a cluster that does not exist yet.

### 3. Implement collection, gateway, and tenant provisioning

Create Docker, Kubernetes node, Kubernetes cluster, remote-cluster, VM, and external-service Alloy configurations. Split node and cluster discovery to avoid duplicate metrics; include backend self-monitoring, retries/backpressure, bounded queues, relabeling, tenant routing, and OTLP receivers.

Implement and test the shared authenticated gateway policy, certificate lifecycle and credential rotation, runtime tenant overrides, Grafana organization provisioning, and data source correlations across metrics/logs/traces/profiles. Do not enable cross-tenant query federation by default.

### 4. Deliver Docker Compose end to end

Add all runtime configurations, the complete Compose file, S3 override, `.env.example`, readiness-aware startup, persistent volumes, per-component service credentials, and non-root execution where compatible.

Use functional probes that exist in each pinned image; do not assume a distroless image contains curl or a shell. One-shot initialization services report completion and fail visibly. Separate initialization from long-running services.

Add an instrumented sample workload and deterministic telemetry fixtures covering metrics, logs, traces, and profiles. Quickstart generates local secrets/trust and boots a runnable sample tenant/datastream whose explicit example retention values are documented as examples rather than production defaults.

### 5. Implement Ansible host and cluster automation

Implement the requested inventories, roles, and playbooks for Docker installation/deployment, k3s bootstrap/join, external Alloy systemd services, node hardening, and firewall configuration.

Support explicitly tested Ubuntu/Debian and RHEL-family versions for Docker hosts; declare a narrower tested OS matrix for k3s/Longhorn if required. Pin artifacts and verify checksums. Provide handlers, validation, check-mode behavior where supported, `no_log` for secret-bearing tasks, and idempotent secret generation.

Validate SSH/admin allowlists before firewall changes; avoid locking out automation. Model Cilium/MetalLB/Longhorn kernel, disk, MTU, address-pool, and network prerequisites explicitly. Keep inventory secrets out of example files.

### 6. Deliver Kubernetes and EKS workloads

Add exact-version official Grafana chart values for self-hosted and AWS profiles, release orchestration, and the small supporting chart.

Wire MinIO/S3, TLS, gateway policies, tenant runtime configuration, service account roles, resource requests/limits, persistent storage, probes, disruption budgets, topology spread, autoscaling, and NetworkPolicies.

Include Kafka where required, Cluster Autoscaler on AWS, metrics-server for HPA, and HPA only for verified horizontally scalable query/Grafana configurations. Multi-replica Grafana requires shared PostgreSQL and supported unified-alerting HA configuration; provision and document those dependencies rather than scaling SQLite-backed instances.

Document small/medium/large resource budgets including Kafka, object storage, shared database, replication, and disk throughput. Keep production and reduced-footprint development values visibly distinct.

### 7. Provision dashboards and alerting

Ship cluster health, node health, Mimir/Loki/Tempo self-monitoring, and golden-signals dashboards, with supplemental Alloy, Pyroscope, Kafka, storage, and gateway health coverage.

Provision stable data source UIDs, variables, exemplars/correlations, alerting contact points, policies, recording rules, and multi-window burn-rate alerts backed by real sample workload metrics.

Use a configurable webhook contact point as the baseline. Local tests use a test receiver; production deployment requires a real endpoint and credentials. Include no-data/backend outage handling and an opt-in external dead-man endpoint to make shared-platform failure visibility explicit.

### 8. Complete external integrations and network examples

Ship deployable remote-cluster Alloy node/cluster manifests and VM registration automation, with tenant/datastream credentials and trust material generated through the same workflow.

Include static/file discovery and Consul/DNS discovery examples, supported basic-auth/OAuth2 collector syntax, and a push-versus-pull decision guide. Document NAT-friendly outbound OTLP/remote-write paths and restricted public versus VPN/peering/PrivateLink options.

Provide a small tested MQTT-to-metrics bridge example for constrained devices plus a direct OTLP HTTP example. Clarify that a central bridge performs tenant assignment and that constrained devices do not need deprecated Promtail.

Generate documented UFW/iptables rules, AWS SG/NACL examples, and Kubernetes policy expectations from the network contract, including stateful SG versus stateless NACL return traffic.

### 9. Add orchestration, CI, and acceptance coverage

Implement `make deploy-docker`, `make deploy-self-hosted`, `make deploy-aws`, corresponding destroy targets, `make lint`, and `make test`, backed by shared scripts rather than duplicated shell logic.

Fail fast for missing configuration, tools, credentials, unreachable endpoints, or unsupported versions. Preserve data by default on teardown; irreversible purge requires a separate explicit option and never removes state-bootstrap resources.

CI covers formatting/schema checks, Terraform init/validate and provider-mocked tests, Ansible lint/syntax and role tests, Helm lint/template/schema validation, Compose config validation, Alloy validation, dashboard/rule validation, security gates, and generated-artifact drift checks.

Bootstrap missing local validation tools into the authorized isolated environment. Run available static/unit checks through WSL without requiring a daemon. Container-dependent checks require an approved existing runtime or separately approved runtime setup; remote CI execution must not be represented as completed merely because workflow files exist.

Add Docker end-to-end tests, Kubernetes integration tests, and separately authorized self-hosted/AWS acceptance jobs. Lightweight cluster tests do not substitute for multi-node k3s/Longhorn or actual EKS/IRSA verification.

### 10. Complete documentation and release evidence

Finish README, `docs/00-quickstart.md`, and all ten numbered documents in the brief. Every guide has prerequisites, concrete commands, expected outcomes, troubleshooting, and rollback/teardown.

Explain maintenance, upgrades, certificate and secret rotation, Kafka recovery, backup/restore, retention/deletion semantics, cardinality/cost limits, monolithic-to-distributed migrations, and non-HA tradeoffs.

Verify commands against implemented targets. The Docker quickstart has an explicit prerequisites/download-speed/resource assumption and a measured cold/warm-start check against the brief's startup target; do not claim the target without measurement.

## Dependencies and execution order

Establish contracts and compatibility first. Infrastructure, collection/gateway logic, and Ansible host roles can then progress independently against those contracts.

Compose depends on collection/gateway contracts. Kubernetes delivery depends on infrastructure, collection/gateway configuration, and host bootstrap interfaces. Dashboards and external integrations depend on the collection and tenant contract. Acceptance and final documentation depend on all deliverables.

Write architecture first and update documentation alongside each component, rather than postponing all documentation until the end.

## Acceptance criteria and safety boundaries

- All three environments render valid, internally consistent configurations with exact supported version pins and no committed plaintext secrets.
- Terraform validates; authenticated plans are clean for supplied environment inputs. After deployment, a second plan is empty and a second Ansible run reports no unintended changes. Helm release inputs and generated files are deterministic.
- Smoke fixtures ingest and query all four signals, survive collector/backend restarts as designed, and exercise Grafana correlations.
- Two customer tenants with two datastreams each cannot ingest/query across unauthorized boundaries. Spoofed headers, bad/revoked certificates, and query attempts with write-only credentials fail.
- Different retention values are selected correctly for every signal and pair. Backend-specific deletion tests use supported durations and verify deletion after documented processing windows; long-duration tests are distinct from fast CI.
- PII fixtures are absent from received payloads and queried records; sensitive labels are not retained accidentally.
- Alerts reach the test receiver, real production destinations are configuration-gated, and malformed notification settings fail explicitly.
- Replica failures, storage persistence, allowed/denied network paths, HTTPS/OTLP gRPC routing, credential rotation, and backup/restore are exercised at the appropriate environment tier.
- Security checks fail closed with actionable findings and narrowly scoped, documented, expiring exceptions only.
- No AWS resources, DNS records, remote host changes, billable acceptance runs, or destructive purges occur merely to validate the plan. Implementation requests environment access/authorization when required.
- Report checks that actually ran separately from blocked cloud/host/runtime checks. Static validation is not evidence of a production-tested deployment.

## Research informing this plan

- Mimir retention and lack of per-series retention: https://grafana.com/docs/mimir/latest/configure/configure-metrics-storage-retention/
- Loki compactor retention and object-store lifecycle constraints: https://grafana.com/docs/loki/latest/operations/storage/retention/
- Tempo deployment modes, Kafka requirement in current distributed architecture, and tenant overrides: https://grafana.com/docs/tempo/latest/configuration/
- Pyroscope retention options and storage-generation restrictions: https://grafana.com/docs/pyroscope/latest/configure-server/reference-configuration-parameters/

These references establish architectural constraints, not a tested version matrix. Exact versions and storage-mode compatibility must be resolved in the first implementation milestone.
