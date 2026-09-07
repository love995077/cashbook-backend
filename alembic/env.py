"""Alembic environment.

The database URL is NOT read from alembic.ini — it comes from the same
`Settings` the application uses, so migrations can never run against a
different database than the app, and no credentials sit in a committed file.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import get_settings

# Importing the models registers every table on Base.metadata, which is what
# `--autogenerate` compares the live database against. Without this import
# autogenerate would see an empty model set and propose dropping everything.
from app import models  # noqa: F401
from app.database import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()

# Escaped because ConfigParser treats a bare % as interpolation, and passwords
# from managed providers frequently contain one.
config.set_main_option("sqlalchemy.url", settings.sqlalchemy_url.replace("%", "%%"))

target_metadata = Base.metadata

# Off for MySQL. The models declare `server_default=func.now()`, which MySQL
# stores and reflects back as `CURRENT_TIMESTAMP`; those two never compare
# equal, so every autogenerate run would propose a no-op `modify_default` for
# all twelve timestamp columns and bury any real change in the noise.
COMPARE_SERVER_DEFAULT = False


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it (`alembic upgrade head --sql`)."""
    context.configure(
        url=settings.sqlalchemy_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=COMPARE_SERVER_DEFAULT,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        # TLS for managed providers; see Settings.connect_args.
        connect_args=settings.connect_args,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # A changed column type would otherwise be ignored silently.
            compare_type=True,
            compare_server_default=COMPARE_SERVER_DEFAULT,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
