from time import perf_counter
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.core.context import set_request_context
from app.providers.models import ProviderOutcome, ProviderRequestResult, ProviderResult
from app.resilience.rate_limiter import RateLimiterUnavailable

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

    if not await request.app.state.admission.acquire():
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"error": {"code": "admission_rejected"}},
            headers={"Retry-After": "1", "X-Request-ID": request_id, "X-Trace-ID": trace_id},
        )
    try:
        return await _proxy_request(request, payload, request_id, trace_id)
    finally:
        request.app.state.admission.release()


async def _proxy_request(
    request: Request,
    payload: dict[str, Any],
    request_id: str,
    trace_id: str,
) -> JSONResponse:
    request_started_at = perf_counter()
    attempted_providers: set[str] = set()
    attempts = []
    result: ProviderResult | None = None
    provider_name: str | None = None
    rate_limited = False
    while len(attempts) < 2:
        remaining_seconds = request.app.state.settings.REQUEST_DEADLINE_SECONDS - (
            perf_counter() - request_started_at
        )
        if remaining_seconds <= 0:
            break
        eligible = {
            name
            for name in request.app.state.provider_router.provider_names
            if name not in attempted_providers
            and request.app.state.circuit_breaker.is_eligible(name)
        }
        if not eligible:
            break
        provider = request.app.state.provider_router.select(eligible=eligible)
        if not request.app.state.circuit_breaker.allow(provider.name):
            attempted_providers.add(provider.name)
            continue
        attempted_providers.add(provider.name)
        capacity, refill_rate = request.app.state.settings.provider_rate_limit(provider.name)
        try:
            rate_decision = await request.app.state.rate_limiter.check(
                provider.name,
                capacity=capacity,
                refill_rate=refill_rate,
            )
        except RateLimiterUnavailable:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"error": {"code": "rate_limiter_unavailable"}},
                headers={"X-Request-ID": request_id, "X-Trace-ID": trace_id},
            )
        if not rate_decision.allowed:
            rate_limited = True
            continue
        provider_name = provider.name
        request.app.state.provider_router.start_attempt(provider)
        attempt_started_at = perf_counter()
        try:
            async with request.app.state.provider_semaphores[provider.name]:
                result = await request.app.state.provider_client.process(
                    provider,
                    payload,
                    timeout=remaining_seconds,
                )
        finally:
            elapsed_ms = (perf_counter() - attempt_started_at) * 1000
            request.app.state.provider_router.finish_attempt(provider, elapsed_ms)
        attempts.append(result.attempt)
        request.app.state.circuit_breaker.record(
            provider.name,
            failed=_is_circuit_failure(result),
        )
        if not _is_retryable(result):
            break

    request_result = ProviderRequestResult(result=result, attempts=tuple(attempts))
    request.state.provider_request_result = request_result
    if result is None:
        if rate_limited:
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"error": {"code": "provider_rate_limited"}},
                headers={
                    "Retry-After": "1",
                    "X-Request-ID": request_id,
                    "X-Trace-ID": trace_id,
                },
            )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"error": {"code": "no_provider_available"}},
            headers={"X-Request-ID": request_id, "X-Trace-ID": trace_id},
        )

    response_headers = {
        "X-Request-ID": request_id,
        "X-Trace-ID": trace_id,
        "X-Upstream-Provider": provider_name or "",
    }
    if attempts and all(attempt.outcome is ProviderOutcome.TIMEOUT for attempt in attempts):
        return JSONResponse(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            content={"error": {"code": "upstream_timeout"}},
            headers=response_headers,
        )
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


def _is_retryable(result: ProviderResult) -> bool:
    return (
        result.outcome in {ProviderOutcome.TIMEOUT, ProviderOutcome.CONNECTION_ERROR}
        or result.error_class == "request_error"
        or result.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        or result.outcome is ProviderOutcome.SERVER_ERROR
    )


def _is_circuit_failure(result: ProviderResult) -> bool:
    return (
        result.outcome
        in {
            ProviderOutcome.TIMEOUT,
            ProviderOutcome.CONNECTION_ERROR,
            ProviderOutcome.SERVER_ERROR,
        }
        or result.error_class == "request_error"
    )
