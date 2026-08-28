"""Alembic environment (T0.3).

WHY THIS EXISTS AT ALL. Until now the schema was applied by
`Base.metadata.create_all` at boot (main.py) and rebuilt wholesale by
`app.cli reset-demo`. create_all CREATES MISSING TABLES AND NOTHING ELSE — it
never adds a column, never widens a type, never makes a column nullable, and it
reports no error when it skips one. models.py says so in six separate comments,
each written after someone discovered it the hard way. The variant work adds
tables *and* changes an existing column, so the gap stops being theoretical.

WHICH DATABASE. In order: a connection the caller handed us, then a url the
caller set, then app.config.DATABASE_URL. `sqlalchemy.url` is left empty in
alembic.ini on purpose, so the ini never becomes a second source of truth for
production. The caller-wins order is not decoration: an earlier version read
app.config unconditionally, which meant a caller passing a throwaway database
was silently ignored and the migration ran against the real one.

BATCH MODE IS NOT OPTIONAL. SQLite cannot ALTER COLUMN. Alembic's batch mode
emulates it by building a new table, copying the rows, and swapping it in —
without `render_as_batch=True` the first `alter_column` we write dies on the dev
default, which is the very migration this tool was adopted to perform
(price_aed_min becoming nullable, T0.7). Harmless on Postgres, which takes the
native path anyway.
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Import the models package so every table is registered on Base.metadata before
# autogenerate compares. Importing app.db alone gives an EMPTY metadata and
# autogenerate would cheerfully write a migration that drops all 17 tables.
import app.models  # noqa: F401
from app.config import DATABASE_URL
from app.db import Base

config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers=False IS LOAD-BEARING, and its default is True.
    # fileConfig reconfigures logging for the WHOLE PROCESS, and by default it
    # switches off every logger that already exists. Alembic normally runs as a
    # standalone command where that is harmless — but here it also runs inside
    # the app (migrate.upgrade_to_head at boot) and inside pytest, where it
    # silently killed five unrelated tests that assert on log output, in files
    # nobody had touched. The failure looks like flakiness and is not.
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _url() -> str:
    """Where to migrate. A CALLER'S URL WINS; app.config is the fallback.

    The first version of this set the url from app.config unconditionally, and
    that is a genuinely dangerous shape: a caller that carefully passes a
    throwaway database is silently ignored and the migration runs against
    whatever DATABASE_URL says. It cost a test suite that stamped the real dev
    database instead of its own temp files. Data survived; the principle did not.

    So the precedence is: an explicitly supplied url (tests, tooling, a
    one-off `alembic -x`), then app.config.DATABASE_URL. alembic.ini's own value
    stays empty, so it never becomes the second source of truth for production.
    """
    supplied = config.get_main_option("sqlalchemy.url", None)
    return supplied or DATABASE_URL


target_metadata = Base.metadata


def _is_sqlite() -> bool:
    return _url().startswith("sqlite")


def run_migrations_offline() -> None:
    """`--sql` mode: emit SQL to stdout without a DBAPI connection."""
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=_is_sqlite(),
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Normal mode: connect and run."""
    # A CONNECTION SUPPLIED BY THE CALLER WINS over building our own. This is
    # what lets app/migrate.py run inside a transaction it already owns, and what
    # lets a test point the whole thing at a temp database by handing over an
    # engine — without either of them having to mutate global config and hope.
    supplied = config.attributes.get("connection")
    if supplied is not None:
        _run_with(supplied)
        return

    section = dict(config.get_section(config.config_ini_section, {}) or {})
    section["sqlalchemy.url"] = _url()
    connectable = engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _run_with(connection)


def _run_with(connection) -> None:
    """Configure and run against one connection, however we got it."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # See the module docstring: SQLite has no ALTER COLUMN.
        render_as_batch=_is_sqlite(),
        # Catch a type that changed in models.py but not in the DB. Off by
        # default in alembic, and its absence is a classic source of
        # "the migration ran and changed nothing".
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
