# Operator image: Python 3.13 slim, dependencies installed from uv.lock, runs as non-root.
# Build: docker build -t secret-sync:dev .

FROM ghcr.io/astral-sh/uv:0.13.8 AS uv

FROM python:3.13.18-slim-trixie AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv UV_PYTHON_DOWNLOADS=never
WORKDIR /src
# Dependencies first (cached layer), then the project itself.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.13.18-slim-trixie
LABEL org.opencontainers.image.source="https://github.com/ngems1/k8s-secret-sync-operator" \
      org.opencontainers.image.description="Annotated ConfigMaps -> Secrets from AWS Secrets Manager / Azure Key Vault" \
      org.opencontainers.image.licenses="MIT"
COPY --from=build /app/.venv /app/.venv
# USER: kopf derives its identity from getpass.getuser(), which fails for a numeric UID with
# no /etc/passwd entry ("No username set in the environment"): found by the e2e test.
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 USER=secret-sync
# Numeric UID so Kubernetes can verify runAsNonRoot without reading /etc/passwd.
USER 65532:65532
EXPOSE 8080 9100
ENTRYPOINT ["kopf", "run", "--standalone", "--all-namespaces", "--liveness=http://0.0.0.0:8080/healthz", "-m", "secret_sync.operator"]
