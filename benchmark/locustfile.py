from locust import HttpUser, between, task


class GatewayUser(HttpUser):
    wait_time = between(0.1, 0.5)

    @task
    def process(self) -> None:
        self.client.post("/v1/process", json={"value": "benchmark"}, name="POST /v1/process")
