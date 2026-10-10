"""init

Revision ID: 0001
Revises:
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    tz = sa.DateTime(timezone=True)
    op.create_table(
        "profile",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("profile_text", sa.Text, nullable=False, server_default=""),
        sa.Column("targets", sa.JSON, nullable=False),
        sa.Column("settings", sa.JSON, nullable=False),
    )
    op.create_table(
        "plans",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("week_start", sa.Date, nullable=False, index=True),
        sa.Column("data", sa.JSON, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", tz, nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "food_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("eaten_at", tz, nullable=False, server_default=sa.func.now()),
        sa.Column("date", sa.Date, nullable=False, index=True),
        sa.Column("slot", sa.String(20)),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("items", sa.JSON, nullable=False),
        *[sa.Column(c, sa.Float, nullable=False, server_default="0") for c in ("kcal", "protein", "fat", "carbs", "fiber")],
        sa.Column("source", sa.String(10), nullable=False, server_default="text"),
        sa.Column("status", sa.String(10), nullable=False, server_default="eaten"),
    )
    op.create_table(
        "favorites",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("items", sa.JSON, nullable=False),
        *[sa.Column(c, sa.Float, nullable=False, server_default="0") for c in ("kcal", "protein", "fat", "carbs", "fiber")],
        sa.Column("uses", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_table(
        "weight_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("date", sa.Date, nullable=False, unique=True),
        sa.Column("weight", sa.Float, nullable=False),
    )
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("created_at", tz, nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "ai_usage",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("date", sa.Date, nullable=False, index=True),
        *[sa.Column(c, sa.Integer, nullable=False, server_default="0")
          for c in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")],
    )
    op.create_table(
        "sent_events",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("sent_at", tz, nullable=False, server_default=sa.func.now()),
    )


def downgrade():
    for t in ("sent_events", "ai_usage", "chat_messages", "weight_log", "favorites", "food_log", "plans", "profile"):
        op.drop_table(t)
