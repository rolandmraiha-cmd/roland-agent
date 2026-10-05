.PHONY: lint fmt-check test test-integration

lint:
	ruff check agent tests

fmt-check:
	ruff format --check agent tests

test:
	pytest -q
	node --test tests/frontend/chat.test.cjs

test-integration:
	@echo "Integration tests will be added in a later milestone."
