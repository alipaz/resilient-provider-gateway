import os

from locust import HttpUser, constant_throughput, task

TARGET_RPS = float(os.getenv("TARGET_RPS", "200"))
BENCHMARK_USERS = int(os.getenv("BENCHMARK_USERS", "1000"))


class GatewayUser(HttpUser):
    wait_time = constant_throughput(TARGET_RPS / BENCHMARK_USERS)

    @task
    def process(self) -> None:
        self.client.post(
            "/v1/process",
            json={"value": "benchmark-fixed-200-rps"},
            name="POST /v1/process",
        )
