# Design

## Context

See `proposal.md` for motivation. What exists, as observed in the
repository and on this machine:

- `render-contracts` writes the gateway configuration
  (`gateway/traefik-static.yaml`, `gateway/traefik-dynamic.yaml`), runtime
  overrides for the four backends, and `grafana/desired-state.json`. It does
  not write any backend's own configuration.
- `nighthawk/gateway.py` expects fixed mount paths under
  `/etc/nighthawk/gateway/` for the client CA, the server certificate, and
  the dynamic file. Routers are bound to `Host(gateway.hostname)`. There is
  no route for the Grafana UI.
- `render-collector` always copies a profile's host sources. The `docker`
  profile's sources need the Docker socket and cAdvisor's host mounts.
- `nighthawk/trust.py` issues collector certificates and one server
  certificate for `gateway.hostname`. `materialize-secrets` writes every
  referenced secret to `.materialized-secrets/<file>/<key>`.
- `config/tenants.example.yaml` uses `https://seaweedfs:8333` with CA
  reference `storage-ca` and one secret identity per backend
  (`mimir-storage`, `loki-storage`, `tempo-storage`, `pyroscope-storage`),
  one tenant/datastream (`example/application`) whose ingestion credential is
  certificate-bound, and entry points `local-gateway` (8443, loopback) and
  `grafana-gateway` (443, private).
- `config/versions.yaml` pins versions but no image digests, and no
  checksums for `sops`/`age`. `compose_architecture` is `classic` for Mimir
  and `monolithic` for Tempo.
- `docs/06-tenant-provisioning.md` lists the static settings the overrides
  depend on (runtime file, multitenancy, federation off, Loki compactor
  retention, Tempo defaults that must be changed).
- This machine: rootless Podman 5.8.4 and Docker Compose 5.3.1; no Docker
  daemon; the Podman API socket is not active. Images already present:
  Grafana 13.2.3, Loki 3.7.8, Mimir 3.2.1, Tempo 3.0.3. Not present:
  Pyroscope, Alloy, Traefik, SeaweedFS, a Python base image. `sops` and
  `age` are not installed. The network is slow (a 167 MB download took about
  half an hour during Phase 3). 4 CPUs, 15 GB RAM.

Operator decision for this change: acceptance runs the stack for real under
rootless Podman.

## Goals / Non-Goals

**Goals:**
- A stack that starts from a clean checkout with one command and whose every
  configuration file is rendered from the platform document.
- First runtime evidence for what Phases 1 to 3 left owed: gateway isolation
  behind a real Traefik, redaction of real payloads, overrides loaded by real
  backends, persistence across restarts.
- Honest separation of what was observed under Podman from what would need
  Docker Engine.

**Non-Goals:**
- Production single-node hardening and Docker-host installation (Phase 5).
- Retention-deletion acceptance. This change proves each backend loads its
  overrides and runs its retention worker, not that data is deleted after
  the retention period.
- Dashboards, alert rules, and the alert receiver (Phase 7).
- `make` targets and CI definitions (Phase 9). The quickstart is a
  `nighthawk` subcommand so those can wrap it later.
- High availability. Every service is a single instance.

## Decisions

### Rendered runtime directory, static Compose file

`docker-compose/docker-compose.yaml` is a committed, static file. It mounts
everything from two directories whose locations come from `.env`:
`NIGHTHAWK_RENDERED_DIR` (default `.generated/docker`, output of
`render-contracts` plus the rendered collector) and
`NIGHTHAWK_SECRETS_DIR` (default `.materialized-secrets`). Nothing
tenant-specific is in the Compose file, so adding a tenant is a re-render and
a reload, not a Compose edit. Alternative considered: generate the Compose
file. Rejected, because a static file can be reviewed, linted with
`compose config`, and tracked by `check-pins`.

Image references in the Compose file are literal `repository:tag@sha256:...`
strings. `config/versions.yaml` gains a `container_images` section
(repository, tag, digest per image) and one `tracked_consumers` entry per
image, so the existing `check-pins` enforces agreement. Digests are resolved
from the registry during implementation, and for the four images already
present they must match the local copies.

### Networks and exposure

Three networks: `edge` (Traefik, Alloy, Grafana, sample workload),
`backend` (Traefik, the four backends, Grafana is not on it), `storage`
(backends, SeaweedFS, storage init), plus `authz` (Traefik, auth service,
Alloy for its metrics scrape). All but `edge` are `internal: true`. Traefik
is the only service on both `edge` and `backend`, so Grafana and collectors
can reach backends only through it. The only `ports:` entry is Traefik's
`${NIGHTHAWK_BIND_ADDRESS:-127.0.0.1}:8443:8443`. Port 443 (the
`grafana-gateway` entry point) is reachable on the `edge` network only.
Traefik carries network aliases for `gateway.hostname` and the Grafana UI
hostname so in-network clients verify the same certificate names.

### Grafana UI through Traefik

The platform document gains a required `grafana` block:
`{hostname, admin_secret_ref}`. `gateway.py` adds one router per entry point
with rule `Host(grafana.hostname)`, no `forwardAuth`, no tenant header
middleware, and a service pointing at Grafana, whose address is a new
`gateway.upstreams.grafana` origin validated against a new network rule
(`gateway` to `grafana`, tcp/3000). The gateway server certificate gets both
hostnames. Because every telemetry router is bound to
`Host(gateway.hostname)`, a request for the UI hostname can only match the UI
router. Alternative considered: publish Grafana directly with its own TLS.
Rejected: it adds a second published port and a second TLS configuration,
and the architecture already routes the UI through the ingress.

`admin_secret_ref` also gives `provision-grafana` and Grafana itself one
source for the administrator password (`GF_SECURITY_ADMIN_PASSWORD__FILE`).

### Backend configuration

`nighthawk/backends.py` renders `backends/<name>.yaml` during
`render-contracts`, for `deployment: docker` only; other deployments print
that backend configuration is not rendered for them yet. Each renderer has a
reviewed-pin record (version and Compose architecture) like
`overrides.py`.

Common shape: single-binary target, HTTP on the port the network contract
gives for `gateway` to that backend, S3 storage from the bindings (endpoint
host and port, bucket, region, path style, CA file path), credentials from
environment variables expanded by the backend
(`-config.expand-env=true`), multitenancy on, runtime overrides file at a
fixed mount path with a short reload period, and the retention worker on.
Backend-specific points to confirm against each pinned reference during
implementation:

- Mimir 3.2.1: the flags that select the classic (Kafka-free) architecture
  in a single binary; three buckets (blocks, ruler, alertmanager).
- Loki 3.7.8: TSDB schema, `compactor.retention_enabled` with
  `delete_request_store`, `auth_enabled`, federation left off.
- Tempo 3.0.3: whether the monolithic target runs without Kafka at this
  version; `multitenancy_enabled: true`;
  `query_frontend.multi_tenant_queries_enabled: false`; OTLP receivers bound
  to `0.0.0.0`.
- Pyroscope 2.3.1: v2 storage in a single binary and its metastore
  persistence; no federation switch exists, which the docs already state.

If Tempo 3.0.3 or Mimir 3.2.1 cannot run Kafka-free in one process, that
contradicts `versions.yaml` and the architecture decision "Compose remains
monolithic and Kafka-free". The implementer stops and raises it instead of
adding Kafka.

Storage credentials reach a backend as environment variables set by a tiny
entrypoint-free mechanism: Compose `env_file` pointing at a per-backend file
the quickstart writes into the secrets directory (mode `0600`). The rendered
YAML contains only `${NIGHTHAWK_S3_ACCESS_KEY}`-style references.

### Local object storage

Storage identity secrets get a defined shape: a JSON object
`{"access_key": "...", "secret_key": "..."}`. `generate-storage-identity
--config --identity <secret ref>` creates one (20-character access key,
40-character secret) through `store_new_secret`.

`nighthawk/storage.py` renders, into the secrets directory because it
contains keys, the SeaweedFS S3 identity configuration: one identity per
binding identity with read, write, and list actions restricted to that
backend's buckets, and no anonymous identity. SeaweedFS runs as one
`weed server` process with S3 enabled over TLS using a server certificate
for the binding endpoint hostname. A `storage-init` one-shot creates the
buckets and exits; it is written to be repeatable. The exact identity file
format, per-bucket action syntax, TLS flags, and bucket-creation command are
confirmed against SeaweedFS 4.47 during implementation.

Trust: the local development CA signs the gateway server certificate,
collector certificates, and the storage server certificate. The quickstart
stores the CA certificate at `storage-ca` as well, since the contract treats
it as a separate reference. `issue-certificate` gains `--storage`, which
issues for exactly the storage binding endpoint hostnames; `--server` now
issues for the gateway and Grafana hostnames.

### Health checks and start order

Several of these images are distroless. For each pinned image the
implementation inspects what it contains and chooses, in order of
preference: the application's own health subcommand, a `wget`/`curl` that
exists in the image, or no in-container probe. Where no probe exists, the
service has no `healthcheck`, and a `wait-<service>` one-shot built from the
NightHawk image polls its readiness URL over the private network and exits
zero; dependents use `service_completed_successfully` on that one-shot. This
keeps "probe exists in the image" true without adding a shell to upstream
images.

Order: storage server healthy, then `storage-init`, then backends, then
their readiness, then auth service, then Traefik, then Grafana and Alloy,
then `grafana-init` (runs `provision-grafana`).

### NightHawk image

`docker-compose/nighthawk.Dockerfile` builds one image from the pinned
Python base (by digest) containing the `nighthawk` package and its pinned
requirements, running as a non-root user. It serves the auth service,
`wait-*` one-shots, `storage-init` where a Python S3 call is not needed,
`grafana-init`, and the fixture emitter. One image keeps the supply chain
small.

### Collector

`render-collector` gains `--otlp-only`: host source files are not copied, and
`pyroscope.receive_http` moves from the per-profile `profiles.alloy` into a
shared source that OTLP-only rendering still includes, so pushed profiles
keep working. The quickstart's Alloy is the `docker` profile rendered
`--otlp-only --self-monitoring` for `example/application`, so it needs no
socket and no privileges. A Compose profile `host-collection` adds a second
Alloy with the full `docker` sources, the runtime socket, and cAdvisor's
mounts; it is documented as Docker-specific and is not part of acceptance
under rootless Podman.

The example ingestion credential is certificate-bound, so the quickstart
collector presents its client certificate even on the local entry point.
That exercises the mTLS path on every run.

### Quickstart

`nighthawk/quickstart.py` implements `quickstart-docker` and
`teardown-docker` as Python orchestration over existing functions, with the
container CLI injected the way `runner` is in `secrets.py`, so the sequence
is unit-tested without containers.

1. Preflight: Compose command and runtime reachable, `sops`/`age` at pinned
   versions, published port free. Nothing is written before this passes.
2. `fetch-tools` (also a standalone command) downloads pinned `sops` and
   `age` into `.tools/` when absent, verifying the checksums newly recorded
   in the matrix, and the quickstart puts `.tools/` first on its own `PATH`.
3. Create what is missing, never replacing: an age recipient, gateway
   credentials, storage identities, the Grafana admin password, the CA, and
   the gateway, storage, and collector certificates.
4. `materialize-secrets`, `render-contracts`, `render-gateway-policy`,
   `render-collector`, the storage identity file, and the per-backend env
   files, into fresh temporary directories that atomically replace the
   previous render.
5. `compose up --wait`, then report addresses.

The container command is configurable (`NIGHTHAWK_COMPOSE`, default
`docker compose`). Under Podman the Compose CLI talks to the Podman API
socket, so the documentation gives the `podman system service` /
`DOCKER_HOST` setup, and implementation starts a user-level Podman socket
for the session rather than enabling a system service.

`teardown-docker` runs `compose down` without `--volumes`. `--purge` adds
`--volumes` and requires `--yes` or an interactive confirmation.

### Sample workload and fixtures

Two pieces with different jobs:

- `sample-workload/`: a small HTTP service instrumented with the
  OpenTelemetry SDK and the Pyroscope SDK, with its own pinned requirements
  and image, started by the Compose profile `sample`. It is for people
  looking at Grafana.
- `nighthawk emit-fixtures --run-id <id>`: standard-library only, sends OTLP
  over HTTP in its JSON encoding for metrics, logs, and traces, and pushes a
  small checked-in pprof profile, to the collector. It is for tests, which
  need exact, repeatable content, including the sensitive markers.

Alternative considered: use the SDK service for tests too. Rejected: SDK
batching and timing make exact assertions flaky.

### End-to-end tests

`tests/e2e/` holds `unittest` cases that are skipped unless
`NIGHTHAWK_E2E=1`, so the default suite stays container-free. They run
against a stack started from `tests/e2e/platform.yaml`: two tenants with two
datastreams each, one of them with different enabled signals, so isolation
is tested across four backend IDs. Cases:

- Fixture round trip for all four signals through Alloy and the gateway.
- Each credential can read only its own backend ID; a write credential
  cannot query; spoofed and multi-tenant `X-Scope-OrgID` values are refused.
- A request carrying a forged `X-Forwarded-Tls-Client-Cert` header without a
  TLS client certificate is refused; a revoked certificate is refused.
- OTLP gRPC trace export through the gateway.
- Sensitive markers absent from every query result; the free-text marker
  present, as documented.
- Stack restart and single-backend restart keep data.
- Each backend reports the rendered overrides, and a changed override
  appears after re-render.
- Host port scan: only the gateway port listens, on loopback.
- `provision-grafana` twice: second run reports no changes; correlations
  resolve to existing data source UIDs.

Results, including anything that could not be run or that behaved
differently under Podman, are written to `docs/07-docker-compose.md`.
`runtime_verified` flags in `versions.yaml` are set to `true` only for
components these tests exercised, and the docs say that the verification was
under Podman.

## Risks / Trade-offs

- [Risk] Image pulls are slow here and the stack is large; a pull or a
  cold start may exceed tool time limits. → Mitigation: pull each image as
  its own background step early, and build configuration while they
  download.
- [Risk] Compose under rootless Podman differs from Docker (health-gated
  `depends_on`, `internal` networks, `--wait`, port binding). → Mitigation:
  check each used feature early with a two-service probe file; record every
  workaround in the docs and keep the Compose file valid for Docker.
- [Risk] A pinned backend may not run Kafka-free in one process. →
  Mitigation: verify first, before writing the rest of that backend's
  configuration; stop and raise it if false.
- [Risk] 15 GB RAM with four backends, SeaweedFS, Grafana, and Alloy is
  tight. → Mitigation: explicit memory limits in the Compose file and
  measured usage in the docs.
- [Risk] The local CA signs gateway, storage, and collector certificates, so
  its key compromise breaks all three. → Mitigation: development profile
  only; production requires operator-supplied CAs, as already enforced.
- [Risk] Backend-to-gateway links stay plaintext inside private networks. →
  Mitigation: already listed by `gateway/routes.md`; networks are
  `internal`.
- [Trade-off] Storage credentials are environment variables inside backend
  containers. Accepted because the pinned backends take S3 keys from
  configuration, and the alternative is writing them into rendered YAML.
- [Trade-off] Two more required contract fields (`grafana`,
  `gateway.upstreams.grafana`). Accepted while only the example document and
  tests consume the contract.

## Migration Plan

No deployment exists. Platform documents add the `grafana` block and
`gateway.upstreams.grafana`; `validate` names them when missing. Storage
identity secrets created by hand before this change must be regenerated in
the JSON shape. Rollback is `teardown-docker` and reverting the change;
named volumes remain until purged.

## Open Questions

- Whether the `host-collection` profile works under rootless Podman's
  Docker-compatible socket. It is outside acceptance either way; the answer
  only changes a sentence in the docs.
