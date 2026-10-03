FROM python:3.12-slim

# Tools the agent's shell can use inside its own container.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl git jq ca-certificates procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY agent ./agent
RUN pip install --no-cache-dir .

# Runs as a normal user, never root. All its data lives in the /data volume.
RUN useradd --create-home --uid 1000 agent && mkdir -p /data && chown agent:agent /data
USER agent
ENV DATA_DIR=/data AGENT_IN_CONTAINER=1 PYTHONUNBUFFERED=1 HOME=/data/home PYTHONDONTWRITEBYTECODE=1
VOLUME /data
EXPOSE 8080
CMD ["python", "-m", "agent"]
