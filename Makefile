.PHONY: install test lint run benchmark-local benchmark-external

PYTHON ?= .venv/bin/python

install:
	$(PYTHON) -m pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check .

run:
	$(PYTHON) -m uvicorn app.main:app --reload

benchmark-local:
	docker compose up --build -d --wait --wait-timeout 90
	$(PYTHON) -m locust -f benchmark/locustfile.py --host http://localhost:8000 --headless -u 10 -r 2 -t 30s --csv benchmark/results/local-10

benchmark-external:
	docker compose --env-file .env.external.example up --build -d --wait --wait-timeout 90
	$(PYTHON) -m locust -f benchmark/locustfile.py --host http://localhost:8000 --headless -u 10 -r 2 -t 30s --csv benchmark/results/external-10
