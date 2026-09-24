import importlib.util
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.persistence.models import metadata
from app.persistence.repository import persist_audit_event
from app.tasks.celery_app import celery_app, create_celery_app
from app.tasks.persist_audit import persist_audit


def _event() -> dict:
    return {
        "request": {
            "id": "00000000-0000-0000-0000-000000000001",
            "trace_id": "trace-1",
            "received_at": "2026-01-01T00:00:00+00:00",
            "completed_at": "2026-01-01T00:00:01+00:00",
            "final_status": 200,
            "selected_provider": "provider_a",
            "total_latency_ms": 10.0,
            "payload_size": 2,
            "payload_sha256": "a" * 64,
            "error_class": None,
        },
        "attempts": [
            {
                "request_id": "00000000-0000-0000-0000-000000000001",
                "provider": "provider_a",
                "attempt_number": 1,
                "started_at": "2026-01-01T00:00:00+00:00",
                "latency_ms": 10.0,
                "http_status": 200,
                "outcome": "success",
                "error_class": None,
            },
            {
                "request_id": "00000000-0000-0000-0000-000000000001",
                "provider": "provider_b",
                "attempt_number": 2,
                "started_at": "2026-01-01T00:00:01+00:00",
                "latency_ms": 5.0,
                "http_status": 503,
                "outcome": "server_error",
                "error_class": None,
            },
        ],
    }


class RecordingTransaction:
    def __init__(self, session: "RecordingSession") -> None:
        self.session = session

    def __enter__(self) -> "RecordingSession":
        return self.session

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        if exc_type is None:
            self.session.committed = True
        else:
            self.session.rolled_back = True
        return False


class RecordingSession:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.statements: list[object] = []
        self.committed = False
        self.rolled_back = False

    def begin(self) -> RecordingTransaction:
        return RecordingTransaction(self)

    def __enter__(self) -> "RecordingSession":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        return False

    def execute(self, statement: object) -> None:
        if self.error is not None:
            raise self.error
        self.statements.append(statement)


def test_audit_queue_is_durable_bounded_and_confirmed() -> None:
    queue = celery_app.conf.task_queues[0]

    assert queue.name == "audit"
    assert queue.queue_arguments == {"x-max-length": 100000, "x-overflow": "reject-publish"}
    assert celery_app.conf.broker_transport_options["confirm_publish"] is True
    assert celery_app.conf.task_ignore_result is True


def test_clean_celery_configuration_registers_audit_task_and_uses_settings_broker() -> None:
    configured = create_celery_app(Settings(RABBITMQ_URL="amqp://configured//"))

    assert configured.conf.broker_url == "amqp://configured//"
    assert "app.tasks.persist_audit.persist_audit" in celery_app.tasks
    assert "app.tasks.persist_audit" in celery_app.conf.include


def test_persist_task_obtains_a_session_and_persists_complete_event(monkeypatch) -> None:
    session = RecordingSession()
    monkeypatch.setattr("app.tasks.persist_audit.audit_session_factory", lambda: session)

    persist_audit.run(_event())

    assert len(session.statements) == 3
    assert session.committed is True


def test_repository_inserts_one_request_and_all_attempts_in_one_transaction() -> None:
    session = RecordingSession()

    persist_audit_event(_event(), session)

    assert len(session.statements) == 3
    assert session.committed is True


def test_redelivery_uses_idempotent_postgresql_upserts() -> None:
    session = RecordingSession()

    persist_audit_event(_event(), session)

    compiled = [
        str(statement.compile(dialect=postgresql.dialect())) for statement in session.statements
    ]
    assert "ON CONFLICT (id) DO NOTHING" in compiled[0]
    assert "ON CONFLICT ON CONSTRAINT uq_provider_attempt_request_number DO NOTHING" in compiled[1]
    assert "ON CONFLICT ON CONSTRAINT uq_provider_attempt_request_number DO NOTHING" in compiled[2]


def test_database_errors_roll_back_and_propagate_for_celery_autoretry() -> None:
    session = RecordingSession(SQLAlchemyError("database unavailable"))

    with pytest.raises(SQLAlchemyError, match="database unavailable"):
        persist_audit_event(_event(), session)

    assert session.rolled_back is True


def test_audit_metadata_has_required_tables_and_indexes() -> None:
    assert {"requests", "provider_attempts"} <= set(metadata.tables)
    assert {"ix_requests_trace_id", "ix_requests_received_at", "ix_requests_final_status"} == {
        index.name for index in metadata.tables["requests"].indexes
    }


def test_migration_uses_explicit_upgrade_and_downgrade_operations(monkeypatch) -> None:
    migration_path = Path(__file__).parents[2] / "alembic/versions/0001_initial_audit.py"
    spec = importlib.util.spec_from_file_location("initial_audit_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    calls: list[tuple[str, str]] = []

    class Operations:
        def create_table(self, name: str, *args: object, **kwargs: object) -> None:
            calls.append(("create_table", name))

        def create_index(self, name: str, table_name: str, *args: object, **kwargs: object) -> None:
            calls.append(("create_index", name))

        def drop_index(self, name: str, *args: object, **kwargs: object) -> None:
            calls.append(("drop_index", name))

        def drop_table(self, name: str) -> None:
            calls.append(("drop_table", name))

    monkeypatch.setattr(migration, "op", Operations())

    migration.upgrade()
    migration.downgrade()

    assert calls == [
        ("create_table", "requests"),
        ("create_table", "provider_attempts"),
        ("create_index", "ix_requests_trace_id"),
        ("create_index", "ix_requests_received_at"),
        ("create_index", "ix_requests_final_status"),
        ("drop_index", "ix_requests_final_status"),
        ("drop_index", "ix_requests_received_at"),
        ("drop_index", "ix_requests_trace_id"),
        ("drop_table", "provider_attempts"),
        ("drop_table", "requests"),
    ]
