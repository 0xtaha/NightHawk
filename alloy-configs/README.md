# Alloy collector sources

Pinned Alloy version: `1.20.1` (checked against `config/versions.yaml` by
`python -m nighthawk check-pins`).

Each directory is a collector profile. The files hold only discovery and
source components, split by signal, and forward only to the fixed redaction
receivers (`prometheus.relabel.redact`, `loki.relabel.redact`,
`pyroscope.relabel.redact`). They are not runnable on their own.

`python -m nighthawk render-collector` copies the files for a datastream's
enabled signals and generates `datastream.alloy` next to them with the OTLP
receiver, redaction, and gateway delivery. Run the resulting directory with
`alloy run <directory>`. See [collection](../docs/04-collection.md).
