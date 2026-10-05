"""Helpers that let one Alembic chain build MySQL and PostgreSQL alike.

The migrations under alembic/versions/ were written against MySQL, which keeps
an ENUM's members inside the column definition. PostgreSQL does not: an enum is
a named TYPE that exists apart from any table. Three habits that are harmless
on MySQL therefore break the chain on PostgreSQL:

  * `op.add_column` with an `sa.Enum` never creates the type, so the ALTER
    fails with "type ... does not exist". Only `op.create_table` fires the
    event that creates it.
  * Reusing one enum in a second `op.create_table` creates it a second time
    and fails with "type ... already exists".
  * Changing an enum's members is a MODIFY COLUMN on MySQL and an ALTER TYPE
    on PostgreSQL. The column-level form simply does nothing to the type.

`ensure_enum` + `enum_type` cover the first two: the type is created once,
explicitly, with `checkfirst`, and every column then refers to it without
trying to create it again. The third is handled per migration, because what
is safe to do to a type depends on what is being changed.

Every helper is a no-op on MySQL, so the revisions that already ran against a
live MySQL database behave exactly as they always did.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


def dialect_name() -> str:
    return op.get_bind().dialect.name


def is_postgres() -> bool:
    return dialect_name() == "postgresql"


def ensure_enum(name: str, *values: str) -> None:
    """Create the named enum type on PostgreSQL if it does not exist yet."""
    if is_postgres():
        postgresql.ENUM(*values, name=name).create(op.get_bind(), checkfirst=True)


def enum_type(name: str, *values: str) -> sa.types.TypeEngine:
    """The column type for an enum created by `ensure_enum`.

    On PostgreSQL it refers to the existing type and never tries to create it;
    everywhere else it is the plain `sa.Enum` the migration always used.
    """
    if is_postgres():
        return postgresql.ENUM(*values, name=name, create_type=False)
    return sa.Enum(*values, name=name)


def add_enum_value(name: str, value: str) -> None:
    """Append one member to a PostgreSQL enum type. No-op elsewhere.

    `IF NOT EXISTS` so a rehearsal that re-runs a revision cannot fail on a
    value an earlier attempt already added. PostgreSQL 12+ accepts this inside
    a transaction block; the value just cannot be USED until that transaction
    commits, which no migration in this chain does.
    """
    if is_postgres():
        op.execute(f"ALTER TYPE {name} ADD VALUE IF NOT EXISTS '{value}'")
