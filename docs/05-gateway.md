# Tenant gateway

## Status

The gateway is Traefik plus a small NightHawk auth service. The Traefik
configuration, the auth policy, and the auth service are implemented and
unit-tested, including the complete allow/deny decision. Nothing here has
been run behind a real Traefik; see [what is still unproven](#what-is-still-unproven).

## How a request is handled

```text
collector or Grafana
   | TLS (client certificate verified if presented)
Traefik entry point  -- removes client-supplied X-Forwarded-* headers
   | route matches one row of the route table, else 404
   | passTLSClientCert  -- attaches the verified certificate
   | forwardAuth  ->  auth service /verify?entry=..&signal=..&permission=..
   |                    200 + X-Scope-OrgID: <backend ID>, or 401 / 403
   | Authorization header removed, path rewritten
backend (Mimir, Loki, Tempo, Pyroscope)
```

The backend tenant comes only from the credential. Traefik replaces any
client `X-Scope-OrgID` with the auth service's value.

## Prerequisites

- The Python environment from [configuration](02-configuration.md), with
  `sops` and `age` at their pinned versions for the trust commands.
- A platform document with a `gateway` block
  ([configuration](02-configuration.md#gateway-block)).
- The auth service reachable only from Traefik. It trusts the certificate
  header Traefik forwards, so anything else that can reach it could claim a
  certificate identity. This is a hard requirement, not a recommendation.

## Render the configuration

```console
$ python -m nighthawk render-contracts --config config/tenants.example.yaml --output .generated/contracts
Rendered non-secret contracts to .generated/contracts; no deployment was generated.
```

`gateway/` then contains:

| File | Use |
| --- | --- |
| `traefik-static.yaml` | Traefik static configuration: one TLS-only entry point per selected port, the metrics listener, and the file provider |
| `traefik-dynamic.yaml` | Routers, middlewares, services, and TLS options. Mount at `/etc/nighthawk/gateway/traefik-dynamic.yaml` |
| `routes.md` | The route table for this document, and the plaintext links behind the gateway |

Traefik expects these files, provided by the deployment:

| Path | Content |
| --- | --- |
| `/etc/nighthawk/gateway/client-ca.pem` | CA certificate that verifies collector certificates |
| `/etc/nighthawk/gateway/gateway-server.crt.pem`, `gateway-server.key.pem` | Server certificate for `gateway.hostname` |

Do not add `forwardedHeaders.insecure` or `forwardedHeaders.trustedIPs` to
the entry points. Traefik's default removal of client `X-Forwarded-*`
headers is what stops a client from sending its own certificate header.

## Routes

Only these are routed. There is no catch-all, so deletion, rule-management,
ring, and configuration endpoints are unreachable. Query routes are exact
allow-lists because backends serve write APIs under the same prefixes.

| Signal | Permission | Transport | External path | Sent to backend as |
| --- | --- | --- | --- | --- |
| metrics | ingest | Prometheus remote write | `/metrics/api/v1/push` | `/api/v1/push` |
| metrics | ingest | OTLP HTTP | `/v1/metrics` | `/otlp/v1/metrics` |
| metrics | query | HTTP | `/metrics/prometheus/api/v1/{query, query_range, query_exemplars, series, labels, label/<name>/values, metadata, rules, alerts, format_query, status/buildinfo}` | `/prometheus/api/v1/...` |
| logs | ingest | Loki push | `/logs/loki/api/v1/push` | `/loki/api/v1/push` |
| logs | ingest | OTLP HTTP | `/v1/logs` | `/otlp/v1/logs` |
| logs | query | HTTP | `/logs/loki/api/v1/{query, query_range, labels, label/<name>/values, series, index/stats, index/volume, index/volume_range, patterns, detected_fields, detected_labels, tail}` | `/loki/api/v1/...` |
| traces | ingest | OTLP HTTP | `/v1/traces` | unchanged, OTLP HTTP receiver |
| traces | ingest | OTLP gRPC | `/opentelemetry.proto.collector.trace.v1.TraceService/Export` | unchanged, OTLP gRPC receiver |
| traces | query | HTTP | `/traces/api/{echo, traces/<id>, v2/traces/<id>, search, search/tags, search/tag/<name>/values, v2/search/tags, v2/search/tag/<name>/values, metrics/query, metrics/query_range}` | `/api/...` |
| profiles | ingest | Pyroscope push | `/profiles/push.v1.PusherService/Push` | `/push.v1.PusherService/Push` |
| profiles | ingest | Pyroscope ingest API | `/profiles/ingest` | `/ingest` |
| profiles | query | HTTP | `/profiles/querier.v1.QuerierService/*` | `/querier.v1.QuerierService/*` |

OTLP over gRPC is offered for traces only. Mimir and Loki accept OTLP over
HTTP, not gRPC.

The Grafana UI is a separate case: requests for `grafana.hostname` on the
same listeners are forwarded to Grafana with no tenant authentication and no
tenant header, and Grafana performs its own login. Every telemetry route is
bound to `gateway.hostname`, so the UI host name can never reach a signal
backend.

Backend paths were read at the pinned tags:
[Mimir api.go](https://github.com/grafana/mimir/blob/mimir-3.2.1/pkg/api/api.go#L298-L299),
[Loki api_paths.go](https://github.com/grafana/loki/blob/v3.7.8/pkg/util/constants/api_paths.go),
[Tempo http.go](https://github.com/grafana/tempo/blob/v3.0.3/pkg/api/http.go#L69-L85),
[Pyroscope api.go](https://github.com/grafana/pyroscope/blob/v2.3.1/pkg/api/api.go#L281-L311).
The Tempo OTLP service name and `/v1/traces` path are the OTLP standard
served by Tempo's vendored receiver; they were not read in Tempo's own
source.

## Decision order

The auth service evaluates each request in this order. Every failure denies.

| Step | Check | Failure |
| --- | --- | --- |
| 1 | Route class (entry point, signal, permission) is known | 403 |
| 2 | Exactly one `Authorization: Basic` header; the user name is the credential ID | 401 |
| 3 | SHA-256 of the secret equals the policy digest, compared in constant time. An unknown ID is compared against a dummy digest, so it is indistinguishable from a wrong secret | 401 |
| 4 | The credential's permission matches the route | 403 |
| 5 | The route's signal is enabled for the credential's datastream | 403 |
| 6 | If the credential declares a certificate identity: exactly one verified certificate, not revoked, whose only URI SAN equals that identity | 403 |
| 7 | On a `restricted-external` entry point, an ingest credential without a certificate identity is refused | 403 |
| 8 | If `X-Scope-OrgID` was sent: exactly one value, equal to the bound backend ID. `a\|b` federation syntax and repeated headers fail | 403 |
| 9 | Allow with `X-Scope-OrgID: <backend ID>` | |

A client may omit `X-Scope-OrgID`; the credential already decides the tenant.
`Plan.md`'s "reject missing tenant headers" is read as: no request reaches a
backend without a gateway-set header.

For gRPC, a denial is a plain HTTP 401 or 403 without a `grpc-status`
trailer. gRPC clients report it as `Unauthenticated` or `PermissionDenied`.

Secrets are compared as plain SHA-256 digests, not a password hash. That is
sound only because gateway secrets are generated 32-byte random tokens;
`render-gateway-policy` refuses any secret shorter than 32 characters.

## Render and load the policy

The policy needs credential secrets, so it is a separate step from
`render-contracts`, which never decrypts.

```console
$ python -m nighthawk materialize-secrets --config config/tenants.example.yaml
$ python -m nighthawk render-gateway-policy --config config/tenants.example.yaml \
    --output .materialized-secrets/gateway-policy.json
Rendered gateway policy for 2 credential(s) to .materialized-secrets/gateway-policy.json.
$ python -m nighthawk serve-authz --policy .materialized-secrets/gateway-policy.json --listen 0.0.0.0:9180
Serving tenant gateway policy on 0.0.0.0:9180 with 2 credential(s).
```

The bundle holds digests, bindings, and revoked fingerprints, never a secret.
It is written with mode `0600` and replaced atomically.

- The service exits non-zero if the policy is missing or invalid at start.
- Send `SIGHUP` after re-rendering to reload. An invalid new policy is
  refused and the previous one stays in force; `/healthz` then reports
  `"policy_reload_failed": true` and `/metrics` reports
  `nighthawk_authz_policy_reload_failed 1`.
- `/healthz` always answers 200 while the service is serving, so a bad reload
  does not cause a restart into a state with no valid policy.
- `/metrics` also exposes `nighthawk_authz_decisions_total` by status.

## Trust lifecycle

Every command here checks `sops` and `age` first, as `doctor` does. Add
`--help` to any of them for the full options.

### First issuance (development)

```console
# 1. One secret per declared credential. The value is never printed.
$ python -m nighthawk generate-credential --config config/tenants.example.yaml \
    --credential example-ingest --recipient age1...
Generated and encrypted the secret for example-ingest in secrets/local.sops.yaml
$ python -m nighthawk generate-credential --config config/tenants.example.yaml --credential example-query

# 2. A local client CA. Key and certificate are stored SOPS-encrypted.
$ python -m nighthawk init-ca --config config/tenants.example.yaml --valid-days 365
Created the gateway client CA (SHA-256 fingerprint ...)

# 3. Certificates. --valid-days is required; there is no default.
$ python -m nighthawk issue-certificate --config config/tenants.example.yaml \
    --credential example-ingest --valid-days 30 --output-dir .materialized-secrets/collector
Issued .materialized-secrets/collector/example-ingest.crt.pem with key .materialized-secrets/collector/example-ingest.key.pem
SHA-256 fingerprint: ...
$ python -m nighthawk issue-certificate --config config/tenants.example.yaml \
    --server --valid-days 90 --output-dir .materialized-secrets/gateway
$ python -m nighthawk issue-certificate --config config/tenants.example.yaml \
    --storage --valid-days 90 --output-dir .materialized-secrets/storage
```

`--server` issues one certificate for the gateway and Grafana UI host names.
`--storage` issues one for exactly the host names of the local storage
binding endpoints, and is refused for cloud storage. `quickstart-docker` runs
all of these steps for you and renews a certificate that has less than seven
days left.

`--recipient` is needed only when the SOPS file does not exist yet; later
keys keep the file's recipients. A collector certificate is issued only for
an identity a credential declares, and carries that identity as its only
SAN. Keys are written with mode `0600`; `*.pem` and
`.materialized-secrets/` are ignored by Git. Record the printed fingerprint:
it is what you revoke.

`generate-credential` and `init-ca` refuse to replace an existing value.

### Production

A `production` document needs an operator-supplied client CA: put its
certificate at `client_ca_secret_ref`, omit `client_ca_key_secret_ref`, and
issue collector certificates with your own PKI using the declared
`spiffe://nighthawk/<tenant>/<datastream>/<collector>` URI as the only SAN.
`init-ca` refuses a production document unless `--confirm-local-ca` is given.

### Rotate a gateway credential without a gap

1. Declare a second credential for the same tenant, datastream, and
   permission, with its own secret reference. `validate` accepts it.
2. `generate-credential --credential <new id>`.
3. `materialize-secrets`, `render-gateway-policy`, then `SIGHUP` the auth
   service. Both credentials now work.
4. Switch the collector (`render-collector --credential <new id>`) or, for a
   query credential, run `provision-grafana`.
5. Remove the old credential from the document, re-render the policy, and
   `SIGHUP` again. The old credential now gets 401.

Rollback: until step 5 the old credential still works; revert step 4.

`rotate-secret` replaces a value in place instead. It is simpler but the old
secret stops working at the next policy reload, so there is a gap until the
collector has the new one.

### Renew a collector certificate

Issue a new certificate for the same credential into a new directory and
switch the collector to it. The policy binds the identity, not the key, so
nothing is re-rendered. Rollback: switch back to the old certificate while it
is still valid.

### Revoke

- One certificate: add its fingerprint (64 lowercase hex characters) to
  `gateway.revoked_certificate_fingerprints`, re-render the policy, `SIGHUP`.
  Other certificates for the same identity keep working.
- Every certificate of a collector: remove `certificate_identity`, or the
  credential, and re-render.

Rollback: remove the fingerprint and re-render.

## Plaintext links

Traefik to the auth service and to the backends is plaintext HTTP in the
example document. `gateway/routes.md` lists every such link for the rendered
document. They must stay on a private network. Use `https://` upstreams
where a backend is configured for TLS.

## Verified Traefik behaviour

Read from Traefik `v3.7.13` source.

| Behaviour | Source |
| --- | --- |
| `forwardAuth` returns the auth server's non-2xx status to the client | [forward.go](https://github.com/traefik/traefik/blob/v3.7.13/pkg/middlewares/auth/forward.go#L274-L312) |
| A header in `authResponseHeaders` is deleted from the request, then set from the auth response | [forward.go](https://github.com/traefik/traefik/blob/v3.7.13/pkg/middlewares/auth/forward.go#L322-L328) |
| `trustForwardHeader: true` is needed for the certificate header to reach the auth server | [forward.go](https://github.com/traefik/traefik/blob/v3.7.13/pkg/middlewares/auth/forward.go#L430-L517) |
| `passTLSClientCert` with `pem: true` sets `X-Forwarded-Tls-Client-Cert` to the base64 DER, leaf first, chain comma-joined | [pass_tls_client_cert.go](https://github.com/traefik/traefik/blob/v3.7.13/pkg/middlewares/passtlsclientcert/pass_tls_client_cert.go#L326-L355) |
| Entry points delete client `X-Forwarded-*` headers unless `insecure` or `trustedIPs` is set | [forwarded_header.go](https://github.com/traefik/traefik/blob/v3.7.13/pkg/middlewares/forwardedheaders/forwarded_header.go#L37-L49) |
| `aliasHeadersStrategy: delete` removes header-name aliases | [header-aliases.md](https://github.com/traefik/traefik/blob/v3.7.13/docs/content/security/header-aliases.md) |
| `clientAuthType: VerifyClientCertIfGiven`, `caFiles` | [tls.go](https://github.com/traefik/traefik/blob/v3.7.13/pkg/tls/tls.go#L32-L54) |
| TLS options apply per host name, not per router | [tls-options.md](https://github.com/traefik/traefik/blob/v3.7.13/docs/content/reference/routing-configuration/http/tls/tls-options.md) |
| An empty `customRequestHeaders` value removes the header | [headers.md](https://github.com/traefik/traefik/blob/v3.7.13/docs/content/reference/routing-configuration/http/middlewares/headers.md) |
| `h2c://` service URLs for cleartext gRPC backends | [v3 migration](https://github.com/traefik/traefik/blob/v3.7.13/docs/content/migrate/v3.md) |

## Observed at runtime

The end-to-end suite (`tests/e2e/`, see [Docker Compose](07-docker-compose.md))
ran against Traefik 3.7.13 under rootless Podman and observed:

- A forged `X-Forwarded-Tls-Client-Cert` header, including the underscore
  alias, without a TLS client certificate is refused for a
  certificate-bound credential.
- OTLP gRPC trace export passes `forwardAuth` and reaches Tempo; a denial on
  the gRPC path is a plain HTTP 401 or 403.
- Each credential reads only its own backend ID; spoofed and multi-tenant
  `X-Scope-OrgID` values, write-only queries, and query-only writes are
  refused; a revoked certificate is refused after a policy reload while a
  sibling certificate keeps working.

## What is still unproven

- Throughput of the auth service. It is a standard-library threaded HTTP
  server on the path of every request and has not been load-tested. It is
  stateless, so it can be replicated.
- Behaviour on a `restricted-external` entry point from outside the host.
  The suite reaches only the loopback entry point.

## Troubleshooting

- 401 for a credential you expect to work: the policy was not re-rendered or
  reloaded after the secret changed. Check `/healthz`.
- 403 for a remote collector: no client certificate was presented, its URI
  SAN differs from the declared identity, or it is revoked.
- 403 for a local collector on port 443: that entry point is
  `restricted-external`; use a certificate-bound credential.
- 404: the path is not in the route table.
- `gateway secret is shorter than 32 characters`: use `generate-credential`.
