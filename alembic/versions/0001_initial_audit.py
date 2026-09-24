"""initial audit tables"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001_initial_audit"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "requests",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("trace_id", sa.String(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("final_status", sa.Integer(), nullable=False),
        sa.Column("selected_provider", sa.String(), nullable=True),
        sa.Column("total_latency_ms", sa.Float(), nullable=False),
        sa.Column("payload_size", sa.Integer(), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("error_class", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="requests_pkey"),
    )
    op.create_table(
        "provider_attempts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("error_class", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["request_id"], ["requests.id"], name="provider_attempts_request_id_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="provider_attempts_pkey"),
        sa.UniqueConstraint(
            "request_id",
            "attempt_number",
            name="uq_provider_attempt_request_number",
        ),
    )
    op.create_index("ix_requests_trace_id", "requests", ["trace_id"], unique=False)
    op.create_index("ix_requests_received_at", "requests", ["received_at"], unique=False)
    op.create_index("ix_requests_final_status", "requests", ["final_status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_requests_final_status", table_name="requests")
    op.drop_index("ix_requests_received_at", table_name="requests")
    op.drop_index("ix_requests_trace_id", table_name="requests")
    op.drop_table("provider_attempts")
    op.drop_table("requests")
