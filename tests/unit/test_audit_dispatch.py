from app.persistence.dispatch import dispatch_audit_event


class Task:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.events: list[dict] = []

    def apply_async(self, args, queue: str, retry: bool) -> None:
        if self.error:
            raise self.error
        self.events.append(args[0])


def test_dispatch_publishes_to_audit_queue() -> None:
    task = Task()
    assert dispatch_audit_event({"request": {}, "attempts": []}, task=task) is True
    assert task.events == [{"request": {}, "attempts": []}]


def test_rejected_publish_is_recorded_without_changing_response(monkeypatch) -> None:
    failures: list[str] = []
    monkeypatch.setattr("app.persistence.dispatch.record_persistence_failure", failures.append)
    response = {"status": 200}

    assert dispatch_audit_event({}, task=Task(ConnectionError("rejected"))) is False
    assert failures == ["broker_publish_failed"]
    assert response == {"status": 200}
