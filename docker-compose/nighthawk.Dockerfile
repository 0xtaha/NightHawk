# NightHawk tooling image: auth service, readiness waits, Grafana provisioning, fixture emitter.
# Build context is the repository root; see .dockerignore for what it may contain.
FROM docker.io/library/python:3.12-slim-bookworm@sha256:2ed6491b93cd49272ee6de2b5a38440c3448360322c089fc23e370722d74179d

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /opt/nighthawk
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY config/ config/
COPY nighthawk/ nighthawk/

# Compose overrides this with the operator's UID so mounted secrets are readable.
USER 65532:65532
ENTRYPOINT ["python", "-m", "nighthawk"]
