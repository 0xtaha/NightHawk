"""Render one Alloy collector: static per-profile sources plus generated redaction and delivery.

Sources in `alloy-configs/<profile>/` forward only to the fixed `*.redact` receivers.
Everything that depends on the contract, including every gateway exporter, is generated
here, so telemetry cannot reach the gateway without passing redaction.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from nighthawk.config import ROOT, ConfigurationError, Credential, Platform, Stream

ALLOY_CONFIGS = ROOT / "alloy-configs"
OTLP_SIGNALS = ("metrics", "logs", "traces")
# Entry receivers the static source files are allowed to forward to.
REDACT_RECEIVERS = {
    "metrics": "prometheus.relabel.redact.receiver",
    "logs": "loki.relabel.redact.receiver",
    "profiles": "pyroscope.relabel.redact.receiver",
}
CREDENTIAL_FILE = 'sys.env("NIGHTHAWK_CREDENTIAL_FILE")'
CA_FILE = 'sys.env("NIGHTHAWK_GATEWAY_CA_FILE")'
CLIENT_CERT_FILE = 'sys.env("NIGHTHAWK_CLIENT_CERT_FILE")'
CLIENT_KEY_FILE = 'sys.env("NIGHTHAWK_CLIENT_KEY_FILE")'


@dataclass(frozen=True)
class Profile:
    requires_certificate: bool = False
    self_monitoring: bool = False
    privileged: bool = False
    # Accepts profiles pushed by application SDKs.
    sdk_profiles: bool = True


PROFILES = {
    "docker": Profile(self_monitoring=True),
    "k8s-node": Profile(sdk_profiles=False),
    "k8s-cluster": Profile(self_monitoring=True),
    "remote-cluster": Profile(requires_certificate=True),
    "vm": Profile(requires_certificate=True),
    "external-service": Profile(requires_certificate=True),
    "profiling-ebpf": Profile(privileged=True, sdk_profiles=False),
}


def _quote(value: str) -> str:
    return json.dumps(value)


def _alternation(fields: tuple[str, ...]) -> str:
    # Field names are limited to [A-Za-z0-9_.-] by the platform schema; only "." needs neutralizing.
    return "|".join(field.replace(".", "[.]") for field in fields)


def _label_alternation(fields: tuple[str, ...]) -> str:
    """Drop fields as label names. A label cannot hold "." or "-", so `user.email` arrives as `user_email`."""
    names: list[str] = []
    for field in fields:
        for name in (field, re.sub(r"[.-]", "_", field)):
            if name not in names:
                names.append(name)
    return _alternation(tuple(names))


def _case_variants(fields: tuple[str, ...]) -> list[str]:
    return sorted({variant for field in fields for variant in (field, field.lower(), field.upper(), field.title())})


def _select_credential(platform: Platform, stream: Stream, profile: Profile, credential_id: str | None) -> Credential:
    candidates = [
        item for item in platform.credentials
        if (item.tenant, item.datastream, item.permission) == (stream.tenant, stream.datastream, "ingest")
    ]
    if credential_id is not None:
        candidates = [item for item in candidates if item.id == credential_id]
        if not candidates:
            raise ConfigurationError(
                f"{credential_id!r} is not an ingestion credential of {stream.tenant}/{stream.datastream}"
            )
    if profile.requires_certificate:
        candidates = [item for item in candidates if item.certificate_identity is not None]
        if not candidates:
            raise ConfigurationError(
                f"{stream.tenant}/{stream.datastream}: this profile needs an ingestion credential "
                "that declares a certificate identity"
            )
    if len(candidates) != 1:
        raise ConfigurationError(
            f"{stream.tenant}/{stream.datastream}: several ingestion credentials qualify "
            f"({', '.join(item.id for item in candidates)}); choose one explicitly"
        )
    return candidates[0]


def _tls(block: str, indent: str, with_certificate: bool) -> str:
    lines = [f"{indent}{block} {{", f"{indent}  ca_file   = {CA_FILE}"]
    if with_certificate:
        lines += [f"{indent}  cert_file = {CLIENT_CERT_FILE}", f"{indent}  key_file  = {CLIENT_KEY_FILE}"]
    return "\n".join(lines + [f"{indent}}}"])


def _basic_auth(credential: Credential, indent: str) -> str:
    return (
        f"{indent}basic_auth {{\n"
        f"{indent}  username      = {_quote(credential.id)}\n"
        f"{indent}  password_file = {CREDENTIAL_FILE}\n"
        f"{indent}}}"
    )


def _metrics(base: str, credential: Credential, label_pattern: str, with_certificate: bool, monitored: list[tuple[str, str]]) -> str:
    platform_scrape = ""
    if monitored:
        targets = "".join(
            f'    {{"__address__" = {address}, "job" = {_quote(job)}}},\n' for job, address in monitored
        )
        platform_scrape = f"""
// Platform self-monitoring: signal backends, the gateway proxy, and the auth service.
prometheus.scrape "platform" {{
  targets = [
{targets}  ]
  forward_to = [prometheus.relabel.redact.receiver]
}}
"""
    return f"""
// ---- metrics ----
prometheus.exporter.self "collector" {{ }}

prometheus.scrape "collector" {{
  targets    = prometheus.exporter.self.collector.targets
  job_name   = "alloy"
  forward_to = [prometheus.relabel.redact.receiver]
}}
{platform_scrape}
prometheus.relabel "redact" {{
  forward_to = [prometheus.remote_write.gateway.receiver]

  rule {{
    action = "labeldrop"
    regex  = "(?i)({label_pattern})"
  }}
}}

prometheus.remote_write "gateway" {{
  endpoint {{
    url = {_quote(base + "/metrics/api/v1/push")}

{_basic_auth(credential, "    ")}

{_tls("tls_config", "    ", with_certificate)}

    // Bounded: at most capacity * max_shards samples in memory; older samples are dropped.
    queue_config {{
      capacity             = 10000
      max_shards           = 10
      max_samples_per_send = 2000
      min_backoff          = "30ms"
      max_backoff          = "5s"
      sample_age_limit     = "30m"
    }}
  }}

  // Bounded on disk: the write-ahead log keeps at most this much history during an outage.
  wal {{
    truncate_frequency = "30m"
    max_keepalive_time = "2h"
  }}
}}
"""


def _logs(
    base: str, credential: Credential, fields: tuple[str, ...], pattern: str, label_pattern: str, with_certificate: bool,
) -> str:
    variants = ", ".join(_quote(item) for item in _case_variants(fields))
    return f"""
// ---- logs ----
loki.relabel "redact" {{
  forward_to = [loki.process.redact.receiver]

  rule {{
    action = "labeldrop"
    regex  = "(?i)({label_pattern})"
  }}
}}

loki.process "redact" {{
  forward_to = [loki.write.gateway.receiver]

  // Exact names only: this stage has no pattern form. The shipped sources create no
  // structured metadata; OTLP log attributes are redacted by pattern below.
  stage.structured_metadata_drop {{
    values = [{variants}]
  }}

  // Best effort on message bodies: key=value, key: value, and JSON "key": value.
  stage.replace {{
    expression = `(?i)\\b(?:{pattern})\\s*[=:]\\s*("[^"]*"|[^\\s,;&]+)`
    replace    = "[REDACTED]"
  }}

  stage.replace {{
    expression = `(?i)"(?:{pattern})"\\s*:\\s*("(?:[^"\\\\]|\\\\.)*"|[^,}}\\s]+)`
    replace    = "[REDACTED]"
  }}
}}

loki.write "gateway" {{
  endpoint {{
    url = {_quote(base + "/logs/loki/api/v1/push")}

{_basic_auth(credential, "    ")}

{_tls("tls_config", "    ", with_certificate)}

    // Bounded: one batch is buffered and sources are held back while it retries;
    // the batch is dropped after max_backoff_retries.
    batch_size          = "1MiB"
    batch_wait          = "1s"
    min_backoff_period  = "500ms"
    max_backoff_period  = "1m"
    max_backoff_retries = 10
  }}
}}
"""


def _otlp(base: str, credential: Credential, signals: list[str], pattern: str, with_certificate: bool) -> str:
    key_pattern = f"(?i)^({pattern})$"

    def outputs(target: str) -> str:
        return "\n".join(f"    {signal:<7} = [{target}]" for signal in signals)

    def statements(block: str, contexts: tuple[str, ...], extra: str = "") -> str:
        rendered = ""
        for context in contexts:
            rendered += f"""
  {block} {{
    context    = "{context}"
    statements = [
      `delete_matching_keys({context}.attributes, "{key_pattern}")`,{extra if context == "log" else ""}
    ]
  }}
"""
        return rendered

    body = (
        "\n      // Best effort on string bodies; the whole key/value match is replaced."
        f'\n      `replace_pattern(log.body, "(?i)\\\\b({pattern})\\\\s*[=:]\\\\s*(\\"[^\\"]*\\"|[^\\\\s,;&]+)", "[REDACTED]") where IsString(log.body)`,'
        f'\n      `replace_pattern(log.body, "(?i)\\"({pattern})\\"\\\\s*:\\\\s*(\\"[^\\"]*\\"|[^,}}\\\\s]+)", "[REDACTED]") where IsString(log.body)`,'
    )
    transform = ""
    if "traces" in signals:
        transform += statements("trace_statements", ("resource", "scope", "span", "spanevent"))
    if "metrics" in signals:
        transform += statements("metric_statements", ("resource", "scope", "datapoint"))
    if "logs" in signals:
        transform += statements("log_statements", ("resource", "scope", "log"), body)
    return f"""
// ---- OTLP ({", ".join(signals)}) ----
otelcol.receiver.otlp "default" {{
  grpc {{
    endpoint = "0.0.0.0:4317"
  }}

  http {{
    endpoint = "0.0.0.0:4318"
  }}

  output {{
{outputs("otelcol.processor.memory_limiter.default.input")}
  }}
}}

otelcol.processor.memory_limiter "default" {{
  check_interval = "1s"
  limit          = "256MiB"
  spike_limit    = "64MiB"

  output {{
{outputs("otelcol.processor.transform.redact.input")}
  }}
}}

otelcol.processor.transform "redact" {{
  // A statement that fails drops the payload instead of forwarding it unredacted.
  error_mode = "propagate"
{transform}
  output {{
{outputs("otelcol.exporter.otlphttp.gateway.input")}
  }}
}}

// Alloy 1.20.1 fails to build this component with a client_auth block in any form
// ("no credential source provided"), although `alloy validate` accepts it. The top-level
// arguments work. The secret comes from local.file, which also picks up a rotated file;
// the file must hold the secret without a trailing newline.
local.file "gateway_credential" {{
  filename  = {CREDENTIAL_FILE}
  is_secret = true
}}

otelcol.auth.basic "gateway" {{
  username = {_quote(credential.id)}
  password = local.file.gateway_credential.content
}}

otelcol.exporter.otlphttp "gateway" {{
  client {{
    endpoint = {_quote(base)}
    auth     = otelcol.auth.basic.gateway.handler

{_tls("tls", "    ", with_certificate)}
  }}

  // Bounded: at most queue_size batches wait; a batch is dropped after max_elapsed_time.
  sending_queue {{
    enabled       = true
    queue_size    = 1000
    num_consumers = 4
  }}

  retry_on_failure {{
    enabled          = true
    initial_interval = "5s"
    max_interval     = "30s"
    max_elapsed_time = "5m"
  }}
}}
"""


def _profiles(base: str, credential: Credential, label_pattern: str, with_certificate: bool, sdk: bool) -> str:
    receiver = """
// Applications push profiles here with a Pyroscope SDK.
pyroscope.receive_http "sdk" {
  http {
    listen_address = "0.0.0.0"
    listen_port    = 4040
  }
  forward_to = [pyroscope.relabel.redact.receiver]
}
""" if sdk else ""
    return f"""
// ---- profiles ----{receiver}
pyroscope.relabel "redact" {{
  forward_to = [pyroscope.write.gateway.receiver]

  rule {{
    action = "labeldrop"
    regex  = "(?i)({label_pattern})"
  }}
}}

pyroscope.write "gateway" {{
  endpoint {{
    url = {_quote(base + "/profiles")}

{_basic_auth(credential, "    ")}

{_tls("tls_config", "    ", with_certificate)}

    // Bounded: this component has no queue. A profile is retried within these limits, then dropped.
    remote_timeout      = "10s"
    min_backoff_period  = "500ms"
    max_backoff_period  = "1m"
    max_backoff_retries = 10
  }}
}}
"""


def _monitored_targets(platform: Platform) -> list[tuple[str, str]]:
    upstreams = platform.gateway.upstreams
    targets = [
        (job, _quote(urlsplit(upstreams[key]).netloc))
        for job, key in (("mimir", "metrics"), ("loki", "logs"), ("tempo", "traces.query"), ("pyroscope", "profiles"))
        if key in upstreams
    ]
    targets.append(("nighthawk-authz", _quote(urlsplit(platform.gateway.auth_service).netloc)))
    # The proxy's own metrics address is deployment-owned and not part of the platform document.
    targets.append(("traefik", 'sys.env("NIGHTHAWK_GATEWAY_METRICS_ADDRESS")'))
    return targets


def render_collector(
    platform: Platform, tenant: str, datastream: str, profile_name: str, *,
    entry_point: str | None = None, credential_id: str | None = None, self_monitoring: bool = False,
    otlp_only: bool = False, configs: Path = ALLOY_CONFIGS,
) -> dict[str, str]:
    """Return the collector's files by name. Nothing is written here."""
    profile = PROFILES.get(profile_name)
    if profile is None:
        raise ConfigurationError(f"unknown collector profile {profile_name!r}; choose one of {', '.join(PROFILES)}")
    stream = next(
        (item for item in platform.streams if (item.tenant, item.datastream) == (tenant, datastream)), None,
    )
    if stream is None:
        raise ConfigurationError(f"unknown tenant/datastream {tenant}/{datastream}")
    signals = [name for name in ("metrics", "logs", "traces", "profiles") if name in stream.signals]
    if profile.privileged:
        if not stream.allow_privileged_profiling or "profiles" not in stream.signals:
            raise ConfigurationError(
                f"{tenant}/{datastream}: privileged profiling needs allow_privileged_profiling: true and the profiles signal"
            )
        signals = ["profiles"]
    if otlp_only and not profile.sdk_profiles:
        raise ConfigurationError(f"the {profile_name} profile has only host sources; it cannot be rendered OTLP-only")
    if self_monitoring and (not profile.self_monitoring or "metrics" not in signals):
        raise ConfigurationError(
            "self-monitoring is available only for the docker and k8s-cluster profiles on a datastream that enables metrics"
        )
    gateway = platform.gateway
    if entry_point is None:
        rules = [rule for entry in gateway.entry_points for rule in entry.rules]
        if len(rules) != 1:
            raise ConfigurationError(f"several gateway entry points are selected ({', '.join(rules)}); choose one explicitly")
        entry_point = rules[0]
    base = gateway.url(entry_point)
    credential = _select_credential(platform, stream, profile, credential_id)
    with_certificate = credential.certificate_identity is not None
    pattern = _alternation(stream.drop_fields)
    label_pattern = _label_alternation(stream.drop_fields)

    files: dict[str, str] = {}
    # OTLP-only: no host discovery or scraping, so no runtime socket, host mount, or privilege.
    for signal in [] if otlp_only else signals:
        source = configs / profile_name / f"{signal}.alloy"
        if source.exists():
            files[source.name] = source.read_text(encoding="utf-8")
    generated = (
        "// Generated by `nighthawk render-collector`; do not edit. Re-render after a contract change.\n"
        f"// Profile {profile_name}, datastream {tenant}/{datastream}, credential {credential.id}.\n"
        "// The gateway derives the tenant from the credential; no tenant header is set here.\n"
    )
    if "metrics" in signals:
        generated += _metrics(
            base, credential, label_pattern, with_certificate, _monitored_targets(platform) if self_monitoring else [],
        )
    if "logs" in signals:
        generated += _logs(base, credential, stream.drop_fields, pattern, label_pattern, with_certificate)
    otlp = [signal for signal in OTLP_SIGNALS if signal in signals]
    if otlp:
        generated += _otlp(base, credential, otlp, pattern, with_certificate)
    if "profiles" in signals:
        generated += _profiles(base, credential, label_pattern, with_certificate, profile.sdk_profiles)
    # `alloy fmt` indents with tabs; emit the same so the generated file is format-clean.
    files["datastream.alloy"] = "".join(
        "\t" * ((len(line) - len(line.lstrip(" "))) // 2) + line.lstrip(" ")
        for line in generated.splitlines(keepends=True)
    )
    return files


def write_collector(files: dict[str, str], output: Path) -> None:
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    for name, content in sorted(files.items()):
        with (output / name).open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
