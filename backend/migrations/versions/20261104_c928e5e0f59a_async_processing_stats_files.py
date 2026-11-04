"""async processing stats files

Revision ID: c928e5e0f59a
Revises: 654739fe891b
Create Date: 2026-11-04 16:30:20.800867
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c928e5e0f59a"
down_revision: str | None = "654739fe891b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "processed_messages",
        sa.Column("consumer", sa.String(length=64), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("consumer", "message_id", name=op.f("pk_processed_messages")),
    )
    op.create_index(op.f("ix_processed_messages_processed_at"), "processed_messages", ["processed_at"], unique=False)
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("s3_key", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="PENDING", nullable=False),
        sa.Column("uploaded_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('PENDING', 'READY')", name=op.f("ck_documents_status_valid")),
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_documents_size_positive")),
        sa.ForeignKeyConstraint(
            ["event_id"], ["events.id"], name=op.f("fk_documents_event_id_events"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by"], ["users.id"], name=op.f("fk_documents_uploaded_by_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint("s3_key", name=op.f("uq_documents_s3_key")),
    )
    op.create_index(op.f("ix_documents_event_id"), "documents", ["event_id"], unique=False)
    op.create_table(
        "event_summaries",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("peak_occupancy", sa.Integer(), nullable=False),
        sa.Column("peak_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_entries", sa.Integer(), nullable=False),
        sa.Column("total_exits", sa.Integer(), nullable=False),
        sa.Column("revenue_minor", sa.BigInteger(), nullable=False),
        sa.Column("avg_stay_minutes", sa.Integer(), nullable=True),
        sa.Column("busiest_hour", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_rate", sa.Double(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"], ["events.id"], name=op.f("fk_event_summaries_event_id_events"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_event_summaries")),
    )
    op.create_table(
        "hourly_stats",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("gate_id", sa.Uuid(), nullable=False),
        sa.Column("hour_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entries", sa.Integer(), server_default="0", nullable=False),
        sa.Column("exits", sa.Integer(), server_default="0", nullable=False),
        sa.Column("rejections", sa.Integer(), server_default="0", nullable=False),
        sa.Column("revenue_minor", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("released_unpaid_minor", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("decision_ms_sum", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("decision_count", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"], ["events.id"], name=op.f("fk_hourly_stats_event_id_events"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["gate_id"], ["gates.id"], name=op.f("fk_hourly_stats_gate_id_gates"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("event_id", "gate_id", "hour_start", name=op.f("pk_hourly_stats")),
    )
    op.create_table(
        "reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="QUEUED", nullable=False),
        sa.Column("s3_key", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('CSV', 'PDF')", name=op.f("ck_reports_kind_valid")),
        sa.CheckConstraint("status IN ('QUEUED', 'RUNNING', 'READY', 'FAILED')", name=op.f("ck_reports_status_valid")),
        sa.ForeignKeyConstraint(
            ["event_id"], ["events.id"], name=op.f("fk_reports_event_id_events"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"], ["users.id"], name=op.f("fk_reports_requested_by_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reports")),
    )
    op.create_index(op.f("ix_reports_event_id"), "reports", ["event_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_reports_event_id"), table_name="reports")
    op.drop_table("reports")
    op.drop_table("hourly_stats")
    op.drop_table("event_summaries")
    op.drop_index(op.f("ix_documents_event_id"), table_name="documents")
    op.drop_table("documents")
    op.drop_index(op.f("ix_processed_messages_processed_at"), table_name="processed_messages")
    op.drop_table("processed_messages")
