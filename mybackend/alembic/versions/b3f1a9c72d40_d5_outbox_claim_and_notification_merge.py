"""D5: outbox 认领租约 + 站内信合并台账

Revision ID: b3f1a9c72d40
Revises: 7d35890cd7ed
Create Date: 2025-06-01 09:00:00

三个新增列都有明确用途，缺任何一个都会让 feature 退化成"看起来能用"：

1. `domain_events.claimed_at`：消费者认领事件的时刻。没有它，进程崩在中途时
   事件永远停在 PROCESSING，站内信静默丢失（复盘 2.2）。
2. `notifications.merged_into_id`：批量合并后仍保留每条事件的幂等台账，
   但列表/未读数把它过滤掉。
3. `notifications.merge_count`：合并父行累计条数（"5 条批注待处理"）。

注意：SQLite 的 `ALTER TABLE ... ADD COLUMN` 不支持非空且带 volatile 默认值，
所以 `merge_count` 用常量 server_default='1'，迁移后即生效，不需要回填脚本。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "b3f1a9c72d40"
down_revision: str | None = "7d35890cd7ed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("domain_events", schema=None) as batch_op:
        batch_op.add_column(sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))

    with op.batch_alter_table("notifications", schema=None) as batch_op:
        batch_op.add_column(sa.Column("merged_into_id", sa.Uuid(), nullable=True))
        batch_op.add_column(
            sa.Column("merge_count", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.create_index(
            "ix_notifications_user_type_created", ["user_id", "type", "created_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("notifications", schema=None) as batch_op:
        batch_op.drop_index("ix_notifications_user_type_created")
        batch_op.drop_column("merge_count")
        batch_op.drop_column("merged_into_id")

    with op.batch_alter_table("domain_events", schema=None) as batch_op:
        batch_op.drop_column("claimed_at")
