FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates openssh-client rsync \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY agent ./agent
COPY trainerd ./trainerd
COPY training ./training
COPY docker/model/VERSION ./docker/model/VERSION
RUN useradd --uid 1000 trainer && mkdir /models /training-runs /training-data \
    && chown trainer:trainer /models /training-runs /training-data
USER 1000:1000
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp/home
CMD ["python", "-m", "trainerd"]
