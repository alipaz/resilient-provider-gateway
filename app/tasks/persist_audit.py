from sqlalchemy.exc import SQLAlchemyError

from app.persistence.repository import persist_audit_event
from app.tasks.celery_app import celery_app


@celery_app.task(
    bind=True,
    autoretry_for=(SQLAlchemyError, OSError),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def persist_audit(self, event: dict) -> None:
    persist_audit_event(event)
