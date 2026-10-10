# syntax=docker/dockerfile:1.7
# InvoiceIQ — single container: Streamlit (public :8501) + FastAPI (private 127.0.0.1:8000).
# No credentials are baked in; configuration comes from environment variables at runtime.

ARG PYTHON_IMAGE=python:3.12-slim-trixie
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.23

FROM ${UV_IMAGE} AS uv

# ---- Build stage: resolve and install dependencies into /opt/venv ----------------------------
FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /build

# Dependencies first (cached unless the lockfile changes), then the project itself.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable \
    # Third-party packages ship their own test suites (~50 MB); they are never imported.
    && find /opt/venv/lib/python3.12/site-packages -depth -type d -name tests \
       -exec rm -r {} +

# ---- Runtime stage ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

# tini is PID 1: it forwards signals to the supervisor and reaps orphaned processes.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --create-home --home-dir /home/app app

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/app/data \
    APP_ENV=production \
    AI_PROVIDER=mock \
    USAGE_STORE=memory \
    INVOICEIQ_API_URL=http://127.0.0.1:8000

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY data ./data
COPY .streamlit ./.streamlit

USER 10001:10001
EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD ["python", "-m", "invoiceiq.runtime.healthcheck"]

STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "invoiceiq.runtime.supervisor"]
