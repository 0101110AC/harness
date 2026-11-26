"""Alembic 异步环境：连接串统一取自 app.core.config.settings。"""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import settings
from app.models import Base  # 必须导入所有模型，autogenerate 才能看到 23 张表

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", settings.DB_URL)

target_metadata = Base.metadata

# LangGraph Checkpointer 自己建的表（checkpoints/checkpoint_blobs/checkpoint_writes/...）
# 不属于业务 schema：必须从 autogenerate 里排除，否则 `alembic check` 会一直报
# "要 drop 这些表"，D4 之后每次迁移都会被这条噪音挡住。
LANGGRAPH_TABLES = {
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
    "writes",
    "store",
    "store_migrations",
}


def include_object(object_, name, type_, reflected, compare_to):  # noqa: ARG001
    if type_ == "table" and name in LANGGRAPH_TABLES:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=settings.DB_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
        render_as_batch=settings.is_sqlite,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
        # SQLite 不支持大部分 ALTER，用 batch 模式重建表
        render_as_batch=settings.is_sqlite,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
