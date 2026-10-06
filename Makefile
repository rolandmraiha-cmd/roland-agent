.PHONY: lint fmt-check test test-integration build compose-config preflight-edge model-fetch model-install

# Pass file paths and catalogue ids as environment values, never shell source.
export MODEL FILE ID MODEL_PROJECT MODEL_INSTALL_TEST_ONLY

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

model-fetch:
	bash deploy/model.sh fetch

model-install:
	bash deploy/model.sh install
