from contextvars import ContextVar
from uuid import uuid4

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)


def _validate_identifier(name: str, value: str | None) -> str:
    if value is None:
        return uuid4().hex
    is_valid = value and len(value) <= 128 and all(
        character.isalnum() or character in "-_." for character in value
    )
    if not is_valid:
        raise ValueError(
            f"{name} must contain only letters, numbers, hyphens, underscores, or periods"
        )
    return value


def set_request_context(request_id: str | None, trace_id: str | None) -> tuple[str, str]:
    validated_request_id = _validate_identifier("request_id", request_id)
    validated_trace_id = _validate_identifier("trace_id", trace_id)
    _request_id.set(validated_request_id)
    _trace_id.set(validated_trace_id)
    return validated_request_id, validated_trace_id


def get_request_id() -> str:
    request_id = _request_id.get()
    if request_id is None:
        request_id, _ = set_request_context(None, _trace_id.get())
    return request_id


def get_trace_id() -> str:
    trace_id = _trace_id.get()
    if trace_id is None:
        _, trace_id = set_request_context(_request_id.get(), None)
    return trace_id
