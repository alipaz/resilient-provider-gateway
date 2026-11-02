# Gateway benchmark

Locust exercises `POST /v1/process` and its terminal report includes requests
per second, average latency, P95, P99, and failure rate. CSV output is written
under `benchmark/results/` and is intentionally ignored by Git.

## External providers

`make benchmark-external` runs the controlled fixed-throughput profile against
configured external providers: 50 target RPS, 1,000 users, a 100-user/second
ramp, and 60 seconds by default. It passes the same `BENCHMARK_USERS` value to
Locust and the profile, so per-user throughput is `TARGET_RPS / BENCHMARK_USERS`.

This command sends real traffic and must not be used as an unlimited or
closed-loop load test. Override the bounded parameters deliberately, for
example: `make benchmark-external TARGET_RPS=50 BENCHMARK_USERS=1000
BENCHMARK_DURATION=60s`.

Generated files remain under `benchmark/results/`. Add a representative result
only when its environment and run parameters are recorded with it.
