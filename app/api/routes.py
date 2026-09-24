import asyncio
from datetime import datetime, timezone
from hashlib import sha256
from json import dumps
from time import perf_counter
from typing import Any
from uuid import uuid4

import structlog
from fastapi import APIRouter, Body, Request, status
from fastapi.responses import JSONResponse

from app.observability.metrics import (
    circuit_state,
    provider_attempt_counter,
    provider_duration,
    provider_inflight,
    rate_limit_rejections,
)
from app.persistence.dispatch import dispatch_audit_event
from app.providers.circuit_breaker import CircuitState
from app.providers.models import (
    ProviderAttempt,
    ProviderOutcome,
    ProviderRequestResult,
    ProviderResult,
)
from app.resilience.rate_limiter import RateLimiterUnavailable

router = APIRouter(prefix="/v1")
logger = structlog.get_logger()


@router.post("/process")
async def process(request: Request, payload: dict[str, Any] = Body(...)) -> JSONResponse:
    received_at = datetime.now(timezone.utc)
    started_at = perf_counter()
    audit_id = str(uuid4())
    request_id = request.state.request_id
    trace_id = request.state.trace_id

    if not await request.app.state.admission.acquire():
        rate_limit_rejections.labels("admission", "global").inc()
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"error": {"code": "admission_rejected"}},
            headers={"Retry-After": "1", "X-Request-ID": request_id, "X-Trace-ID": trace_id},
        )
    response: JSONResponse | None = None
    try:
        try:
            async with asyncio.timeout(request.app.state.settings.REQUEST_DEADLINE_SECONDS):
                response = await _proxy_request(request, payload, request_id, trace_id)
        except TimeoutError:
            request_result = getattr(request.state, "provider_request_result", None)
            headers = {"X-Request-ID": request_id, "X-Trace-ID": trace_id}
            if request_result is not None and request_result.result is not None:
                headers["X-Upstream-Provider"] = request_result.result.provider_name
            response = JSONResponse(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                content={"error": {"code": "upstream_timeout"}},
                headers=headers,
            )
        return response
    finally:
        request.app.state.admission.release()
        payload_bytes = dumps(payload, separators=(",", ":")).encode()
        request_result = getattr(request.state, "provider_request_result", None)
        attempts = (
            []
            if request_result is None
            else [
                {
                    "request_id": audit_id,
                    "provider": item.provider_name,
                    "attempt_number": number,
                    "started_at": received_at,
                    "latency_ms": item.latency_ms,
                    "http_status": item.status_code,
                    "outcome": item.outcome.value,
                    "error_class": item.error_class,
                }
                for number, item in enumerate(request_result.attempts, 1)
            ]
        )
        dispatch_audit_event(
            {
                "request": {
                    "id": audit_id,
                    "trace_id": trace_id,
                    "received_at": received_at,
                    "completed_at": datetime.now(timezone.utc),
                    "final_status": 500 if response is None else response.status_code,
                    "selected_provider": None
                    if response is None
                    else response.headers.get("x-upstream-provider"),
                    "total_latency_ms": (perf_counter() - started_at) * 1000,
                    "payload_size": len(payload_bytes),
                    "payload_sha256": sha256(payload_bytes).hexdigest(),
                    "error_class": None,
                },
                "attempts": attempts,
            }
        )


async def _proxy_request(
    request: Request,
    payload: dict[str, Any],
    request_id: str,
    trace_id: str,
) -> JSONResponse:
    attempted_providers: set[str] = set()
    attempts = []
    result: ProviderResult | None = None
    provider_name: str | None = None
    rate_limited = False
    _update_circuit_metrics(request)
    while len(attempts) < 2:
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
            rate_limit_rejections.labels("provider", provider.name).inc()
            continue
        provider_name = provider.name
        request.app.state.provider_router.start_attempt(provider)
        provider_inflight.labels(provider.name).inc()
        attempt_started_at = perf_counter()
        try:
            try:
                async with request.app.state.provider_semaphores[provider.name]:
                    result = await request.app.state.provider_client.process(
                        provider,
                        payload,
                    )
            except asyncio.CancelledError:
                elapsed_ms = (perf_counter() - attempt_started_at) * 1000
                result = ProviderResult(
                    attempt=ProviderAttempt(
                        provider_name=provider.name,
                        status_code=None,
                        latency_ms=elapsed_ms,
                        outcome=ProviderOutcome.TIMEOUT,
                        error_class="deadline_exceeded",
                    ),
                    response_json=None,
                )
                attempts.append(result.attempt)
                request.state.provider_request_result = ProviderRequestResult(
                    result=result,
                    attempts=tuple(attempts),
                )
                provider_attempt_counter.labels(provider.name, result.outcome.value).inc()
                provider_duration.labels(provider.name).observe(elapsed_ms / 1000)
                logger.info(
                    "provider_attempt_completed",
                    provider=provider.name,
                    attempt_number=len(attempts),
                    outcome=result.outcome.value,
                    upstream_status=result.status_code,
                    latency_ms=elapsed_ms,
                )
                request.app.state.circuit_breaker.record(provider.name, failed=True)
                _update_circuit_metrics(request)
                raise
        finally:
            elapsed_ms = (perf_counter() - attempt_started_at) * 1000
            request.app.state.provider_router.finish_attempt(provider, elapsed_ms)
            provider_inflight.labels(provider.name).dec()
        attempts.append(result.attempt)
        provider_attempt_counter.labels(provider.name, result.outcome.value).inc()
        provider_duration.labels(provider.name).observe(elapsed_ms / 1000)
        logger.info(
            "provider_attempt_completed",
            provider=provider.name,
            attempt_number=len(attempts),
            outcome=result.outcome.value,
            upstream_status=result.status_code,
            latency_ms=elapsed_ms,
        )
        request.app.state.circuit_breaker.record(
            provider.name,
            failed=_is_circuit_failure(result),
        )
        _update_circuit_metrics(request)
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


def _update_circuit_metrics(request: Request) -> None:
    values = {
        CircuitState.CLOSED: 0,
        CircuitState.HALF_OPEN: 0.5,
        CircuitState.OPEN: 1,
    }
    for provider_name in request.app.state.provider_router.provider_names:
        state = request.app.state.circuit_breaker.state(provider_name)
        circuit_state.labels(provider_name).set(values[state])
