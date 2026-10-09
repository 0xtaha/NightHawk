# Proposal

## Why

`Plan.md` Phase 3 ("Implement collection, gateway, and tenant provisioning")
is the next unblocked gap. Phases 1 and 2 delivered the validated
tenant/credential/network contracts, the secrets lifecycle, and the Terraform
infrastructure, but `docs/02-configuration.md` still states the tooling "does
not yet ... generate complete backend-native configuration, enforce gateway
authentication, or implement redaction". Nothing yet turns a validated
tenant/datastream pair into a collector configuration, an enforced
credential-to-tenant binding, backend limits and retention, or a Grafana
organization. Phase 4 (Compose), Phase 6 (Kubernetes), Phase 7 (dashboards),
and Phase 8 (external integrations) all consume exactly those outputs.

## What Changes

- Add **Alloy collection configurations** under `alloy-configs/` for six
  profiles (Docker, Kubernetes node, Kubernetes cluster, remote cluster, VM,
  external service) plus a separate opt-in privileged eBPF profiling overlay,
  and a `render-collector` command that combines a profile with one
  datastream's contract entry (enabled signals, drop-field redaction, gateway
  endpoint, credential and certificate paths).
- Add the **tenant gateway**: a rendered Traefik configuration that terminates
  TLS/mTLS for HTTP and gRPC, and a small NightHawk forward-auth service that
  binds a credential (and, for remote collectors, a certificate identity) to
  exactly one backend tenant ID, separates ingest from query permission, and
  rejects conflicting or spoofed `X-Scope-OrgID` headers. Operator decision
  recorded for this change: Traefik plus an auth service, not Envoy or nginx.
- Add the **gateway trust lifecycle**: gateway credential generation, a
  locally managed development CA, collector and server certificate issuance
  with SPIFFE URI identities, overlap-based rotation, and revocation by
  identity removal or certificate fingerprint.
- Add **runtime tenant overrides** for Mimir, Loki, and Tempo alongside the
  existing Pyroscope fragment, version-gated the same way, covering retention
  and the contract's ingestion/query limits.
- Add **Grafana tenant provisioning**: a deterministic desired-state render
  (one organization per tenant, one data source per datastream and enabled
  signal, stable UIDs, cross-signal correlations) and an idempotent
  `provision-grafana` reconciler against the Grafana HTTP API. Cross-tenant
  query federation is not rendered.
- **BREAKING** (platform contract): `config/platform.schema.json` gains a
  required `gateway` object (hostname, client-CA reference, revoked
  certificate fingerprints), and the `metrics` signal's ingestion budget is
  expressed in samples per second instead of bytes per second, because Mimir
  enforces a per-tenant sample rate, not a byte rate. Only
  `config/tenants.example.yaml` and the tests consume this contract today.
- **MODIFIED**: the compatibility matrix's pinned inventory must also cover
  Grafana Alloy, newly required by the collection configurations.
- Extend `config/network.yaml` with the private gateway-to-auth-service,
  gateway-to-backend, and Grafana-to-gateway flows this change introduces.

## Capabilities

### New Capabilities
- `alloy-collection`: the per-profile Alloy configurations and the
  per-datastream collector render, including node/cluster discovery split,
  bounded delivery, collection-time redaction, and privileged-profiling
  separation.
- `tenant-gateway`: the authenticated TLS gateway policy that binds
  credentials and certificate identities to backend tenant IDs for HTTP and
  gRPC ingestion and query traffic.
- `gateway-trust-lifecycle`: gateway credential generation, certificate
  authority and certificate issuance, rotation, and revocation.
- `tenant-runtime-overrides`: rendered per-backend runtime overrides
  (retention and limits) for every tenant/datastream pair across all four
  signal backends.
- `grafana-tenant-provisioning`: per-tenant Grafana organizations,
  per-datastream data sources, and cross-signal correlations, rendered and
  reconciled.

### Modified Capabilities
- `compatibility-matrix`: "Complete pinned inventory" must also require an
  exact, source-verified pin for Grafana Alloy.

## Impact

- New: `alloy-configs/`, `nighthawk/collector.py`, `nighthawk/gateway.py`,
  `nighthawk/authz.py`, `nighthawk/trust.py`, `nighthawk/grafana.py`, new
  tests under `tests/`, and `docs/04-collection.md`, `docs/05-gateway.md`,
  `docs/06-tenant-provisioning.md`.
- Modified: `nighthawk/__main__.py` (new subcommands; `render-contracts`
  emits more artifacts), `nighthawk/config.py` (gateway block, metrics unit),
  `nighthawk/retention.py` (generalized into all-backend overrides),
  `config/platform.schema.json`, `config/tenants.example.yaml`,
  `config/network.yaml`, `config/versions.yaml`,
  `config/versions.schema.json`, `requirements.txt` (adds pinned
  `cryptography` for X.509 issuance and parsing), `docs/01-architecture.md`,
  `docs/02-configuration.md`, `docs/03-diagrams.md`, and existing tests that
  build platform documents.
- Not affected: Terraform modules and roots; the AWS network-rule parity
  test (the new rules are not `aws-` destined).
- Out of scope: running any of this. Compose wiring is Phase 4, Kubernetes
  and Helm wiring (including whether Traefik consumes the file-provider
  configuration or CRDs) is Phase 6, dashboards and alerts are Phase 7, and
  remote-cluster manifests, VM registration, and the MQTT bridge are Phase 8.
  No Docker daemon, Alloy, Traefik, or Grafana is available in this
  environment, so evidence is unit tests and static validation only;
  runtime isolation, redaction, and deletion acceptance remain owed by the
  phases that first run the stack.
