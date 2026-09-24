import asyncio
from contextlib import asynccontextmanager
from json import dumps
from time import perf_counter

import httpx
import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis import asyncio as redis_asyncio

from app.api.routes import router
from app.config import Settings
from app.core.context import set_request_context
from app.observability.logging import configure_logging
from app.observability.metrics import request_counter, request_duration
from app.providers.circuit_breaker import CircuitBreaker
from app.providers.client import ProviderClient
from app.providers.router import ProviderRouter
from app.resilience.admission import AdmissionController
from app.resilience.rate_limiter import ProviderRateLimiter

settings = Settings()
configure_logging()
logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    timeout = httpx.Timeout(
        connect=settings.PROVIDER_CONNECT_TIMEOUT_SECONDS,
        read=settings.PROVIDER_READ_TIMEOUT_SECONDS,
        write=settings.PROVIDER_WRITE_TIMEOUT_SECONDS,
        pool=settings.PROVIDER_POOL_TIMEOUT_SECONDS,
    )
    limits = httpx.Limits(
        max_connections=settings.HTTP_MAX_CONNECTIONS,
        max_keepalive_connections=settings.HTTP_MAX_KEEPALIVE_CONNECTIONS,
    )
    redis_client = redis_asyncio.from_url(settings.REDIS_URL)
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as http_client:
        app.state.settings = settings
        app.state.admission = AdmissionController(settings.MAX_INFLIGHT, settings.ADMISSION_WAIT_MS)
        app.state.provider_client = ProviderClient(http_client)
        app.state.provider_router = ProviderRouter(
            settings.provider_configs,
            max_concurrency={
                config.name: settings.provider_max_concurrent(config.name)
                for config in settings.provider_configs
            },
        )
        app.state.circuit_breaker = CircuitBreaker(
            tuple(config.name for config in settings.provider_configs)
        )
        app.state.rate_limiter = ProviderRateLimiter(redis_client)
        app.state.provider_semaphores = {
            config.name: asyncio.BoundedSemaphore(settings.provider_max_concurrent(config.name))
            for config in settings.provider_configs
        }
        try:
            yield
        finally:
            await redis_client.aclose()


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def observe_request(request: Request, call_next):
    correlation_error = False
    try:
        request_id, trace_id = set_request_context(
            request.headers.get("X-Request-ID"),
            request.headers.get("X-Trace-ID"),
        )
    except ValueError:
        request_id, trace_id = set_request_context(None, None)
        correlation_error = True
    request.state.request_id = request_id
    request.state.trace_id = trace_id
    structlog.contextvars.bind_contextvars(request_id=request_id, trace_id=trace_id)
    started_at = perf_counter()
    status_code = 500
    try:
        if correlation_error:
            response = JSONResponse(
                status_code=422,
                content={"error": {"code": "invalid_correlation_id"}},
            )
        else:
            response = await call_next(request)
    except Exception:
        status_code = 500
        response = JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error"}},
        )
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Trace-ID"] = trace_id
        return response
    else:
        status_code = response.status_code
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Trace-ID"] = trace_id
        return response
    finally:
        elapsed = perf_counter() - started_at
        request_counter.labels(request.method, f"{status_code // 100}xx").inc()
        request_duration.observe(elapsed)
        logger.info(
            "request_completed",
            request_id=request_id,
            trace_id=trace_id,
            method=request.method,
            path=request.url.path,
            status_code=status_code,
            total_latency_ms=elapsed * 1000,
        )
        structlog.contextvars.clear_contextvars()


app.include_router(router)


@app.get("/health/live")
async def live_health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
async def ready_health(request: Request) -> Response:
    redis_ok = await request.app.state.rate_limiter.is_available()
    body = {
        "redis": "ok" if redis_ok else "unavailable",
        "rabbitmq_configured": bool(request.app.state.settings.RABBITMQ_URL),
        "postgres_configured": bool(request.app.state.settings.DATABASE_URL),
    }
    return Response(
        content=dumps(body),
        media_type="application/json",
        status_code=200 if redis_ok else 503,
    )


@app.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
