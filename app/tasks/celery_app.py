from celery import Celery
from kombu import Queue

from app.config import Settings


def create_celery_app(settings: Settings | None = None) -> Celery:
    configured = settings or Settings()
    app = Celery(
        "audit",
        broker=configured.RABBITMQ_URL,
        include=["app.tasks.persist_audit"],
    )
    app.conf.update(
        result_backend=None,
        task_ignore_result=True,
        task_default_queue="audit",
        task_queues=(
            Queue(
                "audit",
                durable=True,
                queue_arguments={"x-max-length": 100000, "x-overflow": "reject-publish"},
            ),
        ),
        broker_transport_options={"confirm_publish": True},
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        broker_connection_retry_on_startup=True,
    )
    return app


celery_app = create_celery_app()
celery_app.loader.import_task_module("app.tasks.persist_audit")
