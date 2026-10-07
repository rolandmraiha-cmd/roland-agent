# Sandbox image: runs sandboxd as uid 1000 and executes user commands (§6.3).
# Same base digest as the core image.
FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016

# Common CLI work, PDFs, archives. No build-essential/nodejs until Q13 is answered.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash coreutils findutils grep sed gawk curl wget git jq ca-certificates \
        procps unzip zip xz-utils file less poppler-utils sqlite3 tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Hash-checked core lock (fastapi + uvicorn and their deps).
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY pyproject.toml README.md ./
COPY sandboxd ./sandboxd
# Install only the sandboxd package (no agent code in this image).
RUN printf '%s\n' \
      '[build-system]' \
      'requires = ["setuptools>=68"]' \
      'build-backend = "setuptools.build_meta"' \
      '' \
      '[project]' \
      'name = "roland-sandboxd"' \
      'version = "0.1.0"' \
      'requires-python = ">=3.11"' \
      'dependencies = []' \
      '' \
      '[tool.setuptools]' \
      'packages = ["sandboxd"]' \
      > pyproject.toml \
    && pip install --no-cache-dir --no-deps --no-build-isolation --no-index .

RUN useradd --create-home --uid 1000 runner \
    && mkdir -p /workspace \
    && chown -R runner:runner /workspace
USER 1000:1000
WORKDIR /workspace
ENV HOME=/workspace/.sandbox-home \
    PYTHONNOUSERSITE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SANDBOXD_HOST=10.77.3.20 \
    SANDBOXD_PORT=7000
ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "sandboxd"]
