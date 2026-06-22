#!/usr/bin/env python3
"""Build a larger SQLite database for cold-start benchmarking."""

import os
import sqlite3


DB = "database.db"


def main() -> None:
    if os.path.exists(DB):
        os.remove(DB)

    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    cur.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=MEMORY;

        CREATE TABLE items (
          id INTEGER PRIMARY KEY,
          k1 TEXT NOT NULL,
          k2 TEXT NOT NULL,
          payload BLOB NOT NULL
        );

        CREATE INDEX idx_items_k1 ON items(k1);
        CREATE INDEX idx_items_k2 ON items(k2);

        WITH RECURSIVE cnt(x) AS (
          SELECT 1
          UNION ALL
          SELECT x + 1 FROM cnt WHERE x < 600000
        )
        INSERT INTO items(k1, k2, payload)
        SELECT
          printf('group_%04d', x % 1000),
          printf('tag_%06d', x),
          randomblob(100)
        FROM cnt;
        """
    )

    conn.commit()
    conn.close()

    size = os.path.getsize(DB)
    print(f"built {DB}: {size} bytes ({size / 1024 / 1024:.2f} MiB)")


if __name__ == "__main__":
    main()
