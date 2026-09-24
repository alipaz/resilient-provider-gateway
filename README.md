# Provider gateway interview deliverable

This FastAPI gateway proxies JSON-object requests to three providers. It includes
load-aware routing, bounded retries, in-process circuits, Redis rate limiting,
backpressure, asynchronous PostgreSQL audit persistence, JSON logs, Prometheus
metrics, deterministic local mocks, Docker Compose, and Locust benchmarking.

## Prerequisites and startup

- Docker Desktop with Docker Compose v2.
- Python 3.12 and a project virtual environment for local tests and Locust.

Start the complete stack against the supplied real external providers:

```sh
docker compose up --build -d
```

When the API is healthy, send a sample JSON object:

```sh
curl -i http://localhost:8000/v1/process \
  -H 'content-type: application/json' \
  -H 'X-Request-ID: demo-request-1' \
  -H 'X-Trace-ID: demo-trace-1' \
  --data '{"value":"hello"}'
```

The API is `http://localhost:8000`; the default request path uses the three
supplied external providers. Mock containers remain Compose-network-only. Stop with
`docker compose down` (use `-v` only when intentionally removing local state).
Use the supplied external endpoints with:

```sh
docker compose --env-file .env.external.example up --build
```

The mock containers remain running but are unused in external mode.

## Configuration

Settings load from the environment (or an uncommitted local `.env`). Explicit
provider URLs always take precedence over mode-derived URLs.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PROVIDER_MODE` | `external` | `external` resolves the supplied endpoints; `mock` still resolves the Docker A/B/C providers. |
| `PROVIDER_A_URL`, `PROVIDER_B_URL`, `PROVIDER_C_URL` | unset | Explicit per-provider override; final precedence layer. |
| `PROVIDER_CONNECT_TIMEOUT_SECONDS` | `2` | Upstream connect timeout. |
| `PROVIDER_READ_TIMEOUT_SECONDS`, `PROVIDER_WRITE_TIMEOUT_SECONDS`, `PROVIDER_POOL_TIMEOUT_SECONDS` | `10`, `10`, `2` | Explicit read/write/pool timeouts. |
| `HTTP_MAX_CONNECTIONS`, `HTTP_MAX_KEEPALIVE_CONNECTIONS` | `200`, `100` | Shared HTTPX connection-pool and keepalive limits. |
| `REQUEST_DEADLINE_SECONDS` | `15` | Shared budget for both permitted attempts. |
| `MAX_INFLIGHT`, `ADMISSION_WAIT_MS` | `200`, `50` | Global concurrency and bounded admission wait. |
| `REDIS_URL` | `redis://localhost:6379/0` | Provider token buckets and readiness. |
| `RABBITMQ_URL` | `amqp://guest:guest@localhost:5672//` | Celery audit broker. |
| `DATABASE_URL` | `postgresql+psycopg://localhost/audit` | PostgreSQL audit database. |
| `PROVIDER_{A,B,C}_RATE_CAPACITY`, `PROVIDER_{A,B,C}_RATE_REFILL_RPS` | A `10`/`10`; B/C `100`/`100` | Redis token-bucket capacity/refill per provider. |
| `PROVIDER_{A,B,C}_MAX_CONCURRENT` | A `10`; B/C `100` | In-process upstream-call limit per provider. |

`.env.example` defaults to external mode. `.env.external.example` remains
available with the supplied URL overrides; deployment-specific explicit
`PROVIDER_*_URL` values still win.

## API contract

`POST /v1/process` accepts a JSON object, forwards it to a selected provider,
and returns the final upstream status and JSON object. Every controlled gateway
response, including errors, includes `X-Request-ID` and `X-Trace-ID`; valid
incoming values are reused and unsafe values receive safe generated IDs.
Forwarded responses also include `X-Upstream-Provider`.

| Status | Error code | Meaning |
| --- | --- | --- |
| `422` | `invalid_correlation_id` | An incoming correlation header is unsafe. A non-object body also receives framework `422` validation. |
| `429` | `admission_rejected` | No global slot within 50 ms; `Retry-After: 1`. |
| `429` | `provider_rate_limited` | Every eligible provider was denied by its token bucket; `Retry-After: 1`. |
| `500` | `internal_error` | An unexpected gateway failure; no exception details are exposed. |
| `502` | `invalid_upstream_response` | Upstream did not return a JSON object. |
| `503` | `rate_limiter_unavailable` | Redis could not make a safe rate decision. |
| `503` | `no_provider_available` | No eligible provider remained. |
| `504` | `upstream_timeout` | An upstream attempt timed out or the overall request deadline elapsed. |

`GET /health/live` proves only process liveness. `GET /health/ready` pings
Redis and reports RabbitMQ/PostgreSQL configuration presence; it intentionally
does not fail for external provider downtime. `GET /metrics` exposes Prometheus
metrics.

## Architecture

```mermaid
flowchart LR
    C[Client] -->|POST /v1/process\ncorrelation headers| API[FastAPI gateway]
    API -->|route, rate limit, retry| P[Providers A / B / C]
    API -->|token bucket, readiness ping| R[(Redis)]
    API -->|audit event| MQ[(RabbitMQ audit queue)]
    MQ --> W[Celery audit worker]
    W -->|idempotent transaction| DB[(PostgreSQL)]
    API -->|/metrics| M[Prometheus scraper]
```

The router selects the least normalized in-flight load (in-flight count times
EWMA latency), using round-robin to break ties. A request makes at most two
attempts, never to the same provider. It retries only timeout, network failure,
HTTP 429, or 5xx; other 4xx responses return directly. Each attempt receives
only the remaining overall deadline.

Each provider has an in-process circuit breaker: a 20-call window, at least
five calls, 50% failure threshold, 15 seconds open, and one half-open probe.
Timeouts, network errors, and 5xx count as failures. A bounded global semaphore
and per-provider semaphores constrain active work. Redis Lua token buckets are
checked before upstream execution; unavailable Redis returns `503` rather than
silently bypassing limits.

## Persistence and observability

The API performs upstream calls itself, then publishes one compact audit event
to the durable bounded RabbitMQ `audit` queue. Celery only persists it to
PostgreSQL and has no result backend. Events contain request and attempt
metadata, never full request/response bodies; the database stores payload size
and SHA-256 only. The unique `(request_id, attempt_number)` constraint plus
upserts makes redelivery idempotent; transient database errors retry up to three
times.

Structlog emits JSON. Request logs include `request_id`, `trace_id`, method,
path, status, and latency; provider logs add provider, attempt number, outcome,
upstream status, and latency. Prometheus labels are bounded to method/status
class, provider/outcome, and scope—never correlation IDs or payloads.

## Tests and benchmarks

```sh
make test
make lint
python -m pytest tests/unit -q
python -m pytest tests/integration -q
make benchmark-local
make benchmark-external
```

The recorded five-second local smoke used two users and completed one request
with 0 failures, 0.50 RPS, 2016 ms average latency, and about 2000 ms P95/P99.
This is only a smoke test, not a representative benchmark. On 2026-09-24 all
three supplied external endpoints timed out at the five-second connect limit,
so no external benchmark was reported. See [docs/benchmark.md](docs/benchmark.md).

## Trade-offs and scaling

- Circuit breaker state is per process in this demo, so replicas do not share
  failure history.
- One Celery message and one database transaction per request are simple but
  expensive at high RPS; production should batch audit writes.
- The provider contract was inferred because the supplied PDF has no body
  schema.
- Retrying `POST` is safe only for these mock providers; a real integration
  needs an idempotency key.
- External provider availability directly affects external benchmark results.
- Provider health is visible through logs and metrics, but external downtime
  deliberately does not fail `/health/ready`; readiness covers this
  application's Redis, RabbitMQ, and PostgreSQL dependencies.

For 1,000–10,000 RPS, use multiple API replicas, shared Redis limits,
independently scale the broker/workers, batch database writes, partition
PostgreSQL audit data, size connection pools, and define retention/archival.
This submitted demo does **not** claim to achieve 10,000 RPS.
