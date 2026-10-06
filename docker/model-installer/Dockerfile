# Same verified Python base as core; the installer needs only the standard library.
FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016
RUN mkdir -p /models && chown 1000:1000 /models && chmod 0700 /models
COPY deploy/model_store.py deploy/models.lock docker/model/VERSION /opt/model-install/
COPY deploy/model_licenses/ /opt/model-install/model_licenses/
USER 1000:1000
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "/opt/model-install/model_store.py"]
