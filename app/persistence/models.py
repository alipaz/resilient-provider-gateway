from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID

metadata = MetaData()
requests = Table(
    "requests",
    metadata,
    Column("id", UUID(as_uuid=False), primary_key=True),
    Column("trace_id", String, nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=False),
    Column("final_status", Integer, nullable=False),
    Column("selected_provider", String),
    Column("total_latency_ms", Float, nullable=False),
    Column("payload_size", Integer, nullable=False),
    Column("payload_sha256", String(64), nullable=False),
    Column("error_class", String),
)
provider_attempts = Table(
    "provider_attempts",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("request_id", UUID(as_uuid=False), ForeignKey("requests.id"), nullable=False),
    Column("provider", String, nullable=False),
    Column("attempt_number", Integer, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("latency_ms", Float, nullable=False),
    Column("http_status", Integer),
    Column("outcome", String, nullable=False),
    Column("error_class", String),
    UniqueConstraint("request_id", "attempt_number", name="uq_provider_attempt_request_number"),
)
Index("ix_requests_trace_id", requests.c.trace_id)
Index("ix_requests_received_at", requests.c.received_at)
Index("ix_requests_final_status", requests.c.final_status)
