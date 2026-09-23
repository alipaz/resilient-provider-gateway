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

## Steps 1-10

- [x] Step 1 — project bootstrap, typed settings, health endpoint, and route reservation.
- [x] Step 2 — provider contracts, URL selection, request context, and async client.
- [x] Step 3 — in-memory provider routing and process endpoint proxying.
- [x] Step 4 — bounded retry, request deadline, and per-process circuit breaker.
- [x] Step 5 — admission backpressure and Redis-backed provider rate limiting.
- [x] Step 6 — asynchronous idempotent audit persistence.
- [ ] Step 7 — not implemented.
- [ ] Step 8 — not implemented.
- [ ] Step 9 — not implemented.
- [ ] Step 10 — not implemented.
