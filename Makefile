.PHONY: lint fmt-check test test-integration test-sandbox test-browser build compose-config \
	preflight-edge preflight \
	up down ps logs deploy secrets hash-password firewall firewall-install \
	workspace-fs backup restore restore-test verify memory-report \
	migrate-v1-workspace ship \
	model-fetch model-install

# Pass file paths and catalogue ids as environment values, never shell source.
export MODEL FILE ID MODEL_PROJECT MODEL_INSTALL_TEST_ONLY
export HOST REF APPLY FORCE PULL S
# Do not export WORKSPACE_HOST_DIR / WORKSPACE_SIZE_GB: an empty export
# overrides Compose .env and breaks edge CI bind mounts (missing /srv/...).
# Pass them explicitly when set: WORKSPACE_HOST_DIR=/path make …

lint:
	ruff check agent browserd tests deploy

fmt-check:
	ruff format --check agent tests deploy

test:
	pytest -q
	node --test tests/frontend/chat.test.cjs tests/frontend/snapshot.test.cjs

test-integration:
	bash tests/integration/edge.sh
	@echo "Optional sandbox live stack (Docker + secrets): make test-sandbox"
	@echo "On Contabo after deploy: make verify (runs isolation.sh --server)."

# Live sandbox stack (not run by CI edge job). Needs Docker, secrets/, and workspace bind.
test-sandbox:
	mkdir -p .ci-workspace
	test -f secrets/sandbox_api_token || { echo "missing secrets/sandbox_api_token; run make secrets" >&2; exit 2; }
	docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --build sandbox
	docker compose -f docker-compose.yml -f docker-compose.test.yml run --rm tester
	ISOLATION_COMPOSE_FILES="docker-compose.yml docker-compose.test.yml" bash tests/integration/isolation.sh --ci
	docker compose -f docker-compose.yml -f docker-compose.test.yml down -v

# Live browser stack (A6.3; not run by CI). Needs Docker, a .env and secrets/ (make secrets).
# Builds the browser image, runs it against the fixture site, restarts it, and removes the
# test stack again. CHROMIUM_SANDBOX=1 runs the same with Chromium's own sandbox on.
test-browser:
	bash tests/integration/browser.sh

build:
	docker compose build

compose-config:
	docker compose config -q

up:
	docker compose up -d --remove-orphans

down:
	docker compose down --remove-orphans

ps:
	docker compose ps

logs:
	docker compose logs --tail=200 -f $(if $(S),$(S),)

preflight-edge:
	python3 deploy/preflight_edge.py

preflight:
	bash deploy/preflight.sh

memory-report:
	bash deploy/memory-report.sh

secrets:
	bash deploy/secrets.sh

hash-password:
	bash deploy/hash-password.sh

firewall:
	bash deploy/firewall.sh

firewall-install:
	bash deploy/firewall.sh --install

workspace-fs:
	bash deploy/workspace-fs.sh $(WORKSPACE_SIZE_GB)

deploy:
	bash deploy/deploy.sh

backup:
	docker compose exec -T core python -m agent backup-now

restore:
	bash deploy/restore.sh

restore-test:
	bash deploy/restore.sh --test

verify:
	bash deploy/verify.sh

migrate-v1-workspace:
	bash deploy/migrate-v1-workspace.sh

ship:
	bash deploy/ship.sh

model-fetch:
	bash deploy/model.sh fetch

model-install:
	bash deploy/model.sh install
