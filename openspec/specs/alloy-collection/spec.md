# alloy-collection

## Purpose

Provides the Grafana Alloy collector configurations that gather metrics, logs,
traces, and profiles from each supported source type and deliver them to the
tenant gateway for exactly one tenant/datastream pair, with redaction applied
before telemetry leaves the collector.

## Requirements

### Requirement: Collector profiles for every supported source type
The system SHALL ship an Alloy configuration profile for each of: a Docker
host, a Kubernetes node, a Kubernetes cluster, a remote Kubernetes cluster, a
virtual machine, and an external service. The system SHALL provide a command
that renders a deployable collector configuration from one profile and one
tenant/datastream pair of a validated platform document.

#### Scenario: Rendering a collector for a declared datastream
- **WHEN** an operator renders a collector for a supported profile and a
  tenant/datastream pair that exists in a valid platform document
- **THEN** the system writes a collector configuration for that profile into a
  new output directory and reports success

#### Scenario: Rejecting an unknown profile or datastream
- **WHEN** an operator requests a profile that is not shipped, or a
  tenant/datastream pair that the platform document does not declare
- **THEN** the system fails explicitly, names the unknown profile or pair, and
  writes no output

#### Scenario: Deterministic output
- **WHEN** the same profile and platform document are rendered twice
- **THEN** the two outputs are byte-identical

### Requirement: Only enabled signals are collected
A rendered collector configuration SHALL contain delivery pipelines only for
the signals its datastream enables, and SHALL expose OTLP receivers over gRPC
and HTTP for the OTLP-capable signals that are enabled.

#### Scenario: Datastream with a disabled signal
- **WHEN** a collector is rendered for a datastream that does not enable
  profiles
- **THEN** the rendered configuration contains no profile collection or
  profile delivery component

#### Scenario: OTLP reception for enabled signals
- **WHEN** a collector is rendered for a datastream that enables traces
- **THEN** the rendered configuration accepts OTLP traces over both gRPC and
  HTTP and forwards them to the gateway

### Requirement: Node and cluster discovery do not overlap
The Kubernetes node profile SHALL collect only node-local sources for the node
it runs on, and the Kubernetes cluster profile SHALL collect only
cluster-scoped sources, so that deploying both to one cluster does not produce
the same series, log stream, or target twice.

#### Scenario: Deploying both profiles to one cluster
- **WHEN** the node profile and the cluster profile are rendered for the same
  datastream
- **THEN** no discovery role or scrape job is present in both rendered
  configurations

#### Scenario: Node profile is scoped to its own node
- **WHEN** the node profile is rendered
- **THEN** every pod-level or node-level discovery in it is restricted to the
  node the collector runs on

### Requirement: Collector identity comes from its credential
A rendered collector SHALL authenticate to the gateway with its datastream's
ingestion credential, read from a file path rather than embedded in the
configuration, and SHALL NOT set a tenant header itself. The remote-cluster,
VM, and external-service profiles SHALL additionally present a client
certificate.

#### Scenario: No embedded secret or tenant header
- **WHEN** any profile is rendered
- **THEN** the output contains no credential value and no `X-Scope-OrgID`
  header, and references the credential by file path

#### Scenario: Remote profile without a certificate identity
- **WHEN** a remote-cluster, VM, or external-service profile is rendered for a
  datastream whose ingestion credentials declare no certificate identity
- **THEN** the system fails explicitly and writes no output

### Requirement: Bounded delivery
Every delivery pipeline in a rendered collector SHALL have explicit bounds on
what it buffers and on how long it retries, so that a gateway outage results
in bounded memory and disk use and in dropped data that is observable, rather
than unbounded growth.

#### Scenario: Every exporter is bounded
- **WHEN** any profile is rendered
- **THEN** each component that sends telemetry to the gateway declares an
  explicit bound on what it buffers, where the component buffers at all, and
  an explicit maximum retry backoff together with a retry count, age limit,
  or elapsed time

### Requirement: Collection-time redaction
A rendered collector SHALL remove every field named in its datastream's
drop-field list from metric labels, log labels and structured metadata, OTLP
span, log, and metric attributes, OTLP resource attributes, and profile
labels, matching field names case-insensitively, before delivery. In
pipelines whose label names cannot contain `.` or `-`, the collector SHALL
also remove the label whose name is the drop field with those characters
replaced by `_`. The documentation SHALL state which content is not
sanitized, including free-text log bodies beyond the documented key/value
patterns and profile payloads.

#### Scenario: Drop-field coverage in every enabled pipeline
- **WHEN** a collector is rendered for a datastream with drop fields
  `password` and `email` and all four signals enabled
- **THEN** each of the metrics, logs, traces, and profiles pipelines contains
  a removal rule for `password` and for `email`

#### Scenario: Redaction precedes delivery
- **WHEN** any profile is rendered
- **THEN** no path from a receiver or discovery component to a gateway
  exporter bypasses that signal's redaction stage

#### Scenario: Drop field with a separator in a label pipeline
- **WHEN** a collector is rendered for a datastream with drop field
  `user.email`
- **THEN** the metric, log, and profile label rules remove both `user.email`
  and `user_email`, and the attribute rules remove `user.email`

### Requirement: Privileged profiling is separate and opt-in
Privileged or eBPF-based profiling SHALL be delivered as a separate collector
configuration from the minimally privileged profiles, and SHALL be renderable
only for a datastream that sets `allow_privileged_profiling: true` and enables
profiles.

#### Scenario: Privileged profiling not allowed
- **WHEN** an operator renders the privileged profiling configuration for a
  datastream with `allow_privileged_profiling: false`
- **THEN** the system fails explicitly and writes no output

#### Scenario: Base profiles stay unprivileged
- **WHEN** any of the six base profiles is rendered
- **THEN** the output contains no eBPF profiling component

### Requirement: Collector and backend self-monitoring
Every rendered collector SHALL deliver its own operational metrics through the
same pipeline as collected telemetry. The Docker and Kubernetes cluster
profiles SHALL support an explicit option that additionally collects the
signal backends', gateway's, and auth service's operational metrics.

#### Scenario: Collector self-metrics
- **WHEN** any profile is rendered for a datastream that enables metrics
- **THEN** the rendered configuration scrapes the collector's own metrics and
  sends them through the redaction and delivery pipeline

#### Scenario: Backend self-monitoring is explicit
- **WHEN** the Docker profile is rendered without the self-monitoring option
- **THEN** the output contains no scrape of backend, gateway, or auth-service
  metrics

### Requirement: OTLP-only collector
The collector render SHALL support an option that omits a profile's host
sources and produces a collector that only receives pushed telemetry and
scrapes itself, with the same redaction and delivery as any other collector.

#### Scenario: Rendering without host sources
- **WHEN** a collector is rendered with the OTLP-only option
- **THEN** the output contains no host discovery or host scraping component,
  needs no runtime socket or host mount, and still contains the redaction and
  delivery pipelines for every enabled signal

#### Scenario: Pushed profiles are still accepted
- **WHEN** an OTLP-only collector is rendered for a datastream that enables
  profiles
- **THEN** it accepts pushed profiles and forwards them through redaction
