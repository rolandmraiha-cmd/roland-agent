"""Exercise the pinned server with a tiny CI fixture; no real chats or tools."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main(project: str):
    if os.getenv("GITHUB_ACTIONS") != "true" or not project.startswith("roland-agent-edge-ci-"):
        raise SystemExit("The model probe is restricted to its disposable CI project")
    compose = ["docker", "compose", "--project-name", project, "-f", str(ROOT / "docker-compose.yml")]
    checks = """
import json, pathlib, httpx
token = pathlib.Path('/run/secrets/model_server_token').read_text().strip()
schema = {
    'type': 'object', 'properties': {
        'action': {'type': 'string', 'const': 'reply'},
        'text': {'type': 'string', 'const': 'ok'},
    }, 'required': ['action', 'text'], 'additionalProperties': False,
}
payload = {
    'messages': [{'role': 'user', 'content': 'Reply with a JSON action.'}],
    'stream': False, 'temperature': 0, 'max_tokens': 128,
    'response_format': {'type': 'json_schema', 'json_schema': {'name': 'action', 'schema': schema}},
}
with httpx.Client(base_url='http://10.77.6.60:8080', trust_env=False, timeout=60) as client:
    assert client.post('/v1/chat/completions', json=payload).status_code == 401
    headers = {'Authorization': 'Bearer ' + token}
    response = client.post('/v1/chat/completions', json=payload, headers=headers)
    assert response.status_code == 200, 'Authenticated inference failed'
    answer = json.loads(response.json()['choices'][0]['message']['content'])
    assert answer == {'action': 'reply', 'text': 'ok'}
    slots = client.get('/slots', headers=headers)
    assert slots.status_code == 501
    assert slots.json()['error']['type'] == 'not_supported_error'
    assert slots.json()['error']['message'] == 'This server does not support slots endpoint. Start it with `--slots`'
print('Authenticated JSON-schema inference passed; a missing token was refused.')
"""
    subprocess.run(compose + ["exec", "-T", "core", "python", "-c", checks], check=True, timeout=90)
    runtime_check = """test "$(id -u)" = 1000 &&
grep -q '^CapEff:[[:space:]]*0000000000000000$' /proc/self/status &&
grep -q '^NoNewPrivs:[[:space:]]*1$' /proc/self/status"""
    subprocess.run(compose + ["exec", "-T", "model", "bash", "-c", runtime_check], check=True, timeout=15)
    egress = subprocess.run(
        compose + ["exec", "-T", "model", "bash", "-c", "timeout 5 bash -c 'echo > /dev/tcp/1.1.1.1/443'"],
        capture_output=True,
        timeout=15,
    )
    assert egress.returncode != 0, "Model unexpectedly reached the public internet"
    write = subprocess.run(
        compose + ["exec", "-T", "model", "bash", "-c", "touch /models/runtime-write-must-fail"],
        capture_output=True,
        timeout=15,
    )
    assert write.returncode != 0, "Model weights volume is writable at runtime"
    subprocess.run(compose + ["restart", "model"], check=True, timeout=40)
    subprocess.run(compose + ["up", "-d", "--wait", "--wait-timeout", "180"], check=True, timeout=210)
    print("Model egress refusal, read-only weights and restart health passed.")


if __name__ == "__main__":
    main(sys.argv[1])
