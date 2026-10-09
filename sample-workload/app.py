"""NightHawk sample workload: a small loop that behaves like an instrumented application.

It pushes traces, metrics, and logs over OTLP and CPU profiles with the Pyroscope SDK,
all to the local collector. Logs carry the active trace ID and profiles carry span IDs,
so Grafana's trace-to-logs and trace-to-profiles links have something to follow.
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import signal
import time

import pyroscope
from opentelemetry import _logs, metrics, trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from pyroscope.otel import PyroscopeSpanProcessor

SERVICE = os.environ.get("OTEL_SERVICE_NAME", "nighthawk-sample")
running = True


def configure() -> None:
    resource = Resource.create({"service.name": SERVICE})
    tracing = TracerProvider(resource=resource)
    tracing.add_span_processor(PyroscopeSpanProcessor())
    tracing.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracing)
    metrics.set_meter_provider(MeterProvider(
        resource=resource, metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=15000)],
    ))
    logging_provider = LoggerProvider(resource=resource)
    logging_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    _logs.set_logger_provider(logging_provider)
    logging.basicConfig(level=logging.INFO, handlers=[LoggingHandler(logger_provider=logging_provider), logging.StreamHandler()])
    pyroscope.configure(
        application_name=SERVICE,
        server_address=os.environ["PYROSCOPE_SERVER_ADDRESS"],
        tags={"service_name": SERVICE},
    )


def checkout(items: int) -> str:
    """Burn a little CPU so the profile has a recognizable frame."""
    digest = b"nighthawk"
    for _ in range(items * 20000):
        digest = hashlib.sha256(digest).digest()
    return digest.hex()[:12]


def stop(*_: object) -> None:
    global running
    running = False


def main() -> None:
    configure()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    tracer = trace.get_tracer(SERVICE)
    meter = metrics.get_meter(SERVICE)
    orders = meter.create_counter("sample_orders_total", description="Orders processed by the sample workload")
    duration = meter.create_histogram("sample_order_duration_seconds", unit="s")
    log = logging.getLogger(SERVICE)
    while running:
        started = time.monotonic()
        items = random.randint(1, 5)
        with tracer.start_as_current_span("process-order") as span:
            span.set_attribute("order.items", items)
            with tracer.start_as_current_span("checkout"):
                receipt = checkout(items)
            failed = random.random() < 0.05
            if failed:
                span.set_attribute("error", True)
                log.error("order failed items=%d receipt=%s", items, receipt)
            else:
                log.info("order processed items=%d receipt=%s", items, receipt)
        orders.add(1, {"outcome": "failed" if failed else "ok"})
        duration.record(time.monotonic() - started, {"outcome": "failed" if failed else "ok"})
        time.sleep(1)


if __name__ == "__main__":
    main()
