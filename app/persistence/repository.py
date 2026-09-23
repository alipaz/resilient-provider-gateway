from sqlalchemy.dialects.postgresql import insert

from app.persistence.models import provider_attempts, requests


def persist_audit_event(event: dict, session=None) -> None:
    if session is None:
        return
    request = event["request"]
    session.execute(
        insert(requests).values(**request).on_conflict_do_nothing(index_elements=["id"])
    )
    for attempt in event["attempts"]:
        session.execute(
            insert(provider_attempts)
            .values(**attempt)
            .on_conflict_do_nothing(constraint="uq_provider_attempt_request_number")
        )
    session.commit()
