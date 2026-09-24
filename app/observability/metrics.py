from prometheus_client import Counter, Gauge, Histogram

request_counter = Counter("gateway_requests_total", "Gateway requests", ["method", "status_class"])
request_duration = Histogram("gateway_request_duration_seconds", "Gateway request duration")
provider_attempt_counter = Counter(
    "provider_attempts_total", "Provider attempts", ["provider", "outcome"]
)
provider_duration = Histogram("provider_duration_seconds", "Provider duration", ["provider"])
provider_inflight = Gauge("provider_inflight", "Provider inflight", ["provider"])
circuit_state = Gauge("circuit_state", "Circuit state", ["provider"])
rate_limit_rejections = Counter(
    "rate_limit_rejections_total", "Rate limit rejections", ["scope", "provider"]
)
persistence_dispatch_failures = Counter(
    "persistence_dispatch_failures_total", "Persistence dispatch failures"
)
