import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from redis import asyncio as redis_asyncio

from app.api.routes import router
from app.config import Settings
from app.providers.circuit_breaker import CircuitBreaker
from app.providers.client import ProviderClient
from app.providers.router import ProviderRouter
from app.resilience.admission import AdmissionController
from app.resilience.rate_limiter import ProviderRateLimiter

settings = Settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    timeout = httpx.Timeout(
        connect=settings.PROVIDER_CONNECT_TIMEOUT_SECONDS,
        read=settings.PROVIDER_READ_TIMEOUT_SECONDS,
        write=settings.PROVIDER_WRITE_TIMEOUT_SECONDS,
        pool=settings.PROVIDER_POOL_TIMEOUT_SECONDS,
    )
    redis_client = redis_asyncio.from_url(settings.REDIS_URL)
    async with httpx.AsyncClient(timeout=timeout) as http_client:
        app.state.settings = settings
        app.state.admission = AdmissionController(settings.MAX_INFLIGHT, settings.ADMISSION_WAIT_MS)
        app.state.provider_client = ProviderClient(http_client)
        app.state.provider_router = ProviderRouter(settings.provider_configs)
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
app.include_router(router)


@app.get("/health/live")
async def live_health() -> dict[str, str]:
    return {"status": "ok"}
