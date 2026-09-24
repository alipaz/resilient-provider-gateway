import structlog

from app.observability.metrics import persistence_dispatch_failures
from app.tasks.persist_audit import persist_audit

logger = structlog.get_logger()


def record_persistence_failure(reason: str) -> None:
    persistence_dispatch_failures.inc()


def dispatch_audit_event(event: dict, task=persist_audit) -> bool:
    try:
        task.apply_async(args=[event], queue="audit", retry=False)
    except Exception:
        logger.error("audit_publish_failed")
        record_persistence_failure("broker_publish_failed")
        return False
    return True
