"""Deterministic telemetry fixtures for all four signals, with sensitive markers under every drop field.

Standard library only. Metrics, logs, and traces are OTLP over HTTP in its JSON encoding;
the profile is a minimal pprof built here and pushed to the collector's ingest endpoint.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from nighthawk.config import ConfigurationError, Stream

SERVICE = "nighthawk-fixture"
METRIC = "nighthawk_fixture_value"
SPAN_NAMES = ("fixture-parent", "fixture-child")
PROFILE_TYPE = "process_cpu:cpu:nanoseconds:cpu:nanoseconds"
# method, url, headers, body -> status
Post = Callable[[str, dict[str, str], bytes], int]


def marker(run_id: str, position: str, field: str) -> str:
    return f"nhmark-{run_id}-{position}-{field}".replace(".", "_")


def trace_id(run_id: str) -> str:
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:32]


def _attributes(values: dict[str, str]) -> list[dict]:
    return [{"key": key, "value": {"stringValue": value}} for key, value in sorted(values.items())]


def _resource(run_id: str, fields: tuple[str, ...]) -> dict:
    values = {"service.name": SERVICE, "nighthawk.run_id": run_id}
    values.update({field: marker(run_id, "resource", field) for field in fields})
    return {"attributes": _attributes(values)}


def metrics_payload(run_id: str, fields: tuple[str, ...], now_ns: int) -> dict:
    values = {"run_id": run_id}
    values.update({field: marker(run_id, "metric", field) for field in fields})
    return {"resourceMetrics": [{"resource": _resource(run_id, fields), "scopeMetrics": [{
        "scope": {"name": SERVICE},
        "metrics": [{"name": METRIC, "unit": "1", "gauge": {"dataPoints": [{
            "asDouble": 42.0, "timeUnixNano": str(now_ns), "attributes": _attributes(values),
        }]}}],
    }]}]}


def log_bodies(run_id: str, fields: tuple[str, ...]) -> list[str]:
    key_values = " ".join(f"{field}={marker(run_id, 'logkv', field)}" for field in fields)
    as_json = json.dumps({field: marker(run_id, "logjson", field) for field in fields}, sort_keys=True)
    return [
        f"fixture {run_id} plain line",
        f"fixture {run_id} keyvalue {key_values}",
        f"fixture {run_id} json {as_json}",
        # No key pattern: the documented limit is that this marker is retained.
        f"fixture {run_id} freetext {free_text_marker(run_id)}",
    ]


def free_text_marker(run_id: str) -> str:
    return marker(run_id, "freetext", "unkeyed")


def logs_payload(run_id: str, fields: tuple[str, ...], now_ns: int) -> dict:
    values = {"run_id": run_id}
    values.update({field: marker(run_id, "logattr", field) for field in fields})
    return {"resourceLogs": [{"resource": _resource(run_id, fields), "scopeLogs": [{
        "scope": {"name": SERVICE},
        "logRecords": [{
            "timeUnixNano": str(now_ns + index), "severityText": "INFO", "body": {"stringValue": body},
            "attributes": _attributes(values), "traceId": trace_id(run_id),
            "spanId": hashlib.sha256(f"{run_id}-0".encode()).hexdigest()[:16],
        } for index, body in enumerate(log_bodies(run_id, fields))],
    }]}]}


def traces_payload(run_id: str, fields: tuple[str, ...], now_ns: int) -> dict:
    values = {"run_id": run_id}
    values.update({field: marker(run_id, "span", field) for field in fields})
    event = {field: marker(run_id, "spanevent", field) for field in fields}
    ids = [hashlib.sha256(f"{run_id}-{index}".encode()).hexdigest()[:16] for index in range(len(SPAN_NAMES))]
    spans = []
    for index, name in enumerate(SPAN_NAMES):
        span = {
            "traceId": trace_id(run_id), "spanId": ids[index], "name": name, "kind": 1,
            "startTimeUnixNano": str(now_ns - 2_000_000 + index * 500_000),
            "endTimeUnixNano": str(now_ns - 1_000_000 + index * 250_000),
            "attributes": _attributes(values),
            "events": [{"timeUnixNano": str(now_ns - 1_500_000), "name": "fixture-event", "attributes": _attributes(event)}],
            "status": {"code": 1},
        }
        if index:
            span["parentSpanId"] = ids[0]
        spans.append(span)
    return {"resourceSpans": [{"resource": _resource(run_id, fields), "scopeSpans": [{
        "scope": {"name": SERVICE}, "spans": spans,
    }]}]}


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _field(number: int, payload: bytes | int) -> bytes:
    if isinstance(payload, int):
        return _varint(number << 3) + _varint(payload)
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def pprof_profile(now_ns: int) -> bytes:
    """A one-sample CPU profile (gzip-compressed pprof): main -> fixture_work, 10ms."""
    strings = ["", "cpu", "nanoseconds", "fixture_work", "main", "fixture.py"]
    value_type = _field(1, 1) + _field(2, 2)
    functions = [_field(1, index + 1) + _field(2, 3 + index) + _field(4, 5) for index in range(2)]
    locations = [_field(1, index + 1) + _field(4, _field(1, index + 1) + _field(2, 10 + index)) for index in range(2)]
    sample = _field(1, _varint(1) + _varint(2)) + _field(2, _varint(10_000_000))
    body = _field(1, value_type) + _field(2, sample)
    body += b"".join(_field(4, location) for location in locations)
    body += b"".join(_field(5, function) for function in functions)
    body += b"".join(_field(6, text.encode("utf-8")) for text in strings)
    body += _field(9, now_ns - 10_000_000_000) + _field(10, 10_000_000_000)
    body += _field(11, value_type) + _field(12, 10_000_000)
    return gzip.compress(body, mtime=0)


def profile_labels(run_id: str, fields: tuple[str, ...]) -> dict[str, str]:
    labels = {"run_id": run_id}
    labels.update({field.replace(".", "_").replace("-", "_"): marker(run_id, "profile", field) for field in fields})
    return labels


def _http_post(url: str, headers: dict[str, str], body: bytes) -> int:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, OSError) as error:
        raise ConfigurationError(f"cannot reach {url}: {getattr(error, 'reason', error)}") from None


def emit(
    stream: Stream, run_id: str, otlp: str, profiles: str, *, now_ns: int | None = None, post: Post = _http_post,
) -> dict:
    """Send the fixture set for the stream's enabled signals; return what was sent for later assertions."""
    if not run_id.isalnum():
        raise ConfigurationError("the run identifier must be alphanumeric")
    now = time.time_ns() if now_ns is None else now_ns
    fields = stream.drop_fields
    sent: dict = {"run_id": run_id, "trace_id": trace_id(run_id), "signals": [], "markers": {}}
    builders = {"metrics": metrics_payload, "logs": logs_payload, "traces": traces_payload}
    for signal, build in builders.items():
        if signal not in stream.signals:
            continue
        body = json.dumps(build(run_id, fields, now), sort_keys=True).encode("utf-8")
        status = post(f"{otlp.rstrip('/')}/v1/{signal}", {"Content-Type": "application/json"}, body)
        if not 200 <= status < 300:
            raise ConfigurationError(f"{signal}: fixture rejected with HTTP {status}")
        sent["signals"].append(signal)
    if "profiles" in stream.signals:
        labels = ",".join(f"{key}={value}" for key, value in sorted(profile_labels(run_id, fields).items()))
        query = urllib.parse.urlencode({
            "name": f"{SERVICE}{{{labels}}}", "from": str(now // 1_000_000_000 - 10), "until": str(now // 1_000_000_000),
            "format": "pprof", "spyName": "nighthawk-fixture",
        })
        status = post(
            f"{profiles.rstrip('/')}/ingest?{query}", {"Content-Type": "application/octet-stream"}, pprof_profile(now),
        )
        if not 200 <= status < 300:
            raise ConfigurationError(f"profiles: fixture rejected with HTTP {status}")
        sent["signals"].append("profiles")
    positions = ("resource", "metric", "logattr", "logkv", "logjson", "span", "spanevent", "profile")
    sent["markers"] = {
        position: {field: marker(run_id, position, field) for field in fields} for position in positions
    }
    sent["free_text_marker"] = free_text_marker(run_id)
    return sent
