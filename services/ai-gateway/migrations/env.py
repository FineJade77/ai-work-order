import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.db import models  # noqa: F401  (registers tables on Base.metadata)
from app.db.base import Base

if context.config.config_file_name is not None:
    fileConfig(context.config.config_file_name)

# The owner account, not the runtime one: DDL and grants need ownership.
engine = create_engine(os.environ["MIGRATION_DATABASE_URL"], poolclass=pool.NullPool)

with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
