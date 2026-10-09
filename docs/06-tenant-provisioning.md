# Tenant provisioning

## Status

`render-contracts` produces per-tenant runtime overrides for all four backends
and a Grafana desired state. `provision-grafana` applies that state through
Grafana's HTTP API. Everything here is unit-tested against the contract and
against an in-memory fake Grafana. Nothing has been run against a real
backend or a real Grafana; that is owed by the phases that first start the
stack.

## Prerequisites

- The Python environment from [configuration](02-configuration.md).
- A platform document that passes `validate`.
- For `provision-grafana` only: a reachable Grafana, a server-admin user, and
  secrets materialized with `materialize-secrets`.

## Runtime overrides

```console
$ python -m nighthawk render-contracts --config config/tenants.example.yaml --output .generated/contracts
Rendered non-secret contracts to .generated/contracts; no deployment was generated.
```

This writes `mimir-overrides.yaml`, `loki-overrides.yaml`,
`tempo-overrides.yaml`, `pyroscope-overrides.yaml`, and
`unenforced-limits.json`. Each override file has one entry per
tenant/datastream pair that enables that backend's signal, keyed by the pair's
backend ID. There is no default or wildcard entry, and a disabled signal has
no entry.

### Verified override keys

Each key was read from the backend's source at the pinned tag. Rendering
fails if `config/versions.yaml` pins a different version until the mapping is
reviewed again.

| Backend | Contract field | Override key | Unit | Source |
| --- | --- | --- | --- | --- |
| Mimir 3.2.1 | `retention` | `compactor_blocks_retention_period` | duration | [limits.go L293](https://github.com/grafana/mimir/blob/mimir-3.2.1/pkg/util/validation/limits.go#L293) |
| Mimir 3.2.1 | `ingestion_rate_samples_per_second` | `ingestion_rate` | samples per second | [limits.go L135](https://github.com/grafana/mimir/blob/mimir-3.2.1/pkg/util/validation/limits.go#L135) |
| Loki 3.7.8 | `retention` | `retention_period` | duration | [limits.go L215](https://github.com/grafana/loki/blob/v3.7.8/pkg/validation/limits.go#L215) |
| Loki 3.7.8 | `ingestion_rate_bytes_per_second` | `ingestion_rate_mb` | MB per second, 1 MB = 1048576 bytes | [limits.go L95](https://github.com/grafana/loki/blob/v3.7.8/pkg/validation/limits.go#L95) |
| Tempo 3.0.3 | `retention` | `compaction.block_retention` | duration | [config.go L184](https://github.com/grafana/tempo/blob/v3.0.3/modules/overrides/config.go#L184-L188) |
| Tempo 3.0.3 | `ingestion_rate_bytes_per_second` | `ingestion.rate_limit_bytes` | bytes per second | [config.go L69](https://github.com/grafana/tempo/blob/v3.0.3/modules/overrides/config.go#L69-L73) |
| Pyroscope 2.3.1 (v2) | `retention` | `retention_period` | duration | [retention.go](https://github.com/grafana/pyroscope/blob/v2.3.1/pkg/metastore/index/cleaner/retention/retention.go) |
| Pyroscope 2.3.1 (v2) | `ingestion_rate_bytes_per_second` | `ingestion_rate_mb` | MB per second | [limits.go L37](https://github.com/grafana/pyroscope/blob/v2.3.1/pkg/validation/limits.go#L37) |

Megabyte values are the declared bytes divided by 1048576 and rounded to six
decimal places, which is less than two bytes per second of error.

Mimir has no per-tenant byte-rate ingestion limit (only `ingestion_rate` in
samples, `request_rate`, and burst settings), which is why the contract
declares the metrics budget in samples per second.

### Limits no backend enforces

`query_concurrency` cannot be enforced per tenant by any of the four backends
at the pinned versions, so it is never written to an override file. Every
declared value is listed in `unenforced-limits.json` with the reason.

| Backend | Why it is not enforced | Source |
| --- | --- | --- |
| Mimir | `max_query_parallelism` bounds the sub-queries of one query; `max_queriers_per_tenant` bounds how many queriers serve the tenant. Neither caps concurrent queries. | [limits.go](https://github.com/grafana/mimir/blob/mimir-3.2.1/pkg/frontend/querymiddleware/limits.go#L245-L277) |
| Loki | Same two keys with the same meaning. | [limits.go L133](https://github.com/grafana/loki/blob/v3.7.8/pkg/validation/limits.go#L133-L146) |
| Tempo | The per-tenant `read` scope has no concurrency setting. | [config.go L163](https://github.com/grafana/tempo/blob/v3.0.3/modules/overrides/config.go#L163-L182) |
| Pyroscope | `max_query_parallelism` is used only by the v1 frontend, not the v2 read path. | [limits.go L86](https://github.com/grafana/pyroscope/blob/v2.3.1/pkg/validation/limits.go#L86) |

Treat `query_concurrency` as a recorded intent until a backend can enforce
it or the contract field is replaced. Do not rely on it for isolation.

### Static backend settings the overrides depend on

The overrides are inert unless the deployment sets these. Phases 4 and 6 own
them.

| Backend | Load the file | Also required |
| --- | --- | --- |
| Mimir | `runtime_config.file` (`-runtime-config.file`) | Compactor running. `multitenancy_enabled` on. Leave `tenant_federation.enabled` off. |
| Loki | `runtime_config.file` | `compactor.retention_enabled: true` and `compactor.delete_request_store` set, or retention deletes nothing. `auth_enabled` on. Leave `querier.multi_tenant_queries_enabled` off. |
| Tempo | `overrides.per_tenant_override_config` | `multitenancy_enabled: true` (default is off). Set `query_frontend.multi_tenant_queries_enabled: false`; it defaults to on. `ingestion.rate_strategy` defaults to `local`, so the rate applies per distributor unless set to `global`. OTLP receivers bind to localhost by default. |
| Pyroscope | `runtime_config.file` | v2 storage and `multitenancy_enabled`. The `|` tenant syntax is always parsed; the gateway rejects it. |

Limits the contract does not declare, such as burst sizes and series limits,
stay at backend defaults.

A rendered retention value is not proof of deletion. Deletion must be
observed after each backend's processing window, including noncurrent object
versions.

## Grafana organizations and data sources

`render-contracts` also writes `grafana/desired-state.json`:

- One organization per tenant, named by the tenant ID.
- One data source per datastream and enabled signal, named
  `<datastream> <signal>`.
- A stable UID, `nh-<m|l|t|p>-<first 32 hex characters of SHA-256(backend ID)>`.
  It depends only on the backend ID and signal, so it never changes when
  other tenants are added, and it fits Grafana's limit of 40 characters from
  `[a-zA-Z0-9_-]` for any backend ID.
- The URL is the gateway entry point named by `gateway.grafana_entry_point`.
  No data source addresses a backend or sets `X-Scope-OrgID`.
- Basic authentication with the datastream's query credential. The file holds
  the secret reference, never the secret.
- Correlations only between data sources of the same datastream, and only
  for enabled signals: traces to logs, metrics, and profiles; logs to traces;
  metric exemplars to traces.

### Apply

```console
$ python -m nighthawk materialize-secrets --config config/production.yaml
$ python -m nighthawk provision-grafana --config config/production.yaml \
    --url https://grafana.example.internal --admin-user admin \
    --admin-password-file /run/secrets/grafana-admin --dry-run
would create organization example
would create data source example/application logs (nh-l-e70e1f20bd525b64db01db8a011f15e6)
...
```

Remove `--dry-run` to apply. A second run prints `no changes`.

- `--dry-run` sends only read requests and needs no materialized secrets.
- `--prune` deletes NightHawk data sources (UID prefix `nh-`) that are no
  longer declared. Without it they are listed as undeclared. Data sources
  with other UIDs are never touched.
- Organizations are never deleted. An organization that is no longer declared
  is listed as undeclared.
- `--update-secrets` resends every data source password. Grafana does not
  return stored passwords, so a password changed with `rotate-secret` under
  the same credential ID is not detected otherwise. Rotating by declaring a
  new credential ID changes the user name and is detected.

### Verified Grafana API

Read from Grafana `v13.2.3` (the app version of the pinned chart).

| Use | Request | Source |
| --- | --- | --- |
| Find an organization | `GET /api/orgs/name/:name`, 404 when missing | [org.go](https://github.com/grafana/grafana/blob/v13.2.3/pkg/api/org.go#L64-L86) |
| Create an organization | `POST /api/orgs` with `{name}`, returns `orgId` | [org.go](https://github.com/grafana/grafana/blob/v13.2.3/pkg/api/org.go#L129-L158) |
| List organizations | `GET /api/orgs` | [api.go](https://github.com/grafana/grafana/blob/v13.2.3/pkg/api/api.go#L374) |
| Select the organization | `X-Grafana-Org-Id` header with basic auth | [service.go](https://github.com/grafana/grafana/blob/v13.2.3/pkg/services/authn/authnimpl/service.go#L503-L507) |
| Data sources | `GET`/`POST /api/datasources`, `PUT`/`DELETE /api/datasources/uid/:uid` | [api.go](https://github.com/grafana/grafana/blob/v13.2.3/pkg/api/api.go#L397-L405) |
| UID rule | at most 40 characters of `[a-zA-Z0-9_-]` | [shortid_generator.go](https://github.com/grafana/grafana/blob/v13.2.3/pkg/util/shortid_generator.go#L33-L46) |
| Tempo links | `tracesToLogsV2`, `tracesToMetrics`, `tracesToProfiles`, `serviceMap` | [provision.md](https://github.com/grafana/grafana/blob/v13.2.3/docs/sources/datasources/tempo/configure-tempo-data-source/provision.md) |
| Loki link | `derivedFields` | [loki configure](https://github.com/grafana/grafana/blob/v13.2.3/docs/sources/datasources/loki/configure/index.md) |
| Prometheus link | `exemplarTraceIdDestinations` | [prometheus configure](https://github.com/grafana/grafana/blob/v13.2.3/docs/sources/datasources/prometheus/configure/_index.md) |

The admin user must be a Grafana server admin using basic authentication;
API tokens cannot manage organizations. The user that creates an
organization becomes its admin, which is what lets later requests select it.
For an organization that already existed, add the admin user to it first.

The Loki, Prometheus, and service-map link fields are confirmed from
Grafana's documentation only, because those plugins' source is not in the
Grafana repository at that tag.

### Required Grafana settings

Phases 4 and 6 must set these; the desired state records the first one.

- `[auth.anonymous] enabled = false`.
- Customer users get the Viewer or Editor role, not organization Admin. An
  organization admin can edit data sources.
- A Grafana server admin is a trusted operator, not a tenant boundary.

Isolation does not rest on Grafana. Whatever a data source is edited to, the
gateway binds the query credential to one backend ID and rejects any other
tenant header.

## Troubleshooting

- `<backend> overrides were reviewed for X, not Y`: a backend pin changed.
  Re-check the keys above at the new tag, then update the reviewed version.
- `GET /api/orgs: Grafana answered HTTP 401`: the admin user or password file
  is wrong. `HTTP 403` means the user is not a server admin.
- A data source request answered `HTTP 401` or `403` for one organization:
  the admin user is not a member of that pre-existing organization.
- `materialized secret is not readable`: run `materialize-secrets` first.

## Rollback

Override files are plain files; restore the previous render. For Grafana,
re-run `provision-grafana` with the previous platform document. Nothing is
deleted unless `--prune` is given.
