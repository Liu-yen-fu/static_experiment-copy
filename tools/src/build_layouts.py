#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

try:
    from .common import atomic_json, sha256_file, sqlite_header
except ImportError:
    from common import atomic_json, sha256_file, sqlite_header


def run_checked(command: list[str], *, stdout=None) -> None:
    subprocess.run(command, check=True, stdout=stdout)


def sqlite_metadata(path: Path) -> tuple[int, int, int, str]:
    page_size, page_count = sqlite_header(path)
    with sqlite3.connect(path) as connection:
        freelist = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        version = str(connection.execute("SELECT sqlite_version()").fetchone()[0])
    return page_size, page_count, freelist, version


def classify(tool: Path, database: Path, output: Path) -> None:
    with output.open("wb") as handle:
        run_checked([str(tool), str(database)], stdout=handle)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--classify-pages", type=Path)
    parser.add_argument("--layout-rewriter", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    classify_tool = (args.classify_pages or root / "tools/bin/classify_pages").resolve()
    rewrite_tool = (args.layout_rewriter or root / "tools/bin/layout_rewriter").resolve()
    source = args.source.resolve()
    if not source.is_file():
        parser.error(f"source does not exist: {source}")
    for tool in (classify_tool, rewrite_tool):
        if not tool.is_file():
            parser.error(f"tool does not exist: {tool}")
    source_hash = sha256_file(source)

    for name in ("original", "vacuum", "rewrite"):
        target_dir = args.output_dir.resolve() / name
        if target_dir.exists():
            if not target_dir.is_dir() or any(target_dir.iterdir()):
                raise FileExistsError(f"refusing to overwrite provisioned layout: {target_dir}")
        else:
            target_dir.mkdir(parents=True)
        database = target_dir / "database.db"
        fix_sql = target_dir / "fix.sql"
        if name == "original":
            shutil.copy2(source, database)
            transformation, tool_hash, fix_hash = "copy", None, None
        elif name == "vacuum":
            shutil.copy2(source, database)
            with sqlite3.connect(database) as connection:
                connection.execute("VACUUM")
            transformation, tool_hash, fix_hash = "vacuum", None, None
        else:
            with fix_sql.open("wb") as handle:
                run_checked([str(rewrite_tool), str(source), str(database)], stdout=handle)
            with sqlite3.connect(database) as connection:
                connection.executescript(fix_sql.read_text(encoding="utf-8"))
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise RuntimeError(f"rewritten database failed integrity_check: {integrity}")
            transformation, tool_hash, fix_hash = "type-aware-rewrite", sha256_file(rewrite_tool), sha256_file(fix_sql)
        classification = target_dir / "classify.csv"
        classify(classify_tool, database, classification)
        page_size, page_count, freelist, sqlite_version = sqlite_metadata(database)
        atomic_json(target_dir / "metadata.json", {
            "schema_version": 1, "layout": name, "source_db_sha256": source_hash,
            "output_db_sha256": sha256_file(database), "file_size": database.stat().st_size,
            "sqlite_page_size": page_size, "sqlite_page_count": page_count, "freelist_count": freelist,
            "classification_sha256": sha256_file(classification), "transformation": transformation,
            "transformation_tool_sha256": tool_hash, "sqlite_version": sqlite_version,
            "fix_sql_sha256": fix_hash,
        })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
