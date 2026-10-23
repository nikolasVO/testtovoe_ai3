import os

from alembic import context
from sqlalchemy import create_engine, pool

from app.config import Settings

url = os.getenv("DATABASE_URL", Settings().database_url)
url = url.replace("postgresql://", "postgresql+psycopg://", 1)
if context.is_offline_mode():
    context.configure(url=url, literal_binds=True, transaction_per_migration=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()
