# Proposal

## Why

`Plan.md` Phase 4 ("Deliver Docker Compose end to end") is the first phase
that runs the platform. Phases 1 to 3 produced validated contracts, a
secrets and trust lifecycle, collector configurations, the gateway
configuration and auth service, runtime overrides, and Grafana provisioning,
but every one of them is verified by unit tests and static checks only.
`docs/05-gateway.md` and `docs/04-collection.md` list runtime behaviour that
is still unobserved: header-spoofing protection behind a real Traefik, gRPC
through the gateway, redaction of real payloads, and whether the backends
load the rendered overrides. Nothing yet renders a backend's own
configuration or provisions local object storage, so the stack cannot start.

## What Changes

- Add a **Docker Compose stack** under `docker-compose/`: monolithic Mimir,
  Loki, Tempo, and Pyroscope, SeaweedFS, Traefik, the NightHawk auth service,
  Grafana, and Alloy, on private networks with only the gateway published,
  on loopback. Named volumes, restart policies, non-root execution where the
  image allows it, readiness-gated start order, and one-shot initialization
  services that fail visibly. An S3 override file replaces SeaweedFS with an
  external S3 endpoint.
- Add **backend static configuration rendering**: `render-contracts` gains
  each backend's configuration, derived from the platform document's storage
  bindings, with multitenancy on, cross-tenant federation off, the runtime
  override file loaded, and the retention workers the overrides depend on
  enabled.
- Add **local object storage provisioning**: SeaweedFS S3 served over TLS,
  one bucket per binding, and one least-privilege identity per signal
  backend, created by an initialization service from the platform document.
- Add a **Docker quickstart**: one command that generates local secrets and
  trust, renders everything, starts the stack, provisions Grafana, and
  reports readiness; plus teardown that keeps data and a separate explicit
  purge. Pinned `sops` and `age` are fetched with checksum verification.
- Add a **sample workload**: an instrumented service and a deterministic
  fixture emitter covering metrics, logs, traces, and profiles, including
  sensitive fixture fields for redaction checks.
- Add **end-to-end tests** run against the live stack under rootless Podman
  (operator decision for this change): all four signals ingested and
  queried, tenant isolation including spoofed headers and certificate
  headers, redaction, restart survival, and backends loading the overrides.
- **BREAKING** (platform contract): a required `grafana` block (UI hostname,
  admin secret reference), and storage identity secrets take a defined
  access-key/secret-key shape.
- **MODIFIED**: gateway server certificates may also name the Grafana UI
  hostname and the storage endpoint hostnames; the gateway routes the
  Grafana UI; collectors can be rendered OTLP-only; the compatibility matrix
  pins every Compose image by digest and `sops`/`age` by checksum.

## Capabilities

### New Capabilities
- `docker-compose-stack`: the Compose topology, exposure, persistence,
  start ordering, health, and initialization behaviour.
- `backend-static-configuration`: rendered per-backend configuration for the
  monolithic Compose profile.
- `local-object-storage`: SeaweedFS buckets, identities, and TLS for the
  local profiles.
- `docker-quickstart`: the one-command local bring-up, teardown, and purge.
- `sample-workload`: the instrumented sample service and deterministic
  telemetry fixtures.

### Modified Capabilities
- `gateway-trust-lifecycle`: server certificate issuance covers the Grafana
  UI hostname and storage endpoint hostnames.
- `tenant-gateway`: the gateway additionally routes the Grafana UI over TLS
  without tenant authentication.
- `alloy-collection`: a collector can be rendered without host sources
  (OTLP-only).
- `compatibility-matrix`: container images used by the Compose profile are
  pinned by digest, and `sops`/`age` downloads by checksum.

## Impact

- New: `docker-compose/` (Compose files, `.env.example`, image build
  contexts), `nighthawk/backends.py`, `nighthawk/storage.py`,
  `nighthawk/quickstart.py`, `sample-workload/`, `tests/e2e/`, new unit
  tests, `docs/00-quickstart.md`, `docs/07-docker-compose.md`.
- Modified: `nighthawk/__main__.py`, `nighthawk/config.py`,
  `nighthawk/gateway.py`, `nighthawk/trust.py`, `nighthawk/collector.py`,
  `config/platform.schema.json`, `config/tenants.example.yaml`,
  `config/network.yaml`, `config/versions.yaml`,
  `config/versions.schema.json`, `.gitignore`, `docs/01` to `docs/06`, and
  existing tests that build platform documents.
- Runtime effects during implementation: pulls the pinned Pyroscope, Alloy,
  Traefik, SeaweedFS, and Python base images, downloads `sops` and `age`
  into the ignored `.tools/`, and starts containers under rootless Podman on
  this machine. No cloud resources, no system-wide changes.
- Out of scope: Ansible Docker-host automation (Phase 5), Kubernetes
  (Phase 6), dashboards and alert rules (Phase 7), `make` targets and CI
  wiring (Phase 9), and long-duration retention-deletion acceptance. Docker
  Engine itself is not available here; differences between Podman and
  Docker that affect a result are recorded, not hidden.
