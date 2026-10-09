# Verified official Caddy 2.11.7 Alpine index (docker-library/repo-info, 6 Oct 2026).
FROM caddy:2.11.7-alpine@sha256:d8542f48d34a9cf4e4c11a478865229840e87e4c96ea3f439101f31a5d35f75f

# Low ports use the namespaced Compose sysctl, not file or process capabilities.
RUN setcap -r /usr/bin/caddy \
    && mkdir -p /data /config \
    && chown -R 1000:1000 /data /config
COPY docker/caddy/Caddyfile /etc/caddy/Caddyfile
USER 1000:1000
