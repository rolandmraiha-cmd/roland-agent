.PHONY: lint fmt-check test test-integration build compose-config preflight-edge

lint:
	ruff check agent tests deploy

fmt-check:
	ruff format --check agent tests deploy

test:
	pytest -q
	node --test tests/frontend/chat.test.cjs

test-integration:
	bash tests/integration/edge.sh

build:
	docker compose build

compose-config:
	docker compose config -q

preflight-edge:
	python3 deploy/preflight_edge.py
