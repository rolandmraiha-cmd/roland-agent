# roland-agent

Roland's own always-on AI agent. You chat with it on its own password-protected web page, it
remembers things, uses tools, and runs scheduled jobs in the background while nobody is chatting.

The agent's brain is any **OpenAI-compatible model API**, so one setting picks it: a free local
model through [Ollama](https://ollama.com) or vLLM, or a paid one like Grok.

## What it can do

- **Web chat page:** works on phone and computer. Replies stream in as the agent types, and old
  chats are kept.
- **Memory:** everything is stored in one SQLite file (`/data/agent.db`): chats, saved facts,
  jobs, job results and usage.
- **Tools:**
  - read public web pages
  - run shell commands (off unless you set `ALLOW_SHELL=true`; see the security notes)
  - read, write and list files in its workspace (`/data/workspace`)
  - save and forget facts
  - schedule, list and cancel jobs (jobs the agent makes wait for your OK)
- **Background jobs:** cron schedules in your time zone, e.g. `0 7 * * *` for every day at 07:00.
  Ask in chat ("every morning at 7, check X") or add one on the **Jobs** tab. A job the agent
  creates from chat stays off until you press **Approve** on the Jobs tab, so a web page can't
  trick it into setting up its own repeating task. The Jobs tab also shows what each run did,
  and lists the facts the agent has saved so you can delete them.
- **Safety:** a daily cap on model calls (`DAILY_CALL_LIMIT`) and a cap on tool steps per message
  (`MAX_TOOL_STEPS`).

## Run it with Docker (recommended)

1. Copy the settings file: `cp .env.example .env`
2. Make your password hash and paste the line it prints into `.env`:
   `docker compose run --rm --no-deps agent python -m agent hash-password`
3. Start everything: `docker compose up -d --build`
4. Download a model into Ollama (once): `docker compose exec ollama ollama pull qwen2.5:7b`
5. Open <http://localhost:8080>. For testing on plain `http://localhost`, set
   `COOKIE_SECURE=false` in `.env`.

To use Grok instead of a local model, set `MODEL_BASE_URL=https://api.x.ai/v1`, `MODEL_NAME` and
`MODEL_API_KEY` in `.env`, and remove the `ollama` service from `docker-compose.yml`.

## Run it without Docker (development)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env              # set MODEL_BASE_URL=http://localhost:11434/v1, DATA_DIR=./data
python -m agent hash-password     # paste the printed line into .env
python -m agent                   # web page on http://localhost:8080
python -m agent chat              # or chat in the terminal
pytest                            # tests (no model needed)
```

Shell commands are off by default everywhere. Never set `ALLOW_SHELL=true` on your own computer.

## Putting it online

The container only listens on `127.0.0.1:8080`. To reach it from your phone anywhere, put a
reverse proxy with HTTPS in front of it, such as [Caddy](https://caddyserver.com) or Nginx with
Let's Encrypt, and keep `COOKIE_SECURE=true`. Never expose port 8080 directly.

Then set these in `.env`:

- `ALLOWED_HOSTS` to your hostname, like `agent.example.com`.
- `FORWARDED_ALLOW_IPS` to the proxy's IP as the agent sees it (for Caddy on the same machine
  talking to the container, usually the Docker gateway, like `172.17.0.1`). Only that address
  may say who the real visitor is, so the per-address lockout counts real visitors and a stranger
  can't pretend to be someone else. If a request carries `X-Forwarded-For` from an address that
  isn't listed, the agent ignores the header and logs a warning once.

## Security notes

- **Login:**
  - Only an argon2 hash of the password is stored.
  - Sessions are random tokens, and only their hash is kept in the database.
  - The cookie is `HttpOnly`, `SameSite=Strict` and `Secure`.
  - A login ends after `SESSION_DAYS`, or after `SESSION_IDLE_HOURS` (default 72) unused.
  - Changing the password hash in `.env` and restarting logs out every device.
- **Wrong passwords:**
  - 5 wrong passwords from one address within 15 minutes lock that address for 15 minutes.
    Other addresses, like yours, can still log in. IPv6 is counted per /64 network, since one
    user usually holds a whole /64.
  - After 20 wrong passwords from everywhere together, each further wrong guess is slowed down,
    but nobody is locked out.
  - Password checks run one at a time, and at most 8 can wait; more get "busy, try again".
    Each address can have only one attempt in progress, and the delay after a wrong guess
    happens outside the queue, so wrong guesses don't hold up your login.
  - Restarting the agent clears all of this.
- **Cross-site requests:** every request that changes something must come from the page's own
  origin and carry the session's CSRF token in an `X-CSRF-Token` header. Strict security headers
  (CSP, no framing) are set, and `ALLOWED_HOSTS` limits which hostnames the page answers to.
- **Shell (off by default):**
  - It's off unless you set `ALLOW_SHELL=true`, and the agent logs a warning at startup when on.
  - When on, commands run as the agent's own user. A web page that tricks the model could then
    try to use a command against the agent itself, such as changing its database. Only turn it
    on if you accept that; v2 will move commands into a separate sandbox container.
  - In Docker it runs as a non-root user with no Docker socket, no host folders, no Linux
    capabilities and a read-only filesystem apart from `/data` and `/tmp`, with CPU, memory and
    process limits.
  - Each command stops after 60 seconds or 8000 characters of output.
  - Commands don't get the API key or password hash in their environment, and the agent
    process blocks other processes from reading its memory (`/proc/<pid>/environ`).
- **Untrusted tool output:** web pages, files and command output reach the model marked as
  untrusted data, and it's told never to follow instructions inside them. That lowers the risk
  but can't remove it, which is why agent-made jobs need your OK.
- **Web fetch:** local and private addresses (localhost, 10.x, 192.168.x, 169.254.x, IPv6 forms
  that wrap them, and so on) are refused and checked again on every redirect. The request then
  connects to the exact address that passed the check, so a DNS trick can't swap in a private one
  afterwards. Fetches never go through a proxy from the environment, and a whole fetch stops
  after 45 seconds.
- **Supply chain:** Docker installs dependencies from `requirements.lock` with checked hashes, and
  the base image is pinned to an exact digest.
- **Upgrades:** an older `agent.db` is updated on start. Jobs from before approvals need your
  OK once, and everyone logs in again.
- **Limits:** at most 3 model calls run at once (others wait), and the daily cap is counted in
  one database step, so parallel chats and jobs can't slip past it. The database uses WAL mode
  and waits for a busy lock instead of failing. Job runs cut off by a restart are marked failed.
- **Known limit:** with the shell on, a command runs as the agent's user and can reach its
  database. Use an API key with a spending limit. v2 moves commands into a separate sandbox.
