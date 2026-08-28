"""Migrations must agree with models.py, and adoption must not destroy data.

WHY THIS FILE IS THE POINT OF T0.3. A migration tool that is merely *present* is
worse than none: it invites everyone to believe the schema is managed while
models.py quietly drifts ahead of the migrations, and the drift is discovered on
the deploy where a column is missing in production and nowhere else.

So these are the two properties worth pinning, and neither is "alembic is
installed":

  1. `alembic upgrade head` produces the schema models.py describes — compared
     semantically (columns, nullability, keys, constraints, indexes), not by
     diffing DDL text, which differs harmlessly between the two paths.
  2. Adopting a database that already exists KEEPS ITS ROWS. That is the whole
     reason Alembic was adopted rather than continuing to drop and re-seed.

Offline: SQLite temp files only, no network, no vendor.
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine, inspect, text

import app.models  # noqa: F401 — registers every table on Base.metadata
from app.db import Base


def _alembic_config(db_url: str):
    from alembic.config import Config

    from app import migrate as migrate_mod

    cfg = Config(str(migrate_mod._ALEMBIC_INI))
    cfg.set_main_option("script_location", str(migrate_mod._ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _upgrade(db_url: str) -> None:
    from alembic import command

    command.upgrade(_alembic_config(db_url), "head")


def _snapshot(db_url: str) -> dict:
    """Semantic schema shape. Deliberately not raw DDL — see the module docstring."""
    insp = inspect(create_engine(db_url))
    out: dict = {}
    for table in sorted(insp.get_table_names()):
        if table == "alembic_version":
            continue
        out[table] = {
            "columns": sorted(
                (c["name"], str(c["type"]), bool(c["nullable"]))
                for c in insp.get_columns(table)
            ),
            "pk": sorted(insp.get_pk_constraint(table).get("constrained_columns") or []),
            "fks": sorted(
                (tuple(f["constrained_columns"]), f["referred_table"],
                 tuple(f["referred_columns"]))
                for f in insp.get_foreign_keys(table)
            ),
            "unique": sorted(
                tuple(sorted(u["column_names"])) for u in insp.get_unique_constraints(table)
            ),
            "indexes": sorted(
                (i["name"], tuple(i["column_names"]), bool(i["unique"]))
                for i in insp.get_indexes(table)
            ),
        }
    return out


def test_migrations_produce_exactly_the_schema_models_describe(tmp_path):
    """The drift guard. If someone edits models.py without a migration, this fails.

    Compared against `create_all`, which IS models.py expressed as DDL — so this
    asserts "the migrations say what the models say" without needing a
    hand-maintained copy of the expected schema.
    """
    by_models = f"sqlite:///{tmp_path / 'models.db'}"
    by_alembic = f"sqlite:///{tmp_path / 'alembic.db'}"

    engine = create_engine(by_models)
    Base.metadata.create_all(engine)
    engine.dispose()

    _upgrade(by_alembic)

    expected, actual = _snapshot(by_models), _snapshot(by_alembic)

    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    assert not missing, f"migrations never create: {missing}"
    assert not extra, f"migrations create tables models.py does not define: {extra}"

    for table in sorted(expected):
        assert actual[table] == expected[table], (
            f"{table} differs between models.py and the migrations.\n"
            f"  models.py  : {expected[table]}\n"
            f"  migrations : {actual[table]}"
        )


def test_autogenerate_finds_nothing_left_to_do(tmp_path):
    """No pending migration. The cheap, direct form of the same guarantee.

    Catches the case the comparison above cannot: a change alembic WOULD emit but
    that happens to round-trip identically through SQLite's weaker type system.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    db_url = f"sqlite:///{tmp_path / 'head.db'}"
    _upgrade(db_url)

    engine = create_engine(db_url)
    with engine.connect() as conn:
        diff = compare_metadata(
            MigrationContext.configure(conn), Base.metadata
        )
    assert diff == [], (
        "models.py and the migrations disagree — alembic would generate:\n  "
        + "\n  ".join(repr(d) for d in diff)
        + "\nRun: alembic revision --autogenerate -m '<what changed>'"
    )


def test_adopting_a_create_all_database_keeps_its_rows(tmp_path, monkeypatch):
    """The upgrade path for every database that exists today.

    Those were all built by create_all, so they hold all seventeen tables and no
    `alembic_version`. Running the baseline against one would try to CREATE
    tables that already exist and fail. It must be STAMPED instead — and, far
    more importantly, the customers already in it must still be there afterwards.
    This is the entire argument for adopting Alembic rather than continuing to
    drop and re-seed.
    """
    path = tmp_path / "legacy.db"
    db_url = f"sqlite:///{path}"

    # A database as it exists today: create_all, some rows, no alembic_version.
    # Inserted through the ORM, not raw SQL: several columns (language_pref among
    # them) carry PYTHON-side defaults, which the ORM applies and a bare INSERT
    # does not. Using the ORM is both what really happens in the app and the only
    # way this fixture resembles a database anyone actually has.
    from sqlalchemy.orm import Session

    from app.models import Customer

    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Customer(name_en="Real Customer", mobile="+971500000001"))
        s.commit()
    engine.dispose()

    from app import db as db_mod
    from app import migrate as migrate_mod

    adopt_engine = create_engine(db_url)
    monkeypatch.setattr(db_mod, "engine", adopt_engine)
    monkeypatch.setattr(migrate_mod, "engine", adopt_engine)

    assert migrate_mod.current_revision() is None, "fixture is not a pre-alembic DB"
    migrate_mod.upgrade_to_head()

    # Adopted...
    assert migrate_mod.current_revision() is not None
    # ...and the row survived. If this ever fails, the adoption path is deleting
    # customer data, which is the one outcome that must never happen quietly.
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 1
    assert raw.execute("SELECT name_en FROM customers").fetchone()[0] == "Real Customer"
    raw.close()
    adopt_engine.dispose()


def test_a_fresh_database_is_migrated_not_stamped(tmp_path, monkeypatch):
    """The other branch: nothing there yet, so actually run the migrations."""
    db_url = f"sqlite:///{tmp_path / 'fresh.db'}"

    from app import db as db_mod
    from app import migrate as migrate_mod

    fresh = create_engine(db_url)
    monkeypatch.setattr(db_mod, "engine", fresh)
    monkeypatch.setattr(migrate_mod, "engine", fresh)

    migrate_mod.upgrade_to_head()

    assert migrate_mod.current_revision() is not None
    tables = set(inspect(fresh).get_table_names())
    assert set(Base.metadata.tables) <= tables, (
        "a fresh database was stamped instead of migrated — it has no tables"
    )
    fresh.dispose()


@pytest.mark.parametrize("name", ["alembic.ini", "alembic"])
def test_the_migration_directory_is_shipped_in_the_image(name):
    """Dockerfile must COPY it. main.py runs migrations at boot; alembic resolves
    script_location on disk, so a container without these imports fine, boots,
    and dies in lifespan — invisible in local dev, fatal on deploy."""
    from pathlib import Path

    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
    assert f"COPY {name}" in dockerfile, f"Dockerfile does not ship {name}"


def test_an_empty_alembic_version_table_is_adopted_not_upgraded(tmp_path, monkeypatch):
    """An empty `alembic_version` table means the SAME thing as no table at all:
    nothing has been applied. This repo's own dev database was found in exactly
    that state — an empty version table beside seventeen populated ones.

    The original condition was `has_table("alembic_version")`, which calls such a
    database MANAGED, takes the upgrade path, and dies on "table vehicle_models
    already exists". Corrected at T0.5 to test for a REVISION, not a table.
    """
    from sqlalchemy.orm import Session

    from app import db as db_mod
    from app import migrate as migrate_mod
    from app.models import Customer

    path = tmp_path / "empty_version.db"
    db_url = f"sqlite:///{path}"

    # A create_all database that ALSO carries an empty alembic_version table.
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    with Session(engine) as sess:
        sess.add(Customer(name_en="Real Customer", mobile="+971500000002"))
        sess.commit()
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
    engine.dispose()

    adopt = create_engine(db_url)
    monkeypatch.setattr(db_mod, "engine", adopt)
    monkeypatch.setattr(migrate_mod, "engine", adopt)

    assert inspect(adopt).has_table("alembic_version"), "fixture must have the table"
    assert migrate_mod.current_revision() is None, "...but no revision in it"

    # The assertion that matters: this must not raise.
    migrate_mod.upgrade_to_head()

    assert migrate_mod.current_revision() is not None, "should now be stamped"
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT name_en FROM customers").fetchone()[0] == "Real Customer"
    raw.close()
    adopt.dispose()
