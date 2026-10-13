"""Alembic environment: create schema from driftline.persistence.models metadata."""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Import all model modules so target_metadata covers every table
# (paper tables register on the shared Base only when imported).
from driftline.paper import models as _paper_models  # noqa: F401
from driftline.persistence.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

if not config.get_main_option("sqlalchemy.url"):
    # Precedence: explicit programmatic config first, then DATABASE_URL env,
    # then the local SQLite default. (An explicitly configured URL must win so
    # tests and tooling can target isolated databases even when DATABASE_URL
    # is set in the environment.)
    db_url = os.environ.get("DATABASE_URL", "sqlite:///./driftline.db")
    # The app uses SQLAlchemy 2.x with psycopg 3 ("postgresql+psycopg://"),
    # which engine_from_config supports natively. (Do NOT rewrite to
    # +psycopg2: psycopg2 is not a project dependency.)
    config.set_main_option("sqlalchemy.url", db_url)


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
