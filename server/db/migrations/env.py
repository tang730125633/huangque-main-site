"""Alembic environment for the Huangque PostgreSQL database."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, pool
from server.db.postgres import sqlalchemy_url

config = context.config
target_metadata = None


def database_url() -> str:
    url = (os.environ.get("HQ_DATABASE_URL") or "").strip()
    if not url:
        raise RuntimeError("HQ_DATABASE_URL is required for database migrations")
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=sqlalchemy_url(database_url()),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = sqlalchemy_url(database_url())
    engine = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
