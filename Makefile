.PHONY: lint fmt-check test test-integration build compose-config \
	preflight-edge preflight \
	up down ps logs deploy secrets hash-password firewall firewall-install \
	workspace-fs backup restore restore-test verify \
	migrate-v1-workspace ship \
	model-fetch model-install

# Pass file paths and catalogue ids as environment values, never shell source.
export MODEL FILE ID MODEL_PROJECT MODEL_INSTALL_TEST_ONLY
export HOST REF APPLY FORCE PULL S
# Do not export WORKSPACE_HOST_DIR / WORKSPACE_SIZE_GB: an empty export
# overrides Compose .env and breaks edge CI bind mounts (missing /srv/...).
# Pass them explicitly when set: WORKSPACE_HOST_DIR=/path make …

lint:
	ruff check agent tests deploy

fmt-check:
	ruff format --check agent tests deploy

test:
	pytest -q
	node --test tests/frontend/chat.test.cjs

test-integration:
	bash tests/integration/edge.sh
	@echo "A4.3/A4.4: bring up sandbox test stack when Docker is available:"
	@echo "  mkdir -p .ci-workspace && make secrets"
	@echo "  docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --build sandbox"
	@echo "  docker compose -f docker-compose.yml -f docker-compose.test.yml run --rm tester"
	@echo "  bash tests/integration/isolation.sh --ci"
	@echo "  docker compose -f docker-compose.yml -f docker-compose.test.yml down -v"
	@echo "On Contabo after deploy: make verify (runs isolation.sh --server)."

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
