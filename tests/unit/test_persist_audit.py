from app.persistence.models import metadata
from app.tasks.celery_app import celery_app
from app.tasks.persist_audit import persist_audit


def test_audit_queue_is_durable_bounded_and_confirmed() -> None:
    queue = celery_app.conf.task_queues[0]
    assert queue.name == "audit"
    assert queue.queue_arguments == {"x-max-length": 100000, "x-overflow": "reject-publish"}
    assert celery_app.conf.broker_transport_options["confirm_publish"] is True
    assert celery_app.conf.task_ignore_result is True


def test_persist_task_writes_one_complete_event(monkeypatch) -> None:
    saved: list[dict] = []
    monkeypatch.setattr("app.tasks.persist_audit.persist_audit_event", saved.append)

    persist_audit.run({"request": {"id": "00000000-0000-0000-0000-000000000001"}, "attempts": []})

    assert len(saved) == 1


def test_audit_metadata_has_required_tables_and_indexes() -> None:
    assert {"requests", "provider_attempts"} <= set(metadata.tables)
    assert {"ix_requests_trace_id", "ix_requests_received_at", "ix_requests_final_status"} == {
        index.name for index in metadata.tables["requests"].indexes
    }
