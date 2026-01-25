from __future__ import with_statement

import os
from dotenv import load_dotenv  # NEW
from alembic import context
from sqlalchemy import engine_from_config, pool

# Load .env before reading DATABASE_URL
load_dotenv()  # NEW

# Alembic Config object
config = context.config

# Set DB URL from env
db_url = os.getenv("DATABASE_URL")
if not db_url:
    raise RuntimeError("DATABASE_URL is not set")

config.set_main_option("sqlalchemy.url", db_url)

# If you later define SQLAlchemy Base, import it here:
# from backend.app.db import Base
# target_metadata = Base.metadata
from backend.app.db import Base
from backend.app import models  # ensure models are imported
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()