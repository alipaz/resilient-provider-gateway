# Implementation status

## Locked architecture

Step 1 uses three direct modules: `app.main` composes the FastAPI application,
`app.config` reads typed environment settings, and `app.api.routes` owns
versioned HTTP routes. The service exposes a liveness endpoint and reserves the
process endpoint without implementing its workflow. Step 2 adds direct provider
contracts, environment-selected provider URLs, request correlation context, and
one lifespan-managed asynchronous HTTP client for single upstream attempts.
Step 3 adds in-memory selection across exactly three providers using in-flight
load, EWMA latency, and round-robin tie-breaking; the process endpoint proxies
one selected upstream request. Database, Redis, Celery, metrics, and deployment
integrations remain out of scope until their respective steps. Step 4 adds a
bounded two-attempt retry and a per-process circuit breaker; per-process breaker
state is a deliberate time-boxed design and is not shared through Redis. Step 5
adds global admission backpressure, per-provider active-call bounds, and Redis
token-bucket rate decisions before upstream execution.
Step 6 adds compact asynchronous audit persistence through a bounded RabbitMQ
queue, with Celery restricted to PostgreSQL persistence and no upstream calls.
The initial audit migration was verified against isolated PostgreSQL 16 with a
downgrade and re-upgrade round trip.
Step 7 adds per-process JSON logging, bounded-label Prometheus metrics, and
lightweight liveness/readiness checks. Redis readiness performs only a
connectivity ping; RabbitMQ and PostgreSQL readiness report configured presence
without issuing per-request service queries.
Step 8 adds one environment-configured local mock-provider application for the
default mock runtime. Its A/B/C profiles provide seeded latency, rate-limit,
5xx, and timeout behavior; integration tests route only to these local mocks.
Step 9 packages the API, migration, worker, stateful dependencies, and local
mock providers with Docker Compose, alongside reproducible Locust benchmarks.
Step 10 finalizes the operator documentation and final audit: the README and
architecture document describe the implemented contracts, boundaries, benchmark
evidence, deployment commands, and demo-scale trade-offs.

## Steps 1-10

- [x] Step 1 — project bootstrap, typed settings, health endpoint, and route reservation.
- [x] Step 2 — provider contracts, URL selection, request context, and async client.
- [x] Step 3 — in-memory provider routing and process endpoint proxying.
- [x] Step 4 — bounded retry, request deadline, and per-process circuit breaker.
- [x] Step 5 — admission backpressure and Redis-backed provider rate limiting.
- [x] Step 6 — asynchronous idempotent audit persistence.
- [x] Step 7 — JSON logs, Prometheus metrics, and readiness checks.
- [x] Step 8 — local mock providers and deterministic gateway integration coverage.
- [x] Step 9 — Docker Compose stack and Locust benchmark harness.
- [x] Step 10 — final documentation and verification audit.
