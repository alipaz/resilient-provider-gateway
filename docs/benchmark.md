# Benchmark record

## 2026-09-24 local mock smoke

- Command: `.venv/bin/python -m locust -f benchmark/locustfile.py --host http://127.0.0.1:8000 --headless -u 2 -r 2 -t 5s --csv benchmark/results/local-smoke`
- Concurrency: 2 users, ramped at 2 users per second, for five seconds.
- Result: 1 completed request, 0 failures, 0.50 RPS, 2016 ms average latency, and approximately 2000 ms for both P95 and P99.

This is a short local-mock smoke test only. It is not a representative
performance benchmark and must not be compared with external-provider results.

## 2026-09-24 external-provider connectivity smoke

Each endpoint was sent one `POST /v1/process` smoke request with an HTTPX
connect timeout of exactly five seconds. All three failed with `ConnectTimeout`:

| Provider | URL | Result |
| --- | --- | --- |
| A | `http://provider.example.invalid:8001/v1/process` | `ConnectTimeout: timed out` |
| B | `http://provider.example.invalid:8002/v1/process` | `ConnectTimeout: timed out` |
| C | `http://provider.example.invalid:8003/v1/process` | `ConnectTimeout: timed out` |

No external-provider benchmark was run. Local mock benchmark measurements must
not be treated as external-provider results.
