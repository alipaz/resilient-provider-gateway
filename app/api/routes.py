from time import perf_counter
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.core.context import set_request_context

router = APIRouter(prefix="/v1")


@router.post("/process")
async def process(request: Request, payload: dict[str, Any] = Body(...)) -> JSONResponse:
    try:
        request_id, trace_id = set_request_context(
            request.headers.get("X-Request-ID"),
            request.headers.get("X-Trace-ID"),
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from error

    provider = request.app.state.provider_router.select()
    request.app.state.provider_router.start_attempt(provider)
    started_at = perf_counter()
    try:
        result = await request.app.state.provider_client.process(provider, payload)
    finally:
        elapsed_ms = (perf_counter() - started_at) * 1000
        request.app.state.provider_router.finish_attempt(provider, elapsed_ms)

    response_headers = {
        "X-Request-ID": request_id,
        "X-Trace-ID": trace_id,
        "X-Upstream-Provider": provider.name,
    }
    if result.response_json is None or result.status_code is None:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "error": {
                    "code": "invalid_upstream_response",
                    "message": "The upstream provider did not return a JSON object.",
                }
            },
            headers=response_headers,
        )
    return JSONResponse(
        status_code=result.status_code,
        content=result.response_json,
        headers=response_headers,
    )
