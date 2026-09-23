from celery import Celery
from kombu import Queue

celery_app = Celery("audit", broker="amqp://guest:guest@localhost:5672//")
celery_app.conf.update(
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
