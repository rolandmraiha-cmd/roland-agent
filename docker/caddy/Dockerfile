# Verified multi-platform digest for the official Caddy 2.11.7 Alpine image.
FROM caddy:2.11.7-alpine@sha256:d76116d819d5162f464b0f2cd09bd28c568a86148c7bc539ce17c33eb22d8bbb

# Low ports use the namespaced Compose sysctl, not file or process capabilities.
RUN setcap -r /usr/bin/caddy \
    && mkdir -p /data /config \
    && chown -R 1000:1000 /data /config
COPY docker/caddy/Caddyfile /etc/caddy/Caddyfile
USER 1000:1000
