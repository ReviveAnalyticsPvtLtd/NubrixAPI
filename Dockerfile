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

# Sandbox runs live here. Setting this also makes a separate uid per run mandatory.
ENV SANDBOX_RUN_ROOT=/var/sandbox-runs
RUN mkdir -m 0711 /var/sandbox-runs

EXPOSE 7860

CMD ["/app/startup.sh"]