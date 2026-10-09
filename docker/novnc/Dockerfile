# noVNC image (§6.6): websockify hands noVNC's script files to the screen page and carries
# the screen connection between Caddy and x11vnc. It holds no secret and mounts nothing.
# It is a relay Roland's keys pass through on their way to the browser, so it logs
# connections only (never --traffic, --record or --verbose) and can reach nothing but x11vnc.
#
# Same pinned base as the core image.
FROM public.ecr.aws/docker/library/python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016

WORKDIR /app
# Hash-checked, like the other images. websockify alone: see requirements-novnc.in.
COPY requirements-novnc.lock ./
RUN pip install --no-cache-dir --require-hashes --no-deps -r requirements-novnc.lock

# noVNC 1.7.0, fetched at build time and pinned by the SHA-256 of GitHub's tarball for the
# tag. The build stops if a single byte differs. When updating, change the tag and the hash
# together (sha256sum of the downloaded .tar.gz) and run the screen tests in docs/NEXT.md.
COPY docker/novnc/fetch.py /tmp/fetch.py
RUN python /tmp/fetch.py \
        https://github.com/novnc/noVNC/archive/refs/tags/v1.7.0.tar.gz \
        b1003a11b6e6e8d8f7f5e5586daae7f8ca651d8aee0aa155ff9ac841c48f52c6 \
        /opt/novnc \
    && rm /tmp/fetch.py

USER 1000:1000
ENV HOME=/tmp \
    PYTHONNOUSERSITE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
# --file-only: no folder listings. --heartbeat: a ping every 30 seconds, so a phone network
# doesn't drop the screen while Roland waits for a code and nothing on it moves.
# No --token-plugin, --cert, --web-auth: who may connect is decided before this container,
# by Caddy asking core (forward_auth).
CMD ["websockify", "--web", "/opt/novnc", "--file-only", "--heartbeat=30", "10.77.2.30:6080", "10.77.5.40:5900"]
