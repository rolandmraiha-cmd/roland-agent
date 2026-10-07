#!/usr/bin/env bash
# Disposable GitHub CI only. It cannot be used as a production deployment script.
set -euo pipefail

if [[ ${GITHUB_ACTIONS:-} != true ]]; then
    echo "The live edge smoke test is restricted to disposable GitHub CI." >&2
    exit 2
fi
EDGE_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd -- "$EDGE_ROOT"
if [[ -e .env || -L .env || -e secrets/agent_password_hash || -L secrets/agent_password_hash || -e secrets/model_server_token || -L secrets/model_server_token || -e secrets/sandbox_api_token || -L secrets/sandbox_api_token || -e .ci-workspace ]]; then
    echo "Refusing to overwrite existing config, secrets or CI workspace." >&2
    exit 2
fi
umask 077
EDGE_PROJECT="roland-agent-edge-ci-${GITHUB_RUN_ID:?}-${GITHUB_RUN_ATTEMPT:?}"
edge_compose=(docker compose --project-name "$EDGE_PROJECT" -f "$EDGE_ROOT/docker-compose.yml")
edge_cleanup() {
    edge_status=$?
    if [[ $edge_status != 0 ]]; then
        "${edge_compose[@]}" logs --no-color --tail 80 || true
    fi
    "${edge_compose[@]}" down --volumes --remove-orphans || true
    exit "$edge_status"
}
trap edge_cleanup EXIT
PYTHONPATH="$EDGE_ROOT" python tests/integration/edge_fixture.py
sudo chown 1000:1000 secrets secrets/agent_password_hash secrets/model_server_token secrets/sandbox_api_token .ci-workspace/workspace
sudo chmod 0700 secrets .ci-workspace/workspace
sudo python3 deploy/preflight_edge.py
"${edge_compose[@]}" config -q
"${edge_compose[@]}" build --pull
MODEL_PROJECT="$EDGE_PROJECT" MODEL=test-tiny MODEL_INSTALL_TEST_ONLY=true bash deploy/model.sh fetch
for edge_tls in acme internal; do
    "${edge_compose[@]}" run --rm --no-deps -e AGENT_HOST=agent.example.test -e "CADDY_TLS=$edge_tls" caddy \
        caddy adapt --config /etc/caddy/Caddyfile --adapter caddyfile --validate >/dev/null
done
"${edge_compose[@]}" up -d --wait --wait-timeout 180
python tests/integration/edge_probe.py "$EDGE_PROJECT"
python tests/integration/model_probe.py "$EDGE_PROJECT"
