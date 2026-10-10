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

The same stack can be deployed to another machine as a production single
node, still without high availability: see
[Docker host deployment](08-ansible.md#docker-host-deployment). There the
configuration is rendered on a control machine and copied, and an extra
Compose file, `docker-compose/docker-compose.external.yaml`, publishes the
certificate-requiring external entry point on an address the operator
states. That file needs `NIGHTHAWK_EXTERNAL_BIND_ADDRESS` and does not
resolve without it. The local quickstart never uses it, so it publishes on
loopback only.

## Layout

`docker-compose/docker-compose.yaml` is static. Nothing tenant-specific is in
it. It mounts two directories named in the env file:

| Directory | Content | Written by |
| --- | --- | --- |
| `NIGHTHAWK_RENDERED_DIR` (default `.generated/docker`) | Non-secret: `contracts/` (output of `render-contracts`, including `backends/`), `overrides/`, `collector/`, `platform/`, `compose.env` | `quickstart-docker` |
| `NIGHTHAWK_SECRETS_DIR` (default `.materialized-secrets`) | Secret, owner-only: `kv/` (secrets read from Vault) and `runtime/` (per-service directories) | `quickstart-docker` |

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

### Vault

Vault is not part of the Compose stack and no container talks to it. The
quickstart, on the host, reads secrets from the Vault the platform document
declares and has certificates signed there, then writes the files above. The
"client CA", "gateway CA", and "storage CA" in the table are all the public
certificate of the authority in Vault's PKI mount; its private key never
leaves Vault. The Vault credential is not written to `compose.env`, to either
directory, or into any container's environment.

Because containers hold only files, the running stack keeps working when
Vault is unavailable. A re-render, a new secret, or a new certificate needs
it again.

## Networks and exposure

| Network | Internal | Members |
| --- | --- | --- |
| `edge` | no | Traefik, Grafana, Alloy, sample workload, tools |
| `backend` | yes | Traefik, the four backends, Alloy, `wait-backends` |
| `storage` | yes | The four backends, SeaweedFS, `storage-init` |
| `authz` | yes | Traefik, the auth service, Alloy |

- The only published port is Traefik's loopback-scoped entry point:
  `NIGHTHAWK_GATEWAY_PORT` (the `local-gateway` rule's port, 8443) bound to
  `NIGHTHAWK_BIND_ADDRESS`, which defaults to `127.0.0.1`. The quickstart
  writes both from the platform document and refuses a bind address that is
  not loopback, because that entry point admits ingestion without a client
  certificate. The Compose file itself does not check the address: if you
  write the environment file by hand, keep it on loopback.
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
| `alloy-host` (profile `host-collection` only) | Root, privileged, runtime socket, host mounts | cAdvisor, the node exporter, and Docker log collection need them. Docker Engine only; started only by `quickstart-docker --host-collection`; never run |
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
- The storage server certificate is requested for the binding endpoint host
  name (`issue-certificate --storage`) from the same authority in Vault's PKI
  mount as the gateway certificate. Backends trust that authority's public
  certificate, which is why a local binding declares `tls.trust: pki`.

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
| `host-collection` | `alloy-host` | Host and container collection with the full `docker` collector profile. `quickstart-docker --host-collection` renders that collector into `collector-host/` and starts the service; without the option the directory is emptied and the service is not started. The node exporter reads the host through the service's `/rootfs` and `/sys` mounts. Docker Engine only: the quickstart refuses the option under rootless Podman. Rendering is unit-tested and accepted by `alloy validate`; the service has never been run |

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
- **`host-collection`** expects the Docker socket and `/var/lib/docker`, so
  the quickstart refuses `--host-collection` under rootless Podman.

Nothing in the Compose file is Podman-only syntax, but it has not been
started under Docker Engine.

## Observed results

Run on 2026-10-10 on Fedora 43, rootless Podman 5.8.4, Docker Compose 5.3.1,
4 CPUs, 16 GB RAM, with `NIGHTHAWK_E2E=1 python -m unittest tests.e2e.test_stack`
against a stack started from `tests/e2e/platform.yaml` (two customers, two
datastreams each), with secrets and certificates from HashiCorp Vault 2.1.2
in dev mode.

All 19 cases passed. The suite starts its own dev-mode Vault from the pinned
image (container `nighthawk-e2e-vault`, loopback port 8210 or
`NIGHTHAWK_E2E_VAULT_PORT`) and bootstraps it, starts its own stack, runs
these, and purges both afterwards. It takes about ten minutes.

The suite was run several times that day while it was being extended. One
run had a failure that did not recur: Grafana answered a data source proxy
query with 404 `Unable to find datasource plugin`, while the same query
passed in the runs before and after and later queries in that same run
succeeded. The cause was not determined; the test now includes Grafana's log
in its failure message.

| Area | Observed |
| --- | --- |
| Four signals | Fixtures sent to the collector over OTLP and the Pyroscope ingest API came back through the gateway from Mimir, Loki, Tempo, and Pyroscope. The collector reached the gateway over TLS with basic authentication |
| Redaction | Marker values placed under every drop field as metric labels, resource, span, span-event, and log attributes, log body `key=value` and JSON pairs, and profile labels were absent from every query result, including `target_info`, where resource attributes become labels. The fixture series itself was returned, so the absence is not an empty result. One drop field, `user.email`, has a separator and arrives in the profile pipeline as the label `user_email`; it was removed too. The marker in free text without a key was still there, as documented |
| Tenant isolation | All four datastreams wrote every signal they enable (14 in total) straight through the gateway. Each owner read its own data back; the other three query credentials got nothing for any of them, 42 cross-reads in all, and 403 where the reader does not enable the signal. A write credential could not query and a query credential could not write. A different tenant, a `a\|b` tenant list, and a wrong tenant on a write were refused with 403; nothing landed in the other tenant |
| Authentication | No credential, an unknown credential, and a wrong secret all returned 401 |
| Routes | A disabled signal returned 403. Deletion, rule-management, readiness, configuration, and override endpoints returned 404 |
| Vault | Every credential, storage identity, and the Grafana password was generated into Vault's key-value mount and read back by the quickstart. The gateway and storage server certificates and the collector client certificate were signed by Vault's PKI authority from locally generated keys, and TLS between collector, gateway, Grafana, backends, and storage worked with that authority's certificate as the only trust |
| Recreated Vault | With the stack's volumes present, the dev-mode Vault was replaced by a new, bootstrapped one. The quickstart stopped before generating anything, named the four storage identities and the seven volumes, and wrote nothing to Vault. After `teardown-docker --purge --yes` it generated everything again and issued certificates from the new authority |
| Credential handling | The Vault token appeared in no file the quickstart wrote and in no container's environment |
| Client certificates | A certificate-bound credential was accepted with its Vault-signed certificate and refused without it. A forged `X-Forwarded-Tls-Client-Cert` header, in three spellings including the underscore alias, was refused |
| External entry point | With `docker-compose.external.yaml` added, the gateway also published its certificate-requiring entry point on a second loopback address (`127.0.0.2`, an unprivileged port standing in for 443). Ingestion there was accepted with the credential's client certificate and refused with 403 without one, for a certificate-bound credential and for a certificate-free one alike; the same certificate-free credential still delivered on the loopback entry point, and the data landed in its own datastream only. Without the override only the loopback entry point was published. This is the one part of a deployment to another machine observed here |
| Revocation | After revoking a certificate in Vault with `revoke-certificate`, listing the fingerprint it printed, and re-running the quickstart, that certificate was refused and a second certificate for the same identity still worked, with no restart |
| gRPC | An OTLP gRPC trace export through the gateway returned `grpc-status: 0` and the trace was queryable. A query credential got 403 and no credential got 401 on the same path |
| Backends | Mimir, Loki, Tempo, and Pyroscope, addressed directly on the private network without a tenant, returned 401; Pyroscope answered the same request with a tenant. Mimir, Tempo, and Pyroscope reported the rendered retention of every datastream for their signal (ten values), including one datastream whose four signals declare four different retentions |
| Override reload | A changed metrics ingestion rate appeared in Mimir within a minute; no container was recreated |
| Exposure | The only published port was Traefik's 8443 on 127.0.0.1 |
| Correlations | Every `datasourceUid` in the 18 provisioned correlation links was a data source of the same Grafana organization, and a datastream's trace link pointed at its own trace data source |
| Credential rotation | An ingestion and a query credential were each rotated by the runbook with no failed request at any step, and a query secret was rotated in place; see [gateway](05-gateway.md#observed-at-runtime) |
| Provisioning | Run again straight after the first start, Grafana provisioning printed `no changes`; and again after rotations and re-renders |
| Grafana | Both organizations existed. An organization's log data source returned the fixtures through Grafana, the gateway, and Loki; the other organization did not have that data source. The UI host name with a telemetry path did not reach a backend. A second `grafana-init` printed `no changes` |
| Persistence | Fixtures were still returned after restarting Loki, Mimir, Tempo, and Pyroscope one at a time, and all four signals after stopping and starting the whole stack. Mimir answered `empty ring` for a short while after its own restart before serving again |

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

Found the same way on 2026-10-10 and fixed: Grafana makes the first data
source created in an organization its default regardless of what is sent, so
provisioning straight after the first start "updated" one data source per
organization instead of reporting no changes. Provisioning now reads a newly
created data source back and corrects it at once; the suite's first case
checks this. A profile label for a drop field with a separator
(`user.email`, arriving as `user_email`) was not removed; the label rules
now match the sanitized name.

## Not verified

- **Retention deletion.** Each backend loads its retention override and runs
  its compactor, but no data has been aged out. A rendered or loaded value
  is not evidence of deletion.
- **Loki's loaded retention.** Loki has no endpoint that reports it.
- **Delivery while a backend or the gateway is down.** The collector's
  queues and retries are configured and unit-tested as text; nothing has
  been stopped while telemetry was being sent.
- **The scrape and log-file redaction pipelines.** The stack's collector
  receives pushed telemetry only.
- **Docker Engine.**
- **A `restricted-external` entry point** reached from another machine.
- **Load.** The memory limits are generous guesses (see the quickstart for
  measured idle usage), and the auth service has not been load-tested.
- **The `host-collection` profile and the external S3 override.**
- **Backup and restore**, and upgrades between pinned versions.
- **A production Vault.** Only a dev-mode Vault in a local container has been
  used: no namespace, no high availability, no sealed or failing Vault during
  a render, no operator-managed PKI hierarchy, and no AppRole login from the
  quickstart (AppRole login itself is covered by an integration test).

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
