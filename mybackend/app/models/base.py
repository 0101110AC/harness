"""ORM 基类、公共列与类型别名。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum as PyEnum
from typing import Any

from sqlalchemy import DateTime, JSON, MetaData, TypeDecorator, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 统一命名约定，保证 Alembic autogenerate 的约束名稳定
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# SQLite 用 JSON，PostgreSQL 自动升级为 JSONB
JSONType = JSON().with_variant(JSONB(), "postgresql")


class UTCDateTime(TypeDecorator):
    """始终返回带 UTC 时区的 datetime。

    背景：SQLite 没有原生时区类型，`DateTime(timezone=True)` 写进去是 UTC、
    读出来却是 naive —— 一旦和 `datetime.now(timezone.utc)` 比较就抛
    `TypeError: can't compare offset-naive and offset-aware datetimes`（D3 复盘 2.1）。
    这里在 bind/result 两端做归一化：存 UTC（不带偏移），读回补上 UTC。
    DDL 与 `DateTime(timezone=True)` 完全一致，不需要数据迁移。
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def enum_col(enum_cls: type[PyEnum], length: int = 32):
    """枚举列：底层 VARCHAR，存 .value，不做 PG 原生 enum（迁移友好）。"""
    from sqlalchemy import Enum as SAEnum

    return SAEnum(
        enum_cls,
        native_enum=False,
        length=length,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


class UUIDMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )


def now_sql():
    """跨方言的 now()（SQLite 用 CURRENT_TIMESTAMP）。"""
    return text("CURRENT_TIMESTAMP")


AnyDict = dict[str, Any]
