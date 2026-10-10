# Quickstart: the whole platform on one machine

Three commands: start a development Vault, configure it, and run the
quickstart. The quickstart generates secrets into that Vault, obtains
certificates from it, renders every configuration file from
`config/tenants.example.yaml`, starts the Docker Compose stack, and provisions
Grafana.

This is a development setup: single node, not highly available, with secrets
and the certificate authority in a throwaway dev-mode Vault that forgets
everything when it stops. The example document's retention
values are examples, not production defaults. It has been run under rootless
Podman only; see [what was verified](07-docker-compose.md#observed-results).

## Prerequisites

- Linux on x86-64 or arm64, with about 4 CPUs, 4 GB of free memory, and 5 GB
  of disk (the images take about 4 GB unpacked).
- Python 3.12 or newer with the repository's requirements installed
  ([configuration](02-configuration.md#prerequisites)).
- A container runtime with the Docker Compose CLI: Docker Engine, or rootless
  Podman with its API socket.
- Network access on the first run to pull the nine pinned images and the
  Vault image, and to build the NightHawk image. Nothing is pulled afterwards.
- Ports 8443 and 8200 free on the loopback address.
- `openssl`, to generate the development Vault's token.

### Rootless Podman

Start the API socket for your session and point the Docker CLI at it:

```console
$ podman system service --time=0 unix://$XDG_RUNTIME_DIR/nighthawk/podman.sock &
$ export DOCKER_HOST=unix://$XDG_RUNTIME_DIR/nighthawk/podman.sock
```

Create `$XDG_RUNTIME_DIR/nighthawk` first if it does not exist. Under
rootless Podman the services run as container root, which is your own
unprivileged user outside the container
([details](07-docker-compose.md#podman-differences)).

## Start a development Vault

The platform keeps its secrets in a HashiCorp Vault that you provide, and gets
its certificates from that Vault's PKI engine. For this quickstart, run one in
dev mode from the image pinned in `config/versions.yaml`:

```console
$ mkdir -p .generated && (umask 077; openssl rand -hex 16 > .generated/vault-token)
$ docker run --detach --name nighthawk-vault --cap-add IPC_LOCK \
    --publish 127.0.0.1:8200:8200 \
    --env VAULT_DEV_ROOT_TOKEN_ID="$(cat .generated/vault-token)" \
    --env VAULT_DEV_LISTEN_ADDRESS=0.0.0.0:8200 \
    docker.io/hashicorp/vault@sha256:c2f666266f383d2cf424d86b8bb8ce7d065562173ffec2b476d762943608bb55 server -dev
```

Then give it the mounts, certificate authority, roles, and policy the
platform needs:

```console
$ python -m nighthawk bootstrap-dev-vault --vault-token-file .generated/vault-token \
    --config config/tenants.example.yaml --confirm-disposable-vault --ca-valid-days 365
Configured the development Vault: mount nighthawk-kv, mount nighthawk-pki, certificate authority, PKI role nighthawk-server, PKI role nighthawk-collector, policy nighthawk
```

The token is that container's root token. It is stored only in
`.generated/vault-token`, which Git ignores and only you can read; the
quickstart never copies it anywhere. See
[development Vault](02-configuration.md#development-vault) for what the
bootstrap does, and [what the platform needs from
Vault](02-configuration.md#what-the-platform-needs-from-vault) to use your
own Vault instead.

A dev-mode Vault keeps everything in memory. **When that container stops,
every secret and the certificate authority are gone**; see
[if the Vault is recreated](#if-the-vault-is-recreated).

## Start

```console
$ python -m nighthawk quickstart-docker --vault-token-file .generated/vault-token
Grafana admin password (shown once): <generated>
NightHawk is running.
  Gateway:  https://gateway.nighthawk.internal:8443  (published on 127.0.0.1:8443)
  Grafana:  https://grafana.nighthawk.internal:8443  (user admin)
  Collector datastream: example/application
  Retention values in the platform document are examples, not production defaults.
  Created this run: credential example-ingest, credential example-query, ...
```

`VAULT_TOKEN` in the environment works in place of `--vault-token-file`.

Before it writes anything, it checks the Compose command, the runtime, Vault
(reachable, unsealed, a supported version, the credential accepted, the
declared mounts and roles present and covering the declared names), and the
port, and lists everything that is missing.

What it creates, only when missing and never replacing:

- In Vault: a secret for every credential, an S3 identity per backend, and
  the Grafana admin password.
- From Vault's certificate authority: certificates for the gateway, object
  storage, and the collector. Their private keys are generated on this
  machine and never sent to Vault.
- `.generated/docker/` (rendered configuration) and `.materialized-secrets/`
  (secrets read from Vault and the certificates, owner-only). Both are
  ignored by Git.

The Grafana admin password is printed once, when it is created. To read it
again:

```console
$ cat .materialized-secrets/runtime/grafana/admin-password
```

Running the command again is safe. It reuses everything, re-renders, tells
the auth service and collector to reload if their files changed, and ends in
the same state. Use `--no-build` to skip rebuilding the NightHawk image.
`--compose` or `NIGHTHAWK_COMPOSE` selects another Compose command; only
`docker compose` has been tried.

The gateway is published on `127.0.0.1` only, on the port the network
contract declares for the `local-gateway` entry point (8443). `--bind-address`
accepts another loopback address such as `::1` and refuses anything else:
that entry point is scoped `loopback` in the gateway policy and accepts
ingestion without a client certificate, so it must not be reachable from
another machine. Publishing for other machines is not supported by the
quickstart.

## Reach it

The two host names are not in DNS. Add them to `/etc/hosts`:

```text
127.0.0.1 gateway.nighthawk.internal grafana.nighthawk.internal
```

Then open `https://grafana.nighthawk.internal:8443` and log in as `admin`.
Your browser will not trust the development Vault's certificate authority;
its certificate is at
`.materialized-secrets/runtime/grafana/gateway-ca.pem`. The `example`
organization has one data source per signal for the `application`
datastream.

Without editing `/etc/hosts`, `curl` can resolve the name itself:

```console
$ curl --cacert .materialized-secrets/runtime/grafana/gateway-ca.pem \
    --resolve grafana.nighthawk.internal:8443:127.0.0.1 \
    https://grafana.nighthawk.internal:8443/api/health
```

## Send something

### Deterministic fixtures

```console
$ docker compose --project-name nighthawk --file docker-compose/docker-compose.yaml \
    --env-file .generated/docker/compose.env \
    run --rm fixtures --tenant example --datastream application --run-id demo1
```

This sends one metric, four log lines, a two-span trace, and a CPU profile
through the collector, and prints what it sent as JSON. Query it back through
the gateway with the datastream's query credential:

```console
$ curl --cacert .materialized-secrets/runtime/grafana/gateway-ca.pem \
    --resolve gateway.nighthawk.internal:8443:127.0.0.1 \
    --user "example-query:$(cat .materialized-secrets/kv/nighthawk/local/example-query)" \
    --get --data-urlencode 'query=nighthawk_fixture_value{run_id="demo1"}' \
    https://gateway.nighthawk.internal:8443/metrics/prometheus/api/v1/query
```

The fixtures carry a marker value under each of the datastream's drop fields.
None of them comes back; a log line's `password=...` pair comes back as
`[REDACTED]`.

### Sample workload

```console
$ docker compose --project-name nighthawk --file docker-compose/docker-compose.yaml \
    --env-file .generated/docker/compose.env --profile sample up --detach --build sample
```

`sample-workload/` is a small loop instrumented with the OpenTelemetry and
Pyroscope SDKs. Give it about a minute, then look at the `application`
data sources in Grafana: `sample_orders_total` in metrics, `order processed`
lines in logs, `process-order` traces, and a `nighthawk-sample` CPU profile.
Logs carry the trace ID and profiles carry span IDs, which is what the
data sources' trace links use. It is optional and not part of the platform.

## Stop

```console
$ python -m nighthawk teardown-docker
Stopped and removed the NightHawk containers and networks.
Volumes, secrets, and certificates were kept.
```

Telemetry, Grafana state, and certificates survive, and so do the secrets as
long as the Vault container keeps running. Start again with the quickstart and
earlier data is still there.

To delete everything stored, including all telemetry:

```console
$ python -m nighthawk teardown-docker --purge --yes
```

Without `--yes` it asks for confirmation, and refuses when it cannot ask.
Purging removes the volumes only. To discard everything else as well:

```console
$ python -m nighthawk clean-secrets
$ docker rm --force nighthawk-vault
$ rm .generated/vault-token
```

### If the Vault is recreated

Stored telemetry is protected by the storage identities in Vault. If the
development Vault restarts, those identities are gone while the volumes are
still there. The quickstart then stops before generating anything:

```text
error: Vault holds no value for the storage identities (loki-storage, mimir-storage, pyroscope-storage, tempo-storage) that protect the data in existing volumes (...). A development Vault loses everything when it restarts. Clear the volumes with `python -m nighthawk teardown-docker --purge --yes`, then run the quickstart again.
```

Start and bootstrap the Vault again, purge as the message says, and run the
quickstart. The new Vault has a new certificate authority, so the quickstart
issues its certificates again and says so.

## Startup time and memory

No startup target is claimed. Measured on 2026-10-10 on Fedora 43 with
rootless Podman 5.8.4 and Docker Compose 5.3.1, 4 CPUs and 16 GB RAM, with
all images already present, the NightHawk image already built (`--no-build`),
and a bootstrapped dev-mode Vault 2.1.2 already running. Times are from the
quickstart command to its last line and include reading secrets from Vault,
rendering, start-up, and Grafana provisioning. One run each.

| Start | Time |
| --- | --- |
| First run: empty Vault, no volumes; generates every secret and certificate | 101 s |
| Cold: volumes purged, secrets and certificates kept | 94 s |
| Warm: after `teardown-docker`, volumes kept | 99 s |
| Re-run while the stack is already up | 17 s |

These match what was measured on 2026-10-09 with file-based secrets (cold 95
to 102 s, warm 93 to 97 s, re-run 17 s): reading secrets from a local Vault
and having three certificates signed adds no visible time. All four differ by
less than the spread seen between runs then, because most of the time is
fixed waits, not work: Mimir and Pyroscope each hold readiness back for a set
period after starting, and the stack starts in dependency order behind them.

Not included: starting the Vault container and `bootstrap-dev-vault`, which
took a few seconds with the image present, and a first run that also pulls
images and builds the NightHawk image. On this machine's connection the
pulls alone took over ten minutes.

Memory in use a few minutes after start, with the sample workload running
and little load, measured on 2026-10-09 and not repeated. This is idle usage,
not a peak under load. The dev-mode Vault container is not in the table.

| Service | Memory | Limit |
| --- | --- | --- |
| Grafana | 231 MB | 1 GB |
| SeaweedFS | 118 MB | 1 GB |
| Alloy | 63 MB | 1 GB |
| Mimir | 52 MB | 2 GB |
| Loki | 43 MB | 2 GB |
| auth service | 39 MB | 256 MB |
| Tempo | 25 MB | 2 GB |
| Pyroscope | 23 MB | 2 GB |
| Traefik | 19 MB | 512 MB |
| sample workload | 50 MB | 256 MB |

About 0.7 GB in total. The limits are generous starting points, not tuned
values.

## Troubleshooting

- `prerequisites not met`: each line names one missing item. A line about
  Vault means it is not running, not bootstrapped, or no credential was
  supplied; go back to [start a development Vault](#start-a-development-vault).
- `does not allow <name>; apply the rendered vault/pki-roles.json`: the
  platform document declares a hostname or collector identity that the role
  in Vault does not cover. Run `bootstrap-dev-vault` again.
- `requested validity outlives the certificate authority`: the authority was
  created with a shorter `--ca-valid-days` than `--certificate-valid-days`.
- `--bind-address ... is not a loopback address`: the quickstart publishes
  only on loopback; see [start](#start).
- `several ingestion credentials qualify` or `several query credentials are
  declared`: the datastream has two credentials of one permission. Name the
  one to use with `--credential`; see
  [rotation](05-gateway.md#rotate-a-gateway-credential-without-a-gap).
- `port 127.0.0.1:8443 is already in use`: another process holds the port.
  The check is skipped when the port is held by this stack.
- `starting the stack failed ... not healthy: <service>`: read that
  service's logs with `docker compose ... logs <service>`.
- The first run seems stuck: it is pulling images or building the NightHawk
  image. The wait is ten minutes by default; raise it with `--timeout`.
- More: [Docker Compose troubleshooting](07-docker-compose.md#troubleshooting).
