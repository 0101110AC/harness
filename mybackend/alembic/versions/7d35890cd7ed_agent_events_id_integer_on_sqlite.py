"""agent_events id integer on sqlite

SQLite 只有 `INTEGER PRIMARY KEY` 才是 rowid 别名（自增）；
初始迁移里的 `BIGINT PRIMARY KEY` 在 SQLite 上会插入失败：
    sqlite3.IntegrityError: NOT NULL constraint failed: agent_events.id
PostgreSQL 的 BIGSERIAL 没有这个问题，所以本迁移**只在 SQLite 上做类型收敛**，
用 batch 模式重建表并保留已有事件（回放窗口里的数据不能丢）。

Revision ID: 7d35890cd7ed
Revises: 85e4a270ca55
Create Date: 2026-11-23 21:23:59.953016
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7d35890cd7ed"
down_revision: str | None = "85e4a270ca55"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return

    with op.batch_alter_table("agent_events", schema=None) as batch_op:
        batch_op.alter_column(
            "id",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=False,
            autoincrement=True,
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return

    with op.batch_alter_table("agent_events", schema=None) as batch_op:
        batch_op.alter_column(
            "id",
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=False,
            autoincrement=True,
        )
