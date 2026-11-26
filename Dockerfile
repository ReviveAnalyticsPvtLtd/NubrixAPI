FROM python:3.12-slim AS base

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    supervisor \
    libgomp1 \
    coreutils \
    build-essential \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv==0.10.7

# Sandbox runs can't write bytecode, so without this they compile every package they import.
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
ENV PATH="/app/.venv/bin:$PATH"

# Sandbox runs live here. Setting this also makes a separate uid per run mandatory.
ENV SANDBOX_RUN_ROOT=/var/sandbox-runs
RUN mkdir -m 0711 /var/sandbox-runs

# A run's uid can read anything world-readable, and the logs hold other users' details.
RUN mkdir -p /app/logs && chmod 0700 /app/logs

# Nothing a run can execute may raise its privileges.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} +

EXPOSE 7860

CMD ["/app/startup.sh"]

FROM base AS test

# Cutover tests use Git to enumerate the source tree.
RUN apt-get update && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/* \
 && find / -xdev -perm /6000 -type f -exec chmod a-s {} +
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --group dev --no-install-project
COPY . .
RUN sed -i 's/\r$//' /app/startup.sh && chmod +x /app/startup.sh && git init --quiet

FROM base AS runtime

COPY . .
RUN sed -i 's/\r$//' /app/startup.sh && chmod +x /app/startup.sh
