# Collection

## Status

Collector configurations for Grafana Alloy 1.20.1 are implemented for six
source types plus a privileged profiling overlay. Their structure is
unit-tested and every shipped and rendered configuration passes the pinned
Alloy binary's own checks (see [validation](#validation)). One of them has
run: the `docker` profile without host sources, in the Compose stack, where
delivery of all four signals and redaction of OTLP payloads and pushed
profile labels were observed. The scrape and log-file pipelines, every other
profile's host sources, and resource bounds under an outage have not been
observed.

## Model

A collector serves exactly one tenant/datastream pair and authenticates with
that pair's ingestion credential. It never sets a tenant header; the gateway
derives the tenant from the credential. A host or cluster that feeds several
datastreams runs one collector per datastream.

A collector directory has two parts:

- **Sources**, copied from `alloy-configs/<profile>/`: discovery, scraping,
  and receivers, one file per signal. They forward only to the fixed
  receivers `prometheus.relabel.redact`, `loki.relabel.redact`, and
  `pyroscope.relabel.redact`.
- **`datastream.alloy`**, generated from the platform document: the OTLP
  receiver, redaction for the datastream's drop fields, and the gateway
  exporters. Exporters exist only here and are referenced only by redaction
  components, so nothing reaches the gateway unredacted.

## Prerequisites

- The Python environment from [configuration](02-configuration.md) and a
  platform document that passes `validate`.
- Alloy 1.20.1 on the target.
- The ingestion credential's secret in a file, and for remote profiles a
  client certificate and key from the [trust lifecycle](05-gateway.md#trust-lifecycle).

## Render a collector

```console
$ python -m nighthawk render-collector --config config/tenants.example.yaml \
    --tenant example --datastream application --profile docker \
    --entry-point local-gateway --output .generated/collector-docker
Rendered docker collector for example/application to .generated/collector-docker.
```

Use the same command with `--profile k8s-node`, `k8s-cluster`,
`remote-cluster`, `vm`, or `external-service`. The output directory must not
exist. Run it with `alloy run <directory>`.

| Option | Meaning |
| --- | --- |
| `--entry-point` | The gateway entry point (network rule ID) the collector connects to. Required when the document selects more than one |
| `--credential` | The ingestion credential ID. Required when several qualify, for example during a rotation |
| `--self-monitoring` | `docker` and `k8s-cluster` only. Also scrapes the signal backends, the auth service, and the gateway proxy |
| `--otlp-only` | Omits the profile's host sources. The collector then only receives pushed telemetry and scrapes itself, and needs no runtime socket, host mount, or privilege. Not available for `k8s-node` and `profiling-ebpf`, which have only host sources |

Only files for the datastream's enabled signals are written, and
`datastream.alloy` contains pipelines only for those signals.

### Environment variables

| Variable | Used by | Content |
| --- | --- | --- |
| `NIGHTHAWK_CREDENTIAL_FILE` | all | Path of the file holding the ingestion secret |
| `NIGHTHAWK_GATEWAY_CA_FILE` | all | CA that signed the gateway server certificate |
| `NIGHTHAWK_CLIENT_CERT_FILE`, `NIGHTHAWK_CLIENT_KEY_FILE` | collectors whose credential declares a certificate identity | Collector certificate and key |
| `HOSTNAME` | `k8s-node`, `profiling-ebpf` | The Kubernetes node name. The Alloy Helm chart sets it; a custom DaemonSet must inject it |
| `NIGHTHAWK_TARGETS_FILE` | `external-service` | Path or glob of a Prometheus file-discovery document |
| `NIGHTHAWK_GATEWAY_METRICS_ADDRESS` | `--self-monitoring` | `host:port` of the gateway proxy's metrics listener (network rule `collector-gateway-metrics`) |

No secret value is written into a configuration. The credential ID is, as the
basic-auth user name. The credential file must hold the secret without a
trailing newline: the OTLP exporter reads it through `local.file`, which does
not trim.

## Profiles

| Profile | Metrics | Logs | Profiles | Certificate |
| --- | --- | --- | --- | --- |
| `docker` | Host (`prometheus.exporter.unix`) and containers (cAdvisor) | Container logs through the Docker socket | SDK push to `:4040` | If the credential declares one |
| `k8s-node` | Kubelet, cAdvisor, and node exporter for this node | Logs of pods on this node from `/var/log/pods` | pprof scrape of pods on this node annotated `profiles.grafana.com/cpu.scrape: "true"`; no SDK push | If the credential declares one |
| `k8s-cluster` | kube-state-metrics, API server, services annotated `prometheus.io/scrape: "true"` | Kubernetes events | SDK push to `:4040` | If the credential declares one |
| `remote-cluster` | kube-state-metrics and annotated services | Pod logs through the Kubernetes API, and events | SDK push to `:4040` | Required |
| `vm` | Host | systemd journal and `/var/log/*.log` | SDK push to `:4040` | Required |
| `external-service` | Targets from a file-discovery document | OTLP only | SDK push to `:4040` | Required |

Every profile also receives OTLP over gRPC (`:4317`) and HTTP (`:4318`) for
the enabled signals among metrics, logs, and traces, and scrapes the
collector's own metrics when metrics are enabled.

The three remote profiles refuse to render unless the datastream has an
ingestion credential with a certificate identity. Remote ingestion without
one is rejected by the gateway anyway.

### What Alloy cannot collect by itself

- Application traces, application metrics, and SDK profiles: instrument the
  application and send OTLP or Pyroscope pushes to the collector.
- Kubernetes object state: install kube-state-metrics. The cluster profiles
  only scrape it.
- Device and service metrics: run the relevant exporter and list it in the
  `external-service` targets file, or annotate its Kubernetes service.

### Node and cluster split

Deploy `k8s-node` as a DaemonSet and `k8s-cluster` as a Deployment or
StatefulSet for the same datastream. They do not overlap:

- `k8s-node` uses only the `node` and `pod` discovery roles, each restricted
  to its own node with a field selector on `HOSTNAME`.
- `k8s-cluster` uses only the `endpointslice` role and the events API.
- Their scrape job names are disjoint.

`k8s-cluster` scrapes with `clustering { enabled = true }`, so replicas share
targets when Alloy runs with `--cluster.enabled`. Without that flag the block
has no effect and every replica scrapes everything; run one replica or enable
clustering. All replicas must use the same configuration.

`remote-cluster` is one self-contained, API-only collector with no DaemonSet
and no host mounts. Do not combine it with the other two for the same
cluster.

### Privileged profiling

```console
$ python -m nighthawk render-collector --config config/production.yaml \
    --tenant example --datastream application --profile profiling-ebpf \
    --entry-point remote-gateway --output .generated/collector-ebpf
```

`profiling-ebpf` is a separate collector for Kubernetes nodes that profiles
the pods on its node with `pyroscope.ebpf`. It renders only when the
datastream sets `allow_privileged_profiling: true` and enables profiles, and
it contains only the profiles pipeline. It must run as root in the host PID
namespace, either privileged or with the capabilities `BPF`, `PERFMON`,
`SYS_PTRACE`, `CHECKPOINT_RESTORE`, `SYS_RESOURCE`, `DAC_READ_SEARCH`, and
`SYSLOG`. No base profile contains an eBPF component. There is no eBPF
overlay for plain Docker hosts.

## Redaction

`collection.drop_fields` names are matched case-insensitively and removed
before delivery.

A label name cannot contain `.` or `-`, so a field such as `user.email`
reaches the metric, log-label, and profile pipelines as the label
`user_email`. Those three `labeldrop` rules therefore match each drop field
and its form with `.` and `-` replaced by `_`. OTLP attributes keep their
declared names and are matched as declared.

| Where | How |
| --- | --- |
| Metric labels | `labeldrop` in `prometheus.relabel.redact`, declared and sanitized names |
| Log labels | `labeldrop` in `loki.relabel.redact`, declared and sanitized names |
| Log message bodies | `key=value`, `key: value`, and JSON `"key": value` have the value replaced with `[REDACTED]` |
| OTLP resource, scope, span, span-event, data-point, and log attributes | `delete_matching_keys` in `otelcol.processor.transform.redact` |
| OTLP log bodies that are strings | `key=value`, `key: value`, and JSON `"key": value` matches are replaced with `[REDACTED]` |
| Profile labels | `labeldrop` in `pyroscope.relabel.redact`, declared and sanitized names |

The OTLP transform runs with `error_mode = "propagate"`: if a statement
fails, the payload is dropped rather than forwarded unredacted.

### Limits

- **Free text is not sanitized.** A secret that appears in a log message or
  attribute value without one of the key patterns above is forwarded.
- **Values are never inspected.** Only field names are matched. An email
  address stored under a field named `contact` is forwarded.
- **Nested attribute maps are not searched.** Only top-level attribute keys
  are removed.
- **Profile payloads are not sanitized.** Function names, file paths, and
  any strings inside a profile are forwarded as collected. Only labels are
  redacted.
- **Loki structured metadata is dropped by exact name.** Alloy's
  `stage.structured_metadata_drop` has no pattern form, so the declared
  names and their lower-case, upper-case, and capitalized forms are listed.
  The shipped sources create no structured metadata, and OTLP log attributes
  are redacted by pattern before they become structured metadata.
- **Nothing is hashed.** Fields are removed. Hashing low-entropy values such
  as emails would be reversible by guessing and is not offered.
- **Sanitized matching is limited to `.` and `-`.** A source that rewrites a
  field name some other way, for example by adding a prefix, is not matched.
- A drop-field list is not evidence that telemetry is clean. Test with
  sensitive fixtures; the Compose stack's end-to-end suite does.

## Delivery bounds

During a gateway outage each exporter holds a bounded amount and then drops.

| Exporter | Bound | What is dropped |
| --- | --- | --- |
| `prometheus.remote_write` | 10 shards of 10000 samples in memory; write-ahead log kept at most 2h; samples older than 30m are not sent; retry backoff at most 5s | Samples that age out |
| `loki.write` | One 1MiB batch; backoff at most 1m; 10 retries | The batch, after the retries. Sources are held back while it retries |
| `otelcol.exporter.otlphttp` | 1000 queued batches; backoff at most 30s; 5m total | Batches beyond the queue, or older than 5m |
| `pyroscope.write` | No queue; 10s timeout; backoff at most 1m; 10 retries | The profile, after the retries |
| OTLP intake | `otelcol.processor.memory_limiter` at 256MiB | New OTLP data is refused while over the limit |

`loki.write` and `pyroscope.write` have no stable queue-size argument in
Alloy 1.20.1, so their bound is the batch and the retry limit. Dropped data
is visible in the collector's own metrics, which every profile sends through
the same pipeline.

## Validation

The pinned binary was downloaded from the 1.20.1 release and its SHA-256
checked against the release's `SHA256SUMS`. With it:

- `alloy fmt --test` passes for every file in `alloy-configs/`.
- `alloy validate <directory>` passes for a rendered collector of every
  profile, including `profiling-ebpf` and `--self-monitoring`.

`alloy validate` checks syntax, component names, required and unknown
arguments. It does not check types, references between components, or
anything that needs a running target, so it is not proof the collectors work.
Running the collector found one thing it missed: Alloy 1.20.1 fails to build
`otelcol.auth.basic` with a `client_auth` block ("no credential source
provided"), so the generated configuration uses the component's top-level
`username` and `password` arguments instead.

The `docker` profile rendered `--otlp-only --self-monitoring` runs in the
Compose stack and has been observed delivering all four signals through the
gateway with mutual TLS, with every drop-field marker removed
([Docker Compose](07-docker-compose.md)). The other profiles' host sources
have not been run. That includes the `docker` profile's own host sources: the
quickstart can render and start them with `--host-collection`, with the node
exporter pointed at the host's `/proc` and `/sys` through the container's
mounts, but that needs Docker Engine and has not been run.

## Component reference

Every component used was checked against the Alloy 1.20.1 reference at
`https://github.com/grafana/alloy/tree/v1.20.1/docs/sources/reference/components/`.
All are generally available; none needs `--stability.level`.

| Component | Used for | Reference page |
| --- | --- | --- |
| `prometheus.exporter.self`, `.unix`, `.cadvisor` | Collector, host, and container metrics | `prometheus/` |
| `prometheus.scrape`, `prometheus.relabel`, `prometheus.remote_write` | Scrape, label redaction, delivery with `queue_config` and `wal` | `prometheus/` |
| `discovery.kubernetes`, `discovery.docker`, `discovery.file`, `discovery.relabel` | Discovery, node-scoped with `selectors` | `discovery/` |
| `loki.source.docker`, `.file`, `.journal`, `.kubernetes`, `.kubernetes_events`, `local.file_match` | Log sources | `loki/`, `local/` |
| `loki.relabel`, `loki.process` (`stage.structured_metadata_drop`, `stage.replace`), `loki.write` | Log redaction and delivery | `loki/` |
| `otelcol.receiver.otlp`, `otelcol.processor.memory_limiter`, `otelcol.processor.transform` | OTLP intake and redaction | `otelcol/` |
| `otelcol.auth.basic` (top-level `username` and `password`, the password read with `local.file`), `otelcol.exporter.otlphttp` | OTLP delivery with `sending_queue` and `retry_on_failure` | `otelcol/` |
| `pyroscope.receive_http`, `pyroscope.scrape`, `pyroscope.ebpf`, `pyroscope.relabel`, `pyroscope.write` | Profile sources, redaction, delivery | `pyroscope/` |

`delete_matching_keys` and `replace_pattern` are from the OTTL functions of
the bundled OpenTelemetry Collector
([v0.161.0](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.161.0/pkg/ottl/ottlfuncs/README.md)).
Loading a directory of `.alloy` files as one configuration is documented in
the `alloy run` reference.

## Troubleshooting

- `several gateway entry points are selected`: pass `--entry-point`.
- `several ingestion credentials qualify`: pass `--credential`.
- `this profile needs an ingestion credential that declares a certificate identity`:
  add `certificate_identity` to the datastream's ingestion credential.
- 403 from the gateway: see [gateway troubleshooting](05-gateway.md#troubleshooting).
- Duplicate series in a cluster: `k8s-cluster` has several replicas without
  `--cluster.enabled`, or `remote-cluster` was combined with another profile.
- Component name conflict at start: a file was added to the directory that
  reuses a component name. Names must be unique across the directory.

## Rollback

A collector directory is self-contained. Point Alloy back at the previous
directory. After any contract change, render a new directory instead of
editing `datastream.alloy`.
