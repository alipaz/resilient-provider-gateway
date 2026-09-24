import subprocess
import sys
from os import environ

import pytest


def test_200_rps_profile_paces_each_of_1000_users_to_one_request_per_five_seconds() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from locust.env import Environment; "
            "from benchmark.locustfile_200rps import GatewayUser; "
            "GatewayUser.host = 'http://test'; "
            "print(GatewayUser(Environment()).wait_time())",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert float(completed.stdout.strip()) == pytest.approx(5.0, abs=0.1)


def test_profile_uses_configured_target_rps_and_user_count() -> None:
    environment = environ | {"TARGET_RPS": "50", "BENCHMARK_USERS": "100"}
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from locust.env import Environment; "
            "from benchmark.locustfile_200rps import BENCHMARK_USERS, GatewayUser, TARGET_RPS; "
            "GatewayUser.host = 'http://test'; "
            "print(TARGET_RPS, BENCHMARK_USERS, GatewayUser(Environment()).wait_time())",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    target_rps, users, wait_seconds = completed.stdout.split()
    assert (target_rps, users) == ("50.0", "100")
    assert float(wait_seconds) == pytest.approx(2.0, abs=0.1)
