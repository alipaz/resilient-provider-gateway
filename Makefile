.PHONY: install test lint run benchmark-external

PYTHON ?= .venv/bin/python

install:
	$(PYTHON) -m pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check .

run:
	$(PYTHON) -m uvicorn app.main:app --reload

TARGET_RPS ?= 50
BENCHMARK_USERS ?= 1000
BENCHMARK_DURATION ?= 60s
benchmark-external:
	docker compose up --build -d --wait --wait-timeout 90
	TARGET_RPS=$(TARGET_RPS) BENCHMARK_USERS=$(BENCHMARK_USERS) $(PYTHON) -m locust -f benchmark/locustfile_200rps.py --host http://localhost:8000 --headless -u $(BENCHMARK_USERS) -r 100 -t $(BENCHMARK_DURATION) --csv benchmark/results/external-$(TARGET_RPS)rps
