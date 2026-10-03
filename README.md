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
  - run shell commands (inside its own container only)
  - read, write and list files in its workspace (`/data/workspace`)
  - save and forget facts
  - schedule, list and cancel jobs
- **Background jobs:** cron schedules in your time zone, e.g. `0 7 * * *` for every day at 07:00.
  Ask in chat ("every morning at 7, check X") or add one on the **Jobs** tab. The Jobs tab also
  shows what each run did.
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

Shell commands are turned off outside Docker, so the agent can't touch your own computer. Set
`ALLOW_SHELL=true` only on a throwaway machine.

## Putting it online

The container only listens on `127.0.0.1:8080`. To reach it from your phone anywhere, put a
reverse proxy with HTTPS in front of it, such as [Caddy](https://caddyserver.com) or Nginx with
Let's Encrypt, and keep `COOKIE_SECURE=true`. Never expose port 8080 directly.

## Security notes

- **Login:**
  - Only an argon2 hash of the password is stored.
  - Sessions are random tokens, and only their hash is kept in the database.
  - The cookie is `HttpOnly`, `SameSite=Strict` and `Secure`.
- **Lockout:** 5 wrong passwords from one address, or 20 from all addresses together, within
  15 minutes lock logins for 15 minutes.
- **Cross-site requests:** every request that changes something must come from the page's own
  origin and carry the session's CSRF token in an `X-CSRF-Token` header. Strict security headers
  (CSP, no framing) are set, and `ALLOWED_HOSTS` limits which hostnames the page answers to.
- **Shell:**
  - It runs as a non-root user in the agent's container, which has no Docker socket, no host
    folders, no Linux capabilities and a read-only filesystem apart from `/data` and `/tmp`.
  - It's a normal shell, not a list of allowed commands, because a fixed list would block most
    real tasks. The container itself is the boundary.
  - It has CPU, memory and process limits, and each command is stopped after 60 seconds.
  - Commands don't get the API key or password hash in their environment.
- **Web fetch:** local and private addresses (localhost, 10.x, 192.168.x, 169.254.x and so on)
  are refused, and checked again on every redirect.
- **Known limit:** the agent's shell runs as the same user as the agent itself, so a determined
  command could still read the agent's own settings, such as through `/proc`. Use an API key with
  a spending limit. A later version can move tools into a separate sandbox container.
