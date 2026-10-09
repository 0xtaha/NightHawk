# Quickstart: the whole platform on one machine

One command generates local secrets and certificates, renders every
configuration file from `config/tenants.example.yaml`, starts the Docker
Compose stack, and provisions Grafana.

This is a development setup: single node, not highly available, with a
locally generated certificate authority. The example document's retention
values are examples, not production defaults. It has been run under rootless
Podman only; see [what was verified](07-docker-compose.md#observed-results).

## Prerequisites

- Linux on x86-64 or arm64, with about 4 CPUs, 4 GB of free memory, and 5 GB
  of disk (the images take about 4 GB unpacked).
- Python 3.12 or newer with the repository's requirements installed
  ([configuration](02-configuration.md#prerequisites)).
- A container runtime with the Docker Compose CLI: Docker Engine, or rootless
  Podman with its API socket.
- Network access on the first run to pull the nine pinned images and build
  the NightHawk image. Nothing is pulled afterwards.
- Port 8443 free on the loopback address.
- `sops` 3.13.3 and `age` 1.3.2. To download both into the ignored `.tools/`
  directory, checked against the checksums in `config/versions.yaml`:

  ```console
  $ python -m nighthawk fetch-tools
  Installed sops, age into /path/to/NightHawk/.tools
  ```

  The quickstart looks in `.tools/` first. `sops` checksums come from its
  release's checksum file; `age` publishes none, so its checksums were
  computed from the release archives when the pin was made.

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

## Start

```console
$ python -m nighthawk quickstart-docker
Grafana admin password (shown once): <generated>
NightHawk is running.
  Gateway:  https://gateway.nighthawk.internal:8443  (published on 127.0.0.1:8443)
  Grafana:  https://grafana.nighthawk.internal:8443  (user admin)
  Collector datastream: example/application
  Retention values in the platform document are examples, not production defaults.
  Created this run: age key, credential example-ingest, ...
```

Before it writes anything, it checks the Compose command, the runtime,
`sops`, `age`, and the port, and lists everything that is missing.

What it creates, only when missing and never replacing:

- An age key at `secrets/local.agekey` and the encrypted
  `secrets/local.sops.yaml`. Both are ignored by Git.
- A secret for every credential, an S3 identity per backend, the Grafana
  admin password, a local certificate authority, and certificates for the
  gateway, object storage, and the collector.
- `.generated/docker/` (rendered configuration) and `.materialized-secrets/`
  (decrypted secrets, owner-only). Both are ignored by Git.

The Grafana admin password is printed once, when it is created. To read it
again:

```console
$ cat .materialized-secrets/runtime/grafana/admin-password
```

Running the command again is safe. It reuses everything, re-renders, tells
the auth service and collector to reload if their files changed, and ends in
the same state. Use `--no-build` to skip rebuilding the NightHawk image, and
`--compose "podman compose"` or `NIGHTHAWK_COMPOSE` to use another Compose
command.

## Reach it

The two host names are not in DNS. Add them to `/etc/hosts`:

```text
127.0.0.1 gateway.nighthawk.internal grafana.nighthawk.internal
```

Then open `https://grafana.nighthawk.internal:8443` and log in as `admin`.
Your browser will not trust the local CA; its certificate is at
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
    --user "example-query:$(cat .materialized-secrets/secrets/local.sops.yaml/example-query)" \
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

Telemetry, Grafana state, secrets, and certificates survive. Start again with
the quickstart and earlier data is still there.

To delete everything stored, including all telemetry:

```console
$ python -m nighthawk teardown-docker --purge --yes
```

Without `--yes` it asks for confirmation, and refuses when it cannot ask.
Purging removes the volumes only. To also discard the generated secrets, run
`python -m nighthawk clean-secrets` and delete `secrets/local.sops.yaml` and
`secrets/local.agekey`; the next quickstart then generates new ones, which
requires a purge first because stored data is tied to the old storage keys.

## Startup time and memory

No startup target is claimed. Measured so far on Fedora 43, rootless Podman
5.8.4, 4 CPUs, 16 GB RAM, with all images already present and the NightHawk
image already built: two cold starts (empty volumes) took 100 s and 95 s from
the command to its last line. Warm-start times and per-service memory have
not been recorded yet.

## Troubleshooting

- `prerequisites not met`: each line names one missing item. For `sops` or
  `age`, run `python -m nighthawk fetch-tools`.
- `port 127.0.0.1:8443 is already in use`: another process holds the port.
  The check is skipped when the port is held by this stack.
- `starting the stack failed ... not healthy: <service>`: read that
  service's logs with `docker compose ... logs <service>`.
- The first run seems stuck: it is pulling images or building the NightHawk
  image. The wait is ten minutes by default; raise it with `--timeout`.
- More: [Docker Compose troubleshooting](07-docker-compose.md#troubleshooting).
