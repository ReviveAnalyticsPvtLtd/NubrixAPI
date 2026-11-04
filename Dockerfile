FROM python:3.12-slim

WORKDIR /app

COPY . .

RUN apt-get update && apt-get install -y \
    supervisor \
    libgomp1 \
    coreutils \
    build-essential \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

RUN pip install uv

RUN chmod +x /app/startup.sh

# Sandbox runs can't write bytecode, so without this they compile every package they import.
ENV UV_COMPILE_BYTECODE=1

# Sandbox runs live here. Setting this also makes a separate uid per run mandatory.
ENV SANDBOX_RUN_ROOT=/var/sandbox-runs
RUN mkdir -m 0711 /var/sandbox-runs

# A run's uid can read anything world-readable, and the logs hold other users' details.
RUN mkdir -p /app/logs && chmod 0700 /app/logs

# Nothing a run can execute may raise its privileges.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} +

EXPOSE 7860

CMD ["/app/startup.sh"]