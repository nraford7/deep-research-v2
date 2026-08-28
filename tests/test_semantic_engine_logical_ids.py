import sqlite3

from vendor.semantic_search import search


def test_v3_migration_backfills_stable_identity_columns():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE files(path TEXT PRIMARY KEY, mtime REAL NOT NULL, sha1 TEXT NOT NULL, chunk_count INTEGER NOT NULL, model TEXT NOT NULL DEFAULT '')")
    conn.execute("INSERT INTO files VALUES ('topic/old.md', 1, 'abc', 1, 'model')")

    search._migrate_v3_to_v4(conn)

    row = conn.execute("SELECT logical_id, display_path FROM files").fetchone()
    assert row == ("topic/old.md", "topic/old.md")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4


def test_v4_schema_declares_unique_logical_identity():
    source = open(search.__file__, encoding="utf-8").read()
    assert "logical_id TEXT UNIQUE" in source
    assert "CREATE UNIQUE INDEX IF NOT EXISTS files_logical_id_uq" in source
