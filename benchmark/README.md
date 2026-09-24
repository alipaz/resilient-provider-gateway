# Gateway benchmark

Locust exercises `POST /v1/process` and its terminal report includes requests
per second, average latency, P95, P99, and failure rate. CSV output is written
under `benchmark/results/` and is intentionally ignored by Git.

## Local mocks

Start the stack, then choose a staged load profile:

| Concurrency | Command |
| --- | --- |
| 10 | `locust -f benchmark/locustfile.py --host http://localhost:8000 --headless -u 10 -r 2 -t 30s --csv benchmark/results/local-10` |
| 50 | `locust -f benchmark/locustfile.py --host http://localhost:8000 --headless -u 50 -r 10 -t 30s --csv benchmark/results/local-50` |
| 100 | `locust -f benchmark/locustfile.py --host http://localhost:8000 --headless -u 100 -r 20 -t 30s --csv benchmark/results/local-100` |
| 250 | `locust -f benchmark/locustfile.py --host http://localhost:8000 --headless -u 250 -r 50 -t 30s --csv benchmark/results/local-250` |

`make benchmark-local` runs the 10-user profile after starting the default
mock stack.

## External providers

`make benchmark-external` starts the same local stack with
`.env.external.example`, so the gateway resolves the three supplied external
provider URLs. Run an explicit five-second connectivity smoke check first;
unavailable external providers are not local benchmark results.

Generated files remain under `benchmark/results/`. Add a representative result
only when its environment and run parameters are recorded with it.
