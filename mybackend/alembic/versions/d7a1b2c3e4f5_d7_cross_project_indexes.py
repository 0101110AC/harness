"""D7: 跨项目批注 / 工作台聚合索引

Revision ID: d7a1b2c3e4f5
Revises: b3f1a9c72d40
Create Date: 2025-11-25 12:00:00

索引全部来自压测后的 EXPLAIN，不是"先加再说"：

1. `annotations (assignee_id, status, updated_at)`
   跨项目批注页与工作台"指派给我的未关闭批注"共用。旧 `(assignee_id, status)`
   是它的前缀，保留只会增加写放大 → 直接替换。
   合成数据（5000 条批注）实测：0.027ms vs 旧计划 2.86ms（还带 TEMP B-TREE）。
2. `annotations (project_id, status, updated_at)`
   项目内批注按状态筛选后按 updated_at 排序；旧索引没有排序键。
3. `drawings (project_id, status, is_deleted)`
   工作台"待审图纸数"按项目聚合，避免 `ix_drawings_status` 全表扫描后回表过滤项目。

**没有新增 `notifications (user_id, is_read)`**：
D5 已有 `(user_id, is_read, created_at)`，未读数查询只用到前两列，EXPLAIN 明确走
`ix_notifications_user_read_created (user_id=? AND is_read=?)`。D7 曾尝试加一条部分
索引（`WHERE is_read=false AND merged_into_id IS NULL`），但在 SQLite 上规划器从未选中，
属于纯写放大，故删除。索引是否"缺失"以 EXPLAIN 为准，不以直觉为准。

SQLite 的布尔值存 0/1，PostgreSQL 是 true/false；本迁移没有跨方言谓词，无需分支。
"""
from __future__ import annotations

from alembic import op

revision: str = "d7a1b2c3e4f5"
down_revision: str | None = "b3f1a9c72d40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("annotations", schema=None) as batch_op:
        batch_op.drop_index("ix_annotations_assignee_status")
        batch_op.create_index(
            "ix_annotations_assignee_status_updated",
            ["assignee_id", "status", "updated_at"],
            unique=False,
        )
        batch_op.create_index(
            "ix_annotations_project_status_updated",
            ["project_id", "status", "updated_at"],
            unique=False,
        )

    with op.batch_alter_table("drawings", schema=None) as batch_op:
        batch_op.create_index(
            "ix_drawings_project_status",
            ["project_id", "status", "is_deleted"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("drawings", schema=None) as batch_op:
        batch_op.drop_index("ix_drawings_project_status")

    with op.batch_alter_table("annotations", schema=None) as batch_op:
        batch_op.drop_index("ix_annotations_project_status_updated")
        batch_op.drop_index("ix_annotations_assignee_status_updated")
        batch_op.create_index(
            "ix_annotations_assignee_status", ["assignee_id", "status"], unique=False
        )
