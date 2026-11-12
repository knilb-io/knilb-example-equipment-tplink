RUN ?= pipenv run

.PHONY: check lint type test hardware openapi

check: lint type test

lint:
	$(RUN) ruff check src tests
	$(RUN) ruff format --check src tests

type:
	$(RUN) mypy

test:
	$(RUN) pytest

hardware:
	$(RUN) pytest -m hardware --no-cov -v -rs

openapi:
	$(RUN) equipment-tplink openapi -o equipment-tplink-openapi.yaml
