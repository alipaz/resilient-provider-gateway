from fastapi import FastAPI

from app.api.routes import router
from app.config import Settings

settings = Settings()
app = FastAPI()
app.include_router(router)


@app.get("/health/live")
async def live_health() -> dict[str, str]:
    return {"status": "ok"}
