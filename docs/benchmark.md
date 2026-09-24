# Benchmark record

## Environment

- Date: 2026-09-24
- Deployment: Docker Compose on a local Mac, with one API container and one Celery worker.
- Provider mode: `external`; the gateway called the three supplied provider endpoints.
- Persistence: RabbitMQ audit queue with a Celery worker writing to PostgreSQL.

## External connectivity smoke

After rebuilding the current external-mode stack, a gateway smoke request returned HTTP
200 through provider C. The earlier connectivity timeouts were transient and are not
representative of the final environment.

## External controlled-load run

The final recorded run used the configurable fixed-throughput profile:

```sh
TARGET_RPS=50 BENCHMARK_USERS=1000 \
.venv/bin/python -m locust \
  -f benchmark/locustfile_200rps.py \
  --host http://127.0.0.1:8000 \
  --headless -u 1000 -r 100 -t 60s \
  --csv benchmark/results/external-fixed-50 \
  --csv-full-history \
  --html benchmark/results/external-fixed-50-report.html \
  --only-summary
```

The configured target was 50 RPS. Locust reported 55.38 aggregate RPS because user
startup contributes immediate requests during the ramp. Results therefore use the
measured rate rather than claiming an exact 50 RPS arrival rate.

| Metric | Result |
| --- | ---: |
| Requests | 3,000 |
| Successful requests | 641 |
| Failed requests | 2,359 |
| Failure rate | 78.63% |
| Aggregate RPS | 55.38 |
| Approximate successful RPS | 11.83 |
| Average latency | 715 ms |
| Median latency | 140 ms |
| P95 latency | 4,200 ms |
| P99 latency | 9,200 ms |
| Maximum latency | 11,251 ms |

The latency distribution includes fast rejected requests, so the 140 ms median must not
be interpreted as successful upstream latency. The reported successful RPS is aggregate
RPS multiplied by the successful-request ratio.

### Errors and provider attempts

| Result | Count |
| --- | ---: |
| HTTP 503 `no_provider_available` | 2,326 |
| HTTP 429 rate-limited | 33 |
| Provider A successes | 298 |
| Provider B successes | 209 |
| Provider B 5xx attempts | 27 |
| Provider B read timeouts | 11 |
| Provider C successes | 134 |
| Provider C 5xx attempts | 16 |

Provider attempt counters increased by 695: 641 successes, 43 upstream 5xx results,
and 11 read timeouts. Provider A had 36 local provider-rate-limit rejections. The retry
budget was not exhausted.

At the end of the run, provider A's circuit was closed while providers B and C were
open. Once B and C were excluded, A's strict rate limit frequently left no eligible
provider, producing the dominant 503 response. This is a protected overload state, not
a claim that the gateway sustained 50 successful RPS.

### Persistence verification

| Counter | Before | After | Delta |
| --- | ---: | ---: | ---: |
| PostgreSQL requests | 156,486 | 159,486 | 3,000 |
| PostgreSQL provider attempts | 16,312 | 17,007 | 695 |

The PostgreSQL request delta exactly matched Locust's 3,000 requests. RabbitMQ's
`audit` queue drained to zero ready and zero unacknowledged messages, and
`persistence_dispatch_failures_total` remained zero.

## Findings and limits

- The gateway remained available and applied bounded rejection rather than building an
  unbounded queue or exhausting persistence resources.
- Reliability-first weighted routing, rate limits, bulkheads, bounded retry, and circuit
  breakers prevented repeated pressure on unavailable providers.
- The external providers, especially B and C, were the observed bottleneck at this load;
  their failures opened both circuits and reduced aggregate successful throughput.
- This run does not demonstrate 50 sustainable successful RPS, and no 1,000-10,000 RPS
  runtime claim is made. That range is addressed by the documented horizontal-scaling
  and asynchronous persistence strategy.
- Higher external stages were intentionally not run after the 50-RPS stage exceeded the
  5% stop threshold. The earlier unconstrained overload runs are not presented as
  representative benchmark results.
