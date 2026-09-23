# Implementation status

## Locked architecture

Step 1 uses three direct modules: `app.main` composes the FastAPI application,
`app.config` reads typed environment settings, and `app.api.routes` owns
versioned HTTP routes. The service exposes a liveness endpoint and reserves the
process endpoint without implementing its workflow. Step 2 adds direct provider
contracts, environment-selected provider URLs, request correlation context, and
one lifespan-managed asynchronous HTTP client for single upstream attempts.
Database, Redis, Celery, metrics, and deployment integrations remain out of
scope until their respective steps.

## Steps 1-10

- [x] Step 1 — project bootstrap, typed settings, health endpoint, and route reservation.
- [x] Step 2 — provider contracts, URL selection, request context, and async client.
- [ ] Step 3 — not implemented.
- [ ] Step 4 — not implemented.
- [ ] Step 5 — not implemented.
- [ ] Step 6 — not implemented.
- [ ] Step 7 — not implemented.
- [ ] Step 8 — not implemented.
- [ ] Step 9 — not implemented.
- [ ] Step 10 — not implemented.
