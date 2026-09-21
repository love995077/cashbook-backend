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

WHAT THE OVERRIDE CANNOT DO
---------------------------
Replay the whole chain on SQLite. The initial migration emits
`DEFAULT (now())`, which MySQL has and SQLite does not, so `upgrade head`
against an empty SQLite file fails on the very first revision. That is a
property of the existing migrations, not of the override.

So the override is for pointing a migration at a DIFFERENT MYSQL - a local
container, a staging copy - rather than at a fundamentally different engine.
To rehearse the whole chain, use the MySQL in docker-compose.yml. For testing
application behaviour rather than migrations, the smoke scripts build their
schema with `Base.metadata.create_all` and skip alembic entirely.
"""

import os
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

# An explicit override wins; otherwise the app's own URL. See the module
# docstring for why this is not simply `settings.sqlalchemy_url`.
DATABASE_URL = os.environ.get("ALEMBIC_DATABASE_URL") or settings.sqlalchemy_url

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


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it (`alembic upgrade head --sql`)."""
    context.configure(
        url=DATABASE_URL,
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
        # TLS for managed providers; see Settings.connect_args. Dropped for
        # an overridden URL that is not MySQL: those args carry PyMySQL's
        # `ssl` dict, and `sqlite3.connect()` rejects it outright - so without
        # this the SQLite rehearsal the override exists to enable would fail
        # on connect.
        connect_args=settings.connect_args if DATABASE_URL.startswith("mysql") else {},
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
