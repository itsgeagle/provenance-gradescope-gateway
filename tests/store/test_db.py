from provgate.store.db import connect


def test_schema_tables_exist() -> None:
    conn = connect(":memory:")
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"classes", "secrets", "forwarded_submissions", "runs"} <= names


def test_connect_is_idempotent() -> None:
    conn = connect(":memory:")
    # applying schema twice on the same connection must not error
    from provgate.store.db import SCHEMA_SQL

    conn.executescript(SCHEMA_SQL)
