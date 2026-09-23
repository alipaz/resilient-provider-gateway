import httpx
import pytest

from app.main import app


@pytest.mark.asyncio
async def test_live_health_reports_ok() -> None:
    """Fails if the live health endpoint is missing or returns the wrong status."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
