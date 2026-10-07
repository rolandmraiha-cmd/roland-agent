# Test runner for the compose test stack (docker-compose.test.yml). Not used in production.
# It has core's dependencies and the test tools, installed here at build time: the running
# container has a read-only root and sits on internal networks, so it can't install anything.
# The repository itself is mounted read-only at /src.
FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016

WORKDIR /deps
COPY requirements.lock requirements-dev.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock -r requirements-dev.lock

USER 1000:1000
WORKDIR /src
ENV HOME=/tmp/home \
    PYTHONPATH=/src \
    PYTHONNOUSERSITE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-m", "pytest", "-m", "integration", "-q", "-p", "no:cacheprovider", "tests/integration"]
