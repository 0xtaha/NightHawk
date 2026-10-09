# Docker Compose stack

## Status

The single-host stack in `docker-compose/` runs the whole platform: monolithic
Mimir, Loki, Tempo, and Pyroscope, SeaweedFS, Traefik, the NightHawk auth
service, Grafana, and an Alloy collector. It has been started and exercised
under **rootless Podman 5.8.4 with Docker Compose 5.3.1**, not under Docker
Engine. [Observed results](#observed-results) lists what was verified and
[Podman differences](#podman-differences) what had to change to get there.

It is a single-node, non-HA deployment. Retention deletion, load, and
production hardening are not verified.

To start it, see the [quickstart](00-quickstart.md). This document explains
what the quickstart builds.

## Layout

`docker-compose/docker-compose.yaml` is static. Nothing tenant-specific is in
it. It mounts two directories named in the env file:

| Directory | Content | Written by |
| --- | --- | --- |
| `NIGHTHAWK_RENDERED_DIR` (default `.generated/docker`) | Non-secret: `contracts/` (output of `render-contracts`, including `backends/`), `overrides/`, `collector/`, `platform/`, `compose.env` | `quickstart-docker` |
| `NIGHTHAWK_SECRETS_DIR` (default `.materialized-secrets`) | Secret, owner-only: `secrets/` (materialized) and `runtime/` (per-service directories) | `quickstart-docker` |

Directories are mounted, never single files, and the quickstart replaces
files one at a time. That is what lets a re-render reach running containers:
adding a tenant is a re-render and a reload, not a Compose edit.

`runtime/` gives each service only what it needs:

| `runtime/` directory | Mounted into | Content |
| --- | --- | --- |
| `gateway` | Traefik | Client CA, gateway server certificate and key, Traefik static and dynamic configuration |
| `authz` | auth service | `policy.json` (digests only) |
| `storage` | SeaweedFS, `storage-init` | S3 identity file, storage server certificate and key, bucket list |
| `storage-trust` | the four backends | Storage CA certificate only |
| `backends/<name>.env` | one backend each, as `env_file` | That backend's S3 access key and secret key |
| `grafana` | Grafana, `grafana-init` | Admin password, gateway CA |
| `collector` | Alloy | Ingestion secret, gateway CA, client certificate and key |

A backend can read neither another backend's keys, nor the storage server
key, nor the identity file.

## Networks and exposure

| Network | Internal | Members |
| --- | --- | --- |
| `edge` | no | Traefik, Grafana, Alloy, sample workload, tools |
| `backend` | yes | Traefik, the four backends, Alloy, `wait-backends` |
| `storage` | yes | The four backends, SeaweedFS, `storage-init` |
| `authz` | yes | Traefik, the auth service, Alloy |

- The only published port is Traefik's `8443`, bound to
  `NIGHTHAWK_BIND_ADDRESS`, which defaults to `127.0.0.1`.
- Traefik is the only route from `edge` to a backend. Grafana is on `edge`
  only, so its data sources can reach telemetry only through the gateway.
- Traefik has network aliases for the gateway and Grafana host names, so
  in-network clients verify the same certificate names as external ones.
- Alloy is also on `backend` and `authz`. That is for self-monitoring scrapes
  of the backends' and the auth service's metrics, and it means the
  platform's own collector could reach a backend directly. Treat it as a
  platform component, not a tenant's.
- Traffic from Traefik to the auth service and backends is plaintext HTTP on
  internal networks. `contracts/gateway/routes.md` lists every such link.

## Start order and health

```text
volume-init ─┐
seaweedfs (healthy) → storage-init (done) → mimir, loki, tempo, pyroscope
                                              → wait-backends (done) ─┐
authz (healthy) ──────────────────────────────────────────────────────┴→ traefik (healthy)
                                                                          → grafana (healthy), alloy
```

Each health check uses a binary that exists in that image. Mimir, Loki,
Tempo, Pyroscope, and Alloy images contain no shell and no HTTP client, so
they have no in-container health check; readiness is observed from outside
by one-shot services built from the NightHawk image.

| Service | Probe | Why |
| --- | --- | --- |
| `seaweedfs` | `wget` in the image, against the master and the S3 endpoint on loopback | Image has BusyBox |
| `authz` | `python -m nighthawk wait-http` against `/healthz` | NightHawk image |
| `traefik` | `traefik healthcheck` (ping on the private metrics listener) | Built into the binary |
| `grafana` | `wget` against `/api/health` | Image has BusyBox |
| `mimir`, `loki`, `tempo`, `pyroscope` | none in-container; `wait-backends` polls each `/ready` | Distroless |
| `alloy` | none in-container; `wait-alloy` polls `/-/ready` | No HTTP client |

`volume-init`, `storage-init`, `wait-backends`, and `wait-alloy` are one-shot
services. A dependent starts only when its one-shot exited zero, so a failed
initialization stops the bring-up. `storage-init` creates every bucket and
then confirms each exists; creating an existing bucket is a no-op, so it is
safe on every start.

`docker compose up --wait` without service names returns non-zero as soon as
any one-shot exits, even with status 0. The quickstart therefore waits on
`grafana` and `alloy` by name and runs `wait-alloy` and `grafana-init`
explicitly.

## Persistence

Named volumes: `seaweedfs-data`, `mimir-data`, `loki-data`, `tempo-data`,
`pyroscope-data`, `grafana-data`, `alloy-data`. `teardown-docker` keeps them;
only `--purge` removes them.

## Privileges

Every service runs as `NIGHTHAWK_UID:NIGHTHAWK_GID`, the user that owns the
owner-only secret files, with a restart policy and a memory limit. No default
service is privileged, adds capabilities, or mounts the runtime socket.

Exceptions:

| Service | Exception | Reason |
| --- | --- | --- |
| `volume-init` | Runs as root, with no network | Named volumes are created root-owned; it only runs `chown` on them |
| `traefik` | `net.ipv4.ip_unprivileged_port_start=0` in its own network namespace | Lets the non-root proxy listen on 443 for in-network clients |
| `alloy-host` (profile `host-collection` only) | Root, privileged, runtime socket, host mounts | cAdvisor and Docker log collection need them. Docker Engine only; not started by default and not exercised |
| all, under rootless Podman | `NIGHTHAWK_UID=0` | See [Podman differences](#podman-differences) |

## Backend configuration

`render-contracts` writes `backends/<name>.yaml` for a `docker` deployment
from the platform document's storage bindings. Rendering fails if
`config/versions.yaml` pins a version or Compose architecture other than the
one below. Each setting was checked by starting the pinned image with it.

| Backend | Setting | Why |
| --- | --- | --- |
| all | `multitenancy_enabled` / `auth_enabled: true` | A request without a tenant is rejected with 401 (observed) |
| all | Runtime override file under `/etc/nighthawk/overrides`, reloaded every 10s | Per-tenant retention and limits |
| all | S3 endpoint, bucket, region, path style from the bindings; keys from `${NIGHTHAWK_S3_ACCESS_KEY}` / `${NIGHTHAWK_S3_SECRET_KEY}` with `-config.expand-env=true` | No key is ever rendered |
| all | Usage reporting off | No telemetry about the deployment leaves it |
| Mimir 3.2.1 | `target: all`; `ingest_storage` left at its default, disabled | Classic, Kafka-free architecture in one process (observed) |
| Mimir | `tenant_federation.enabled: false` | No cross-tenant queries |
| Mimir | `activity_tracker.filepath` under `/data` | The default is a relative path a non-root user cannot write (observed failure) |
| Loki 3.7.8 | `compactor.retention_enabled: true` with `delete_request_store: s3` | Without it the per-tenant `retention_period` deletes nothing |
| Loki | `querier.multi_tenant_queries_enabled: false` | No cross-tenant queries |
| Tempo 3.0.3 | `-target=all` | One Kafka-free process (observed) |
| Tempo | `multitenancy_enabled: true` | Off by default |
| Tempo | `query_frontend.multi_tenant_queries_enabled: false` | On by default |
| Tempo | OTLP receivers on `0.0.0.0` | They bind to localhost by default |
| Tempo | Data under `/var/tempo` | Its live store writes there regardless of the WAL path |
| Pyroscope 2.3.1 | `-architecture.storage=v2` and metastore, database, and compaction directories under `/data`, as command-line flags in the Compose file | No YAML form was found for them at this version |
| Pyroscope | `-self-profiling.disable-push=true` | Its self-profile has no tenant and is rejected |

Pyroscope has no setting to disable the `|` multi-tenant syntax. The gateway
rejects it.

The storage CA reaches a backend as `SSL_CERT_FILE`, which Go uses instead of
the system bundle. That is safe here because a backend's only TLS peer is
object storage. The S3 override removes it.

## Local object storage

SeaweedFS 4.47 runs as one `weed server` process with the S3 gateway.
Behaviour below was observed with the pinned image.

- **TLS only.** With `-s3.cert.file` and `-s3.key.file` and no separate
  `-s3.port.https`, port 8333 serves HTTPS and answers plaintext with 400.
  (With `-s3.port.https` set, the plain port stays open.)
- **Identities.** `runtime/storage/s3.json` has one identity per binding
  identity with `Read`, `Write`, and `List` actions scoped to that backend's
  buckets, for example `Read:nighthawk-loki-chunks`. There is no anonymous
  identity. SeaweedFS's `Write` includes delete.
- **Observed permissions.** An identity can put, get, list, and delete in its
  own bucket. In another backend's bucket every one of those returns 403.
  An unsigned request and a wrong secret return 403. An identity cannot
  create a bucket through S3.
- **Buckets** are created by `storage-init` with `weed shell`
  `s3.bucket.create`, which is repeatable.
- The storage server certificate is issued for the binding endpoint host
  name (`issue-certificate --storage`) by the same local CA as the gateway
  certificate.

## External S3

```console
$ docker compose -f docker-compose/docker-compose.yaml -f docker-compose/docker-compose.s3.yaml \
    --env-file .generated/docker/compose.env config
```

`docker-compose.s3.yaml` removes `seaweedfs`, `storage-init`, the `storage`
network, and the storage volume, drops the storage CA so backends use the
system trust store, and gives backends an outbound network. It resolves and
is covered by a unit test. It has not been run: the platform contract only
accepts AWS storage with IRSA identities for the `aws` deployment, so there
is no Docker document that selects it yet.

## Profiles

| Profile | Adds | Notes |
| --- | --- | --- |
| `tools` | `grafana-init`, `fixtures` | One-shots for `compose run --rm` |
| `sample` | `sample` | The SDK-instrumented [sample workload](00-quickstart.md#sample-workload) |
| `host-collection` | `alloy-host` | Host and container collection with the full `docker` collector profile. Docker Engine only; needs a second rendered collector in `collector-host/`. Not exercised |

## Podman differences

These were needed to run under rootless Podman and are worth knowing before
trying Docker Engine.

- **Services run as container root.** Rootless Podman maps container UID 0 to
  the invoking user, who owns the secret files, so the quickstart sets
  `NIGHTHAWK_UID=0`. Outside the container that is your unprivileged user.
  Under Docker Engine the quickstart uses your own UID. A `keep-id` user
  namespace would have allowed a non-zero UID, but through the Docker API
  Podman intermittently created containers with a one-entry ID mapping that
  could not start.
- **SELinux.** Bind mounts carry `:z`. A private `:Z` label is wrong here
  because several services share the rendered directory.
- **The Podman API socket** must be running and `DOCKER_HOST` must point at
  it. See the quickstart.
- **`host-collection`** expects the Docker socket and `/var/lib/docker` and
  was not tried.

Nothing in the Compose file is Podman-only syntax, but it has not been
started under Docker Engine.

## Observed results

Run on 2026-10-09 on Fedora 43, rootless Podman 5.8.4, Docker Compose 5.3.1,
4 CPUs, 16 GB RAM, with `NIGHTHAWK_E2E=1 python -m unittest tests.e2e.test_stack`
against a stack started from `tests/e2e/platform.yaml` (two customers, two
datastreams each).

All 13 cases passed. The suite starts its own stack, runs these, and purges
it afterwards.

| Area | Observed |
| --- | --- |
| Four signals | Fixtures sent to the collector over OTLP and the Pyroscope ingest API came back through the gateway from Mimir, Loki, Tempo, and Pyroscope. The collector reached the gateway over TLS with basic authentication |
| Redaction | Marker values placed under every drop field as metric labels, resource, span, span-event, and log attributes, log body `key=value` and JSON pairs, and profile labels were absent from every query result. The marker in free text without a key was still there, as documented |
| Tenant isolation | Of four query credentials, only the owner of a log line could read it. A write credential could not query and a query credential could not write. A different tenant, a `a\|b` tenant list, and a wrong tenant on a write were refused with 403; nothing landed in the other tenant |
| Authentication | No credential, an unknown credential, and a wrong secret all returned 401 |
| Routes | A disabled signal returned 403. Deletion, rule-management, readiness, configuration, and override endpoints returned 404 |
| Client certificates | A certificate-bound credential was accepted with its certificate and refused without it. A forged `X-Forwarded-Tls-Client-Cert` header, in three spellings including the underscore alias, was refused |
| Revocation | After adding a fingerprint and re-running the quickstart, that certificate was refused and a second certificate for the same identity still worked, with no restart |
| gRPC | An OTLP gRPC trace export through the gateway returned `grpc-status: 0` and the trace was queryable. A query credential got 403 and no credential got 401 on the same path |
| Backends | Mimir, Loki, and Tempo, addressed directly on the private network without a tenant, returned 401. Mimir and Pyroscope reported the four and three rendered tenants at `/runtime_config`, and Tempo reported the rendered retention and burst |
| Override reload | A changed metrics ingestion rate appeared in Mimir within a minute; no container was recreated |
| Exposure | The only published port was Traefik's 8443 on 127.0.0.1 |
| Grafana | Both organizations existed. An organization's log data source returned the fixtures through Grafana, the gateway, and Loki; the other organization did not have that data source. The UI host name with a telemetry path did not reach a backend. A second `grafana-init` printed `no changes` |
| Persistence | Fixtures were still returned after restarting Loki alone, and all four signals after stopping and starting the whole stack |

Loki's loaded overrides were not observed directly: Loki 3.7.8 has no
endpoint that lists them. Its configuration loads the file, and tenant
enforcement and ingestion were observed.

Running the stack found defects that static checks had passed, all fixed:
Alloy 1.20.1 cannot build `otelcol.auth.basic` with a `client_auth` block;
Tempo refuses every write for a tenant whose override sets a rate without a
burst; Grafana's data source list omits `basicAuthUser`, which made
provisioning update everything on every run; the gateway had no route for
the Pyroscope ingest API that `pyroscope.write` forwards to; and JSON pairs
in OTLP log bodies were not redacted.

## Not verified

- **Retention deletion.** Each backend loads its retention override and runs
  its compactor, but no data has been aged out. A rendered or loaded value
  is not evidence of deletion.
- **Docker Engine.**
- **A `restricted-external` entry point** reached from another machine.
- **Load.** The memory limits are generous guesses (see the quickstart for
  measured idle usage), and the auth service has not been load-tested.
- **The `host-collection` profile and the external S3 override.**
- **Backup and restore**, and upgrades between pinned versions.

## Troubleshooting

- `container uses ID mappings ... but doesn't map UID 0`: a leftover
  container from an older attempt with `keep-id`. Run `teardown-docker` and
  start again.
- A backend exits with `permission denied` under `/data`: `volume-init` did
  not run, or the volume predates a change of `NIGHTHAWK_UID`. Start through
  the quickstart, or purge.
- A backend cannot reach storage with an `x509` error: the storage
  certificate does not match the binding endpoint host name, or
  `storage-trust/ca.pem` is stale. Re-run the quickstart.
- Tempo logs `RATE_LIMITED ... burst: 0 bytes`: the overrides file predates
  the burst fix. Re-render.
- Logs per service: `docker compose --project-name nighthawk --file
  docker-compose/docker-compose.yaml --env-file .generated/docker/compose.env
  logs <service>`.
