# Pinned to an exact image so a rebuild can't silently pull something different.
# python:3.12-slim as of 4 Oct 2026; bump the digest on purpose when updating.
FROM public.ecr.aws/docker/library/python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016

# The core does not run user commands; those go to the isolated sandbox service.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Dependencies come from requirements.lock with checked hashes, so a tampered package fails
# the build. Remake the lock with:
#   uv pip compile pyproject.toml requirements-build.in --generate-hashes --python-version 3.12 --python-platform linux -o requirements.lock
# The lock includes setuptools (the build tool), so building the app below downloads nothing.
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY pyproject.toml README.md ./
COPY agent ./agent
COPY training ./training
COPY docker/model/VERSION ./docker/model/VERSION
# Repo pyproject also lists sandboxd for local/dev installs; core image ships agent only.
RUN sed -i 's/, "sandboxd"//' pyproject.toml     && pip install --no-cache-dir --no-deps --no-build-isolation --no-index .

# Runs as a normal user, never root. Its data lives in the /data volume. HOME points at the
# throwaway /tmp, and Python ignores per-user packages, so the shell can't plant code there
# that the agent would load on its next start.
RUN useradd --create-home --uid 1000 agent \
    && mkdir -p /data /backups /workspace \
    && chown -R agent:agent /data /backups /workspace \
    && chmod 0700 /data /backups /workspace
RUN mkdir -p /training-data && chown agent:agent /training-data && chmod 0700 /training-data
USER agent
ENV DATA_DIR=/data HOME=/tmp/home PYTHONNOUSERSITE=1 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
VOLUME /data
EXPOSE 8080
CMD ["python", "-m", "agent"]
