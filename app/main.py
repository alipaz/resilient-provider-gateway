from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.api.routes import router
from app.config import Settings
from app.providers.circuit_breaker import CircuitBreaker
from app.providers.client import ProviderClient
from app.providers.router import ProviderRouter

settings = Settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    timeout = httpx.Timeout(
        connect=settings.PROVIDER_CONNECT_TIMEOUT_SECONDS,
        read=settings.PROVIDER_READ_TIMEOUT_SECONDS,
        write=settings.PROVIDER_WRITE_TIMEOUT_SECONDS,
        pool=settings.PROVIDER_POOL_TIMEOUT_SECONDS,
    )
    async with httpx.AsyncClient(timeout=timeout) as http_client:
        app.state.settings = settings
        app.state.provider_client = ProviderClient(http_client)
        app.state.provider_router = ProviderRouter(settings.provider_configs)
        app.state.circuit_breaker = CircuitBreaker(
            tuple(config.name for config in settings.provider_configs)
        )
        yield


app = FastAPI(lifespan=lifespan)
app.include_router(router)


@app.get("/health/live")
async def live_health() -> dict[str, str]:
    return {"status": "ok"}
