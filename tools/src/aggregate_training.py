#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

try:
    from .common import atomic_json, sha256_file
except ImportError:
    from common import atomic_json, sha256_file

SCHEMA_VERSION = 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--classification", required=True, type=Path)
    parser.add_argument("--snapshots", required=True, type=Path, nargs="+")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--layout", required=True)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--workload-type", required=True)
    parser.add_argument("--memory-condition-json", required=True)
    parser.add_argument("--training-workloads", required=True, type=Path, nargs="+")
    args = parser.parse_args()
    memory_condition = json.loads(args.memory_condition_json)
    if not isinstance(memory_condition, dict) or set(memory_condition) != {"name", "enabled", "memory_max_bytes"}:
        parser.error("invalid memory condition JSON")

    if len(args.snapshots) != len(args.training_workloads) or not args.snapshots:
        parser.error("snapshot and training workload counts must match and be nonzero")
    pages: dict[int, tuple[str, int]] = {}
    with args.classification.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            page = int(row["page_number"])
            pages[page] = (row["page_type"], int(row["file_offset"]))
    counts = {page: 0 for page in pages}
    for snapshot in args.snapshots:
        seen: set[int] = set()
        with snapshot.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                page = int(row["page_number"])
                if page not in pages or page in seen or row["is_resident"] not in {"0", "1"}:
                    raise ValueError(f"invalid snapshot row in {snapshot}: {row}")
                seen.add(page)
                counts[page] += int(row["is_resident"])
        if seen != pages.keys():
            raise ValueError(f"snapshot does not cover every page: {snapshot}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_csv = args.output_dir / "residency_counts.csv"
    with output_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["page_number", "page_type", "file_offset", "residency_count", "training_run_count", "residency_rate"])
        for page in sorted(pages):
            page_type, offset = pages[page]
            writer.writerow([page, page_type, offset, counts[page], len(args.snapshots), f"{counts[page] / len(args.snapshots):.17g}"])
    interior = {"interior_table", "interior_index"}
    leaf = {"leaf_table", "leaf_index"}
    atomic_json(args.output_dir / "profile.json", {
        "schema_version": SCHEMA_VERSION,
        "layout": args.layout,
        "database_sha256": sha256_file(args.database),
        "workload_type": args.workload_type,
        "memory_condition": memory_condition,
        "training_workloads": [{"name": p.name, "sha256": sha256_file(p)} for p in args.training_workloads],
        "training_run_count": len(args.snapshots),
        "aggregation_metric": "resident snapshot count",
        "tie_break": "residency_count DESC, page_number ASC",
        "classification_sha256": sha256_file(args.classification),
        "profile_csv_path": str(output_csv.resolve()),
        "profile_csv_sha256": sha256_file(output_csv),
        "eligible_interior_count": sum(t in interior for t, _ in pages.values()),
        "eligible_leaf_count": sum(t in leaf for t, _ in pages.values()),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
