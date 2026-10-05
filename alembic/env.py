"""Alembic environment.

The database URL comes from the same `Settings` the application uses, so a
migration cannot silently run against a different database than the app, and
no credentials sit in a committed file.

REHEARSING A MIGRATION SOMEWHERE SAFE
-------------------------------------
Set ALEMBIC_DATABASE_URL and that wins instead:

    ALEMBIC_DATABASE_URL=sqlite:///tmp/rehearsal.sqlite3 alembic upgrade head

This exists because the previous version ignored an explicitly supplied URL.
`config.set_main_option("sqlalchemy.url", ...)` was overwritten unconditionally
a few lines below, so a caller who passed a throwaway SQLite path got the
configured production MySQL and no warning that their override had been
discarded - which is exactly how this project's live database got migrated by
a script that believed it was talking to a temporary file.

An override is deliberately an ENV VAR rather than a parameter: it has to be
visible in the command that runs, not buried in a caller's config object.

ENGINES
-------
The chain builds PostgreSQL (production) and MySQL (the database being
migrated away from) alike, from empty to head. The early revisions were
written for MySQL, and the places where the two engines differ - enum types
above all - go through app/core/migration_support.py. `alembic check` against
either engine at head reports no difference from the models; the test suite
asserts it (tests/test_migrations.py).

What the chain cannot build is SQLite: the initial migration emits
`DEFAULT (now())`, which SQLite does not have. The tests build SQLite schemas
with `Base.metadata.create_all` instead.
"""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import connect_args_for, get_settings, normalise_database_url

# Importing the models registers every table on Base.metadata, which is what
# `--autogenerate` compares the live database against. Without this import
# autogenerate would see an empty model set and propose dropping everything.
from app import models  # noqa: F401
from app.database import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()

# An explicit override wins; otherwise the app's own URL. See the module
# docstring for why this is not simply `settings.sqlalchemy_url`.
DATABASE_URL = normalise_database_url(
    os.environ.get("ALEMBIC_DATABASE_URL") or settings.sqlalchemy_url
)

# Announced on every run. A migration is irreversible often enough that
# knowing which server is about to receive it should not require reading the
# source - and had this line existed, the accident above would have been
# obvious in the first line of output rather than after the fact.
_, _, _target = DATABASE_URL.rpartition("@")
print(f"alembic target: {_target or DATABASE_URL}")

# Escaped because ConfigParser treats a bare % as interpolation, and passwords
# from managed providers frequently contain one.
config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))

target_metadata = Base.metadata

# Off for MySQL. The models declare `server_default=func.now()`, which MySQL
# stores and reflects back as `CURRENT_TIMESTAMP`; those two never compare
# equal, so every autogenerate run would propose a no-op `modify_default` for
# all twelve timestamp columns and bury any real change in the noise.
COMPARE_SERVER_DEFAULT = False


def include_object(obj, name, type_, reflected, compare_to):
    """Leave the PostgreSQL-only case-insensitive indexes out of autogenerate.

    They are expression indexes on lower(name), created and owned by revision
    3b7d9e2a4c61; the models cannot declare them portably. Without this every
    autogenerate run against PostgreSQL would propose dropping them.
    """
    if type_ == "index" and name and name.endswith("_ci"):
        return False
    return True


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it (`alembic upgrade head --sql`)."""
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=COMPARE_SERVER_DEFAULT,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        # Shaped for the engine actually being migrated, not the app's own:
        # PyMySQL's `ssl` dict breaks psycopg and sqlite3 on connect, and an
        # overridden URL may be either. The app's TLS settings apply only when
        # the override is absent, i.e. when this IS the app's database.
        connect_args=(
            settings.connect_args
            if "ALEMBIC_DATABASE_URL" not in os.environ
            else connect_args_for(DATABASE_URL)
        ),
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # A changed column type would otherwise be ignored silently.
            compare_type=True,
            compare_server_default=COMPARE_SERVER_DEFAULT,
            include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
