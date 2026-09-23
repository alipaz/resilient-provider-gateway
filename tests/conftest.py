from app.tasks.celery_app import celery_app

celery_app.conf.task_always_eager = True
celery_app.conf.task_eager_propagates = True
