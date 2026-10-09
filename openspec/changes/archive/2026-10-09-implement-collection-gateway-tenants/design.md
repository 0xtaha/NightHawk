# Design

## Context

See `proposal.md` for motivation and scope. Current state this design builds
on, as observed in the repository:

- `nighthawk/config.py` loads a platform document into frozen dataclasses
  (`Platform`, `Stream`, `Credential`, `SignalPolicy`). Each `Credential`
  already resolves to exactly one `backend_id` and one `permission`, and an
  optional `certificate_identity` of the form
  `spiffe://nighthawk/<tenant>/<datastream>/<collector>` (ingest only,
  unique). The validator already permits several credentials for one
  pair/permission; it only requires that both permissions are present.
- `render-contracts` writes non-secret artifacts into a new directory
  (`platform.json`, `network.json`, `ports.md`, `pyroscope-overrides.yaml`),
  refuses to overwrite, and never decrypts. `nighthawk/retention.py` gates
  the Pyroscope fragment on an exact reviewed pin
  (`2.3.1` / `v2` / `retention_period`).
- `nighthawk/secrets.py` provides the SOPS + age lifecycle behind a `doctor`
  gate, with an injectable `runner` so tests never need the binaries.
  `materialize` decrypts referenced secrets into `.materialized-secrets/`.
- `config/network.yaml` has two gateway rules (`local-gateway` tcp/8443
  loopback, `remote-gateway` tcp/443 restricted-external) and no rules for
  traffic behind the gateway.
- `config/versions.yaml` pins Traefik `3.7.13` and the Grafana chart
  (`13.3.1`, app `13.2.3`) but has no Alloy pin.
- The contract's limits (`ingestion_rate_bytes_per_second`,
  `query_concurrency`) are documented as "not yet translated into
  backend-native units".
- This machine has Python 3.14, `openssl`, and a Docker client without a
  reachable daemon. `alloy`, `traefik`, `sops`, `age`, `helm`, `kubectl`, and
  `terraform` are not installed. Tests are `unittest`, run with
  `python -m unittest discover -s tests -p "test_*.py"`.

Operator decisions taken for this change: the gateway is Traefik plus a
NightHawk forward-auth service, and all of Phase 3 is one change.

## Goals / Non-Goals

**Goals:**
- Every Phase 3 output is derived from the one validated platform document,
  so a tenant/datastream pair cannot have a collector, a gateway binding,
  backend overrides, and Grafana data sources that disagree.
- The tenant-isolation decision is a pure function that is exhaustively
  unit-tested without any running component.
- Everything Phases 4 and 6 need to start the stack is a rendered file or a
  command; they wire and run, they do not re-derive policy.

**Non-Goals:**
- Starting or integrating any service (Compose in Phase 4, Helm in Phase 6).
  Runtime proof of isolation, redaction, and deletion is owed by those
  phases and Phase 9 and is not claimed here.
- Kubernetes-native Traefik CRDs. This change renders Traefik's
  file-provider format only; Phase 6 decides whether to mount that or
  translate it.
- One collector serving several datastreams. A rendered collector serves
  exactly one datastream; routing by namespace or label to several
  datastreams would need selector fields the contract does not have.
- Backend static configuration (for example enabling the Loki compactor's
  retention or pointing a backend at its runtime-override file). This change
  documents which static settings the overrides depend on; Phases 4 and 6
  set them.
- Server-certificate automation on Kubernetes (cert-manager, Phase 6),
  dashboards and alert rules (Phase 7), remote-cluster manifests, VM
  registration, and the MQTT bridge (Phase 8).

## Decisions

### Platform contract additions

A required top-level `gateway` object is added to
`config/platform.schema.json`:

```yaml
gateway:
  hostname: gateway.nighthawk.internal      # collectors and data sources use https://<hostname>
  entry_points: [local-gateway, grafana-gateway]   # network.yaml rule IDs whose destination is `gateway`
  grafana_entry_point: grafana-gateway      # the selected entry point Grafana data sources use
  client_ca_secret_ref: gateway-client-ca   # CA certificate that verifies collector certificates
  client_ca_key_secret_ref: gateway-client-ca-key   # optional; only for a locally managed CA
  auth_service: http://authz:9180           # private address Traefik calls
  upstreams:                                # one per signal enabled anywhere in the document
    metrics: http://mimir:8080
    logs: http://loki:3100
    traces: {query: http://tempo:3200, otlp_grpc: h2c://tempo:4317, otlp_http: http://tempo:4318}
    profiles: http://pyroscope:4040
  revoked_certificate_fingerprints: []      # lowercase hex SHA-256 of the DER certificate
```

Entry points are network rules, and rules have different ports (8443 local,
443 remote and Grafana), so a URL is always `https://<hostname>:<port of the
chosen rule>`. Selected rules that share a port form one listener, which is
treated as exposed as its most exposed rule. `grafana_entry_point` was added
during implementation because the Grafana desired state is rendered
deterministically from the document and needs to know which listener to
address; collectors take the equivalent as a `render-collector` option.

`load_platform` gains cross-checks: every `entry_points` item is a network
rule with destination `gateway`; every upstream and `auth_service` port
matches a network rule from `gateway` to that component; an upstream exists
for every signal some datastream enables; the secret references resolve.
Addresses are explicit because they differ per deployment and there is no
safe default. Alternative considered: derive addresses from `deployment`.
Rejected, since that hides a per-environment value in code and contradicts
the contract's no-implicit-values rule.

`drop_fields` items are tightened to `^[A-Za-z0-9_.-]+$`. Field names are
interpolated into RE2 patterns in Alloy; restricting the alphabet removes
any need to reason about escaping differences between Python and Go.

The metrics signal replaces `ingestion_rate_bytes_per_second` with
`ingestion_rate_samples_per_second` (see "Runtime overrides").

`config/network.yaml` gains private rules from `gateway` to `auth-service`
(tcp/9180), to `mimir` (8080), `loki` (3100), `tempo` (3200, 4317, 4318),
and `pyroscope` (4040), and from `grafana` to `gateway` (tcp/443, private).
None has an `aws-` destination, so `tests/test_terraform_boundaries.py`'s
parity checks are unaffected.

### Gateway: one route model, two consumers

`nighthawk/gateway.py` holds a single route table. Each row is
`(signal, permission, transport, external match, upstream key, upstream
path rewrite)`. Both the Traefik configuration and the auth service's
documentation table are generated from it, so a route cannot exist in
Traefik without a policy class.

- External HTTP paths are signal-prefixed (`/metrics/...`, `/logs/...`,
  `/traces/...`, `/profiles/...`) and the prefix is stripped before
  forwarding. This avoids collisions between backends that share path
  shapes (`/api/...`). OTLP HTTP uses the standard `/v1/metrics`,
  `/v1/logs`, `/v1/traces` paths, rewritten to each backend's OTLP path.
  gRPC cannot be path-prefixed, so OTLP gRPC is routed by fully qualified
  service name and is offered for traces only; Mimir and Loki take OTLP over
  HTTP. The exact backend paths per row are checked against each pinned
  backend's API reference during implementation and recorded in
  `docs/05-gateway.md`.
- Only rows in the table are routed. There is no catch-all router, so
  backend admin, ring, and config endpoints are unreachable through the
  gateway.
- For every selected entry point and every row, the renderer emits one
  Traefik router with a `forwardAuth` middleware whose address is
  `<auth_service>/verify?entry=<rule-id>&signal=<s>&permission=<p>`. The
  class comes from operator-rendered configuration, never from the client.
  `authResponseHeaders` copies `X-Scope-OrgID` from the auth response, which
  replaces any client value on the forwarded request.
- TLS: every entry point is TLS-only. The TLS option uses
  `VerifyClientCertIfGiven` against the client CA, and a `passTLSClientCert`
  middleware (PEM form) forwards the verified certificate to the auth
  service. "If given" rather than "require" because query credentials and
  local collectors on the same entry point have no certificate; the auth
  service enforces who must have one.

Alternative considered: one router per credential with Traefik's own
`basicAuth` and a static header. Rejected: configuration grows with
credentials, htpasswd hashing on the ingest path is slow by design, and it
cannot bind a certificate identity to a credential.

### Auth decision as a pure function

`nighthawk/authz.py` separates `decide(policy, request_facts) -> Decision`
from the HTTP adapter. `request_facts` are: route class (entry, signal,
permission), the `Authorization` header, all `X-Scope-OrgID` header values,
and the forwarded certificate PEM. Order of evaluation, each failing closed:

1. Parse HTTP Basic credentials; credential ID is the username. Anything
   else is `401`.
2. Compare `sha256(secret)` to the policy digest with
   `hmac.compare_digest`; an unknown ID is compared against a fixed dummy
   digest so both failures take the same path and return the same `401`.
3. Permission and signal must match the route class, else `403`.
4. If the credential declares a certificate identity: a certificate must be
   present, its fingerprint must not be revoked, and its single URI SAN must
   equal the identity, else `403`. On an entry point whose network scope is
   `restricted-external`, an ingest credential without a declared identity
   is `403`.
5. If any `X-Scope-OrgID` is supplied: exactly one value, no `|`, equal to
   the bound backend ID, else `403`.
6. `200` with `X-Scope-OrgID: <backend_id>`.

Plan.md says to reject "missing" tenant headers. This design reads that as:
no request reaches a backend without a gateway-set header. A client may omit
the header, because the credential already determines the tenant and
requiring clients to restate it adds no security.

Plain SHA-256 rather than a password KDF: gateway secrets are generated
32-byte random tokens (see trust lifecycle), a minimum length of 32
characters is enforced at policy render, and a slow KDF on every ingest
request would cap throughput for no gain against high-entropy secrets.

The HTTP adapter is `http.server.ThreadingHTTPServer` from the standard
library, exposing `/verify`, `/healthz`, and `/metrics`. It loads the policy
bundle at start (exit non-zero if invalid) and reloads on `SIGHUP`, keeping
the last valid policy and setting a reload-failed gauge when the new file is
invalid. It runs as `python -m nighthawk serve-authz --policy <file>
--listen <addr>`. Alternative considered: an ASGI server. Rejected for now
because it adds runtime dependencies to a component whose logic is the pure
function; the adapter is thin enough to swap if load testing demands it.

The service trusts the forwarded-certificate header, so it must be reachable
only from Traefik. That is a deployment property (private network, the new
`gateway`-to-`auth-service` rule) and is documented as a hard requirement.

### Policy bundle and the secret boundary

`render-contracts` stays secret-free and now also writes
`gateway/traefik-dynamic.yaml`, `gateway/traefik-static.yaml`, and
`gateway/routes.md`. A new command, `render-gateway-policy --config ...
--secrets-dir .materialized-secrets --output <file>`, reads materialized
credential secrets and writes one JSON bundle (mode `0600`, atomic rename):
per credential its digest, backend ID, permission, enabled signals, and
certificate identity, plus revoked fingerprints and entry-point scopes. This
keeps the existing "ordinary validation never decrypts" requirement intact.

### Trust lifecycle

`nighthawk/trust.py`, using the `cryptography` package (new pinned
dependency; the standard library cannot parse or issue X.509):

- `generate-credential --config --credential <id> --recipient ...` creates
  `secrets.token_urlsafe(32)` and stores it through the existing
  `encrypt_secret`; it refuses when the key already exists (rotation uses
  the overlap procedure below, or `rotate-secret` when a gap is acceptable).
- `init-ca --config` creates an ECDSA P-256 CA, stores the key SOPS-encrypted
  at `client_ca_key_secret_ref` and the certificate at
  `client_ca_secret_ref`. `production` documents refuse unless
  `--confirm-local-ca` is given, mirroring
  `--confirm-production-recipients`.
- `issue-certificate --config --credential <id> --valid-days N --output-dir`
  issues a client certificate whose only SAN is the credential's URI
  identity (EKU clientAuth). `issue-certificate --config --server
  --valid-days N` issues a server certificate for `gateway.hostname` (EKU
  serverAuth). `--valid-days` is required. Keys are written `0600` into a
  directory that must be Git-ignored (`*.pem`, `*.key`, and
  `.materialized-secrets/` already are). The command prints the SHA-256
  fingerprint so it can later be revoked.
- All commands sit behind the existing `doctor` gate because they read or
  write SOPS files.

Rotation and revocation use the contract rather than new machinery:

- Credential rotation: declare a second credential for the pair, generate
  it, re-render and reload the policy, move the collector or data source,
  then delete the old credential and reload again.
- Certificate renewal: issue a new certificate for the same identity; the
  policy binds identity, not key, so nothing else changes.
- Revocation: add the fingerprint to
  `gateway.revoked_certificate_fingerprints`, or remove the identity.

Alternative considered: CRLs or OCSP at Traefik. Rejected: Traefik's CRL
support would need verifying per version, and the auth service already sees
the certificate, so a fingerprint list in the policy is simpler and tested
in the same pure function.

### Collectors: static sources, generated redaction and delivery

`alloy-configs/<profile>/` holds hand-written, formatted Alloy files split by
signal (`metrics.alloy`, `logs.alloy`, `profiles.alloy`). They contain only
discovery and source components, and they forward only to fixed component
names: `prometheus.relabel.redact`, `loki.relabel.redact`,
`pyroscope.relabel.redact`. `alloy-configs/profiling-ebpf/` holds the
privileged overlay.

`render-collector --config --tenant --datastream --profile [--self-monitoring]
--output <new dir>` copies the files for the datastream's enabled signals
and generates `datastream.alloy`, which contains everything that depends on
the contract: the OTLP receiver (gRPC and HTTP) wired to enabled signals
only, a memory limiter, the redaction components built from `drop_fields`,
and the gateway exporters. Because exporters exist only in the generated
file and are referenced only by redaction components, "redaction precedes
delivery" is a structural property that a test can assert by scanning
references.

- Redaction: `(?i)^(a|b|c)$` label drops for metrics, logs, and profiles; an
  OTTL `delete_matching_keys` over resource, scope, span, span-event, log,
  and datapoint attributes; and best-effort `key=value` / `"key":"value"`
  body replacement for logs. Free-text bodies and profile payloads are
  documented as not sanitized.
- Delivery: `prometheus.remote_write` with an explicit `queue_config` and
  WAL bounds, `loki.write` with explicit backoff and retry limits,
  `otelcol.exporter.otlphttp` with `sending_queue` and `retry_on_failure`
  limits, `pyroscope.write` with explicit backoff. Basic auth uses
  `password_file`; remote, VM, and external-service profiles add client
  certificate and key file paths and the CA file. No profile sets a tenant
  header. Paths and the gateway URL are generated from the contract
  (`https://<gateway.hostname>`), with file locations taken from documented
  environment variables.
- Node versus cluster: the node profile restricts discovery to its own node
  (`HOSTNAME`-based field selector) and owns kubelet, cAdvisor, node
  exporter, and pod logs. The cluster profile owns kube-state-metrics,
  API-server, Kubernetes events, and service-level scraping, and enables
  Alloy clustering so replicas share targets. A test asserts the two
  profiles declare disjoint discovery roles and job names.
- Self-monitoring: every profile scrapes the collector itself.
  `--self-monitoring` (Docker and cluster profiles only) adds scrapes of the
  backends, Traefik, and the auth service. The operator chooses which
  datastream receives it; nothing is added implicitly.

Alternative considered: generating whole configurations from Python
templates. Rejected: the source side is not contract-dependent, and plain
`.alloy` files can be formatted and reviewed with Alloy's own tooling.

### Runtime overrides

`nighthawk/retention.py` becomes `nighthawk/overrides.py` (keeping
`pyroscope_overrides` importable from its current location for the existing
tests). Each backend has a reviewed-pin record like the existing Pyroscope
one and a mapping from contract fields to override keys. `render-contracts`
writes `mimir-overrides.yaml`, `loki-overrides.yaml`, `tempo-overrides.yaml`,
`pyroscope-overrides.yaml`, and `unenforced-limits.json`.

Candidate mapping, each key to be confirmed against the pinned version's
configuration reference before it is marked reviewed:

| Contract field | Mimir 3.2.1 | Loki 3.7.8 | Tempo 3.0.3 | Pyroscope 2.3.1 |
| --- | --- | --- | --- | --- |
| `retention` | `compactor_blocks_retention_period` | `retention_period` | `compaction.block_retention` | `retention_period` (existing) |
| ingestion budget | `ingestion_rate` (samples/s) | `ingestion_rate_mb` | `ingestion.rate_limit_bytes` | `ingestion_rate_mb` |
| `query_concurrency` | `max_queriers_per_tenant` | `max_queriers_per_tenant` | to be confirmed | to be confirmed |

A contract value with no confirmed per-tenant key for a backend is written
to `unenforced-limits.json` (backend, backend ID, field, value, reason) and
listed in the documentation. It is never dropped silently and never
approximated.

The metrics budget changes unit because Mimir's per-tenant limit is a sample
rate. Converting bytes to samples would need an assumed bytes-per-sample
constant, which is exactly the kind of hidden value the contract forbids.
Megabyte limits are rendered from bytes with fixed formatting so output
stays deterministic. Burst sizes and other limits the contract does not
declare are left at backend defaults and listed as such in the docs.

### Grafana provisioning

`nighthawk/grafana.py` has two halves.

- `desired_state(platform)` is pure and secret-free and is written by
  `render-contracts` as `grafana/desired-state.json`: organizations named by
  tenant ID; per datastream and enabled signal a data source with type,
  name `<datastream> <signal>`, URL `https://<gateway.hostname>/<signal>`,
  basic-auth user = the query credential ID, a `secret_ref` instead of a
  password, and correlation settings (`tracesToLogsV2`, `tracesToMetrics`,
  `tracesToProfiles`, Loki `derivedFields`, Prometheus
  `exemplarTraceIdDestinations`) that reference UIDs of the same datastream
  only. When a pair has several query credentials, the lexicographically
  first ID is used.
- UID: `nh-<m|l|t|p>-<first 32 hex of sha256(backend_id)>`, 37 characters,
  because Grafana UIDs are limited to 40 characters and backend IDs may be
  63.
- `provision-grafana --config --url --admin-user --admin-password-file
  --secrets-dir [--dry-run] [--prune]` reconciles through the admin HTTP
  API: find or create each organization by name, then create or update data
  sources by UID within that organization. Organizations cannot be created
  by Grafana's file provisioning, which is why this is an API reconciler.
  The HTTP client is injectable, as `runner` is in `secrets.py`, so tests
  use a fake Grafana. Organizations are never deleted; undeclared data
  sources are reported and removed only with `--prune`.

Isolation does not rest on Grafana alone: even an organization admin who
edits a data source can only use credentials they hold, and the gateway
binds every credential to one backend ID. Anonymous access and customer
org-admin role assignment are deployment settings that the documentation
lists as required for Phases 4 and 6.

### Compatibility matrix

Add `collectors.alloy` (`version`, `source`, `runtime_verified: false`) to
`config/versions.yaml` and its schema. The version is resolved from the
upstream release page during implementation, as the earlier phases did for
their pins. `alloy-configs/README.md` states the pinned version and is added
to `tracked_consumers`.

### Validation approach

Unit tests cover all Python logic, including the full decision matrix for
`decide`. Rendered Alloy and Traefik files are checked structurally in
Python. Where a pinned Alloy binary can be fetched into an ignored `.tools/`
directory with a verified checksum, `alloy fmt` is run over the shipped and
rendered files; if it cannot, the limitation is recorded in the docs rather
than skipped silently.

### Findings during implementation

Checking the candidates against the pinned sources changed these details:

- `query_concurrency` is not enforceable per tenant on any of the four
  backends. Mimir and Loki only bound sub-query parallelism and the number of
  assigned queriers, Tempo has no such override, and Pyroscope's key is not
  used by the v2 read path. It is reported in `unenforced-limits.json` for
  every backend rather than approximated with `max_queriers_per_tenant`.
- Mimir 3.2.1 has no per-tenant byte-rate ingestion limit, confirming the
  samples-per-second contract field.
- Traefik's `forwardAuth` drops the forwarded-certificate header unless
  `trustForwardHeader: true` is set, and `passTLSClientCert` never removes a
  client-supplied copy. The spoofing protection is the entry point's default
  removal of client `X-Forwarded-*` headers plus `aliasHeadersStrategy:
  delete`; the rendered static configuration must not add
  `forwardedHeaders.insecure` or `trustedIPs`.
- Traefik binds TLS options per host name, so the client-certificate option
  is the `default` option and every router is bound to `gateway.hostname`.
- Query routes are allow-lists of read endpoints, not prefixes, because the
  backends serve deletion and rule-management APIs under the same prefixes.
- The logs redaction entry point is `loki.relabel.redact` (pattern-based
  label drop), followed by `loki.process.redact`. `loki.process` can drop
  structured metadata by exact name only; the shipped sources create none,
  and OTLP log attributes are redacted by pattern.
- `loki.write` and `pyroscope.write` have no stable queue argument in Alloy
  1.20.1. They are bounded by batch size and by retry count and backoff.
- Adding a key to an existing SOPS file uses `sops set --value-stdin`, so
  generated secrets and CA keys never appear in a process listing.
- Self-monitoring scrapes are added to the network contract as `collector`
  rules, including the proxy's metrics listener (`gateway-metrics`, 8082).

## Risks / Trade-offs

- [Risk] The auth service is custom code on the ingestion path and a single
  point of failure for all tenants. → Mitigation: it is stateless, fails
  closed, has no dependencies beyond the policy file, and exposes health
  and metrics. Replica count and load testing are owed by Phases 4, 6,
  and 9.
- [Risk] Traefik's forwarded-certificate header name, PEM encoding, and its
  handling of a client-supplied copy are assumed from documentation, not
  observed. → Mitigation: a task checks them against the pinned 3.7.13
  reference and the parser is isolated in one function; the spoofing
  scenario is listed as a runtime acceptance item for Phase 4.
- [Risk] Override keys and backend API paths are candidates until checked
  against pinned-version references. → Mitigation: version-gated rendering,
  one verification task per backend with the source recorded, and the
  unenforced-limits report for anything not confirmed.
- [Risk] Alloy component arguments may not match the pinned version, and no
  Alloy binary is installed here. → Mitigation: fetch the pinned binary for
  `alloy fmt` when possible; otherwise state plainly that the
  configurations are syntax-unverified.
- [Risk] Redaction of free-text log bodies is pattern-based and incomplete.
  → Mitigation: documented as a limit; structured fields, labels, and
  attributes are removed by exact key.
- [Risk] SHA-256 digests of weak operator-chosen secrets would be
  brute-forceable if the policy bundle leaked. → Mitigation: generated
  tokens by default, enforced minimum length, bundle written `0600`.
- [Trade-off] One collector per datastream means several collectors on a
  host shared by several datastreams. Accepted to avoid adding routing
  selectors to the contract in this change.
- [Trade-off] The contract change is breaking. Accepted now because only
  the example document and tests consume it.

## Migration Plan

Nothing is deployed, so there is no runtime migration. Existing platform
documents must add the `gateway` block and rename the metrics ingestion
field; `validate` names both when they are missing. Rollback is reverting
the change.

## Open Questions

- Which per-tenant key, if any, enforces `query_concurrency` on Tempo and
  Pyroscope at the pinned versions. Either answer is handled: a confirmed
  key is rendered, an unconfirmed one goes to the unenforced-limits report.
