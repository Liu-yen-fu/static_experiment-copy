#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Iterable


NUMERIC_FIELDS = (
    "major_page_faults",
    "minor_page_faults",
    "resident_after_cold_pages",
    "requested_selected_resident_ratio",
    "successful_selected_resident_ratio",
    "first_query_latency_us",
    "effective_first_query_latency_us",
    "average_latency_us",
    "effective_average_query_latency_us",
)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def number(value: float | int | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, int) or float(value).is_integer():
        return str(int(value))
    return f"{value:.{digits}f}"


def to_float(value: str) -> float | None:
    if value == "" or value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def median(values: Iterable[float]) -> float | None:
    data = [value for value in values if value is not None]
    if not data:
        return None
    return statistics.median(data)


def quantile(values: Iterable[float], q: float) -> float | None:
    data = sorted(value for value in values if value is not None)
    if not data:
        return None
    if len(data) == 1:
        return data[0]
    pos = (len(data) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(data) - 1)
    frac = pos - lo
    return data[lo] * (1 - frac) + data[hi] * frac


def summarize_group(rows: list[dict[str, str]]) -> dict[str, str]:
    out: dict[str, str] = {"cells": str(len(rows))}
    for field in NUMERIC_FIELDS:
        values = [to_float(row.get(field, "")) for row in rows]
        values = [value for value in values if value is not None]
        out[f"{field}_median"] = number(median(values))
        out[f"{field}_p25"] = number(quantile(values, 0.25))
        out[f"{field}_p75"] = number(quantile(values, 0.75))
    return out


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract compact report evidence from static_experiment results/all_raw.csv."
    )
    parser.add_argument("--experiment-dir", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    experiment_dir = args.experiment_dir.resolve()
    raw_path = experiment_dir / "results" / "all_raw.csv"
    manifest_path = experiment_dir / "manifest.json"
    if not raw_path.is_file():
        parser.error(f"missing {raw_path}")

    rows = [row for row in read_csv(raw_path) if row.get("status") == "completed"]
    if not rows:
        parser.error("no completed rows found")

    output_dir = (args.output_dir or (experiment_dir / "results" / "report_evidence")).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    by_memory: dict[str, list[dict[str, str]]] = defaultdict(list)
    by_memory_strategy: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    by_memory_baseline: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        memory = row.get("memory_condition", "")
        strategy = row.get("strategy_key", "")
        by_memory[memory].append(row)
        by_memory_strategy[(memory, strategy)].append(row)
        if strategy == "baseline":
            by_memory_baseline[memory].append(row)

    evidence_rows: list[dict[str, str]] = []
    for memory, group in sorted(by_memory.items()):
        evidence_rows.append({"group": "all_completed", "memory_condition": memory, **summarize_group(group)})
    for memory, group in sorted(by_memory_baseline.items()):
        evidence_rows.append({"group": "baseline_only", "memory_condition": memory, **summarize_group(group)})
    for (memory, strategy), group in sorted(by_memory_strategy.items()):
        if strategy in {"baseline", "range_interior"} or strategy.startswith("residency_topk") or strategy.startswith("offset_topk"):
            evidence_rows.append({"group": "strategy", "memory_condition": memory, "strategy_key": strategy, **summarize_group(group)})

    write_csv(output_dir / "fault_residency_summary.csv", evidence_rows)

    memory_meta = []
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for item in manifest.get("memory_conditions", []):
            memory_meta.append(
                [
                    str(item.get("name", "")),
                    str(item.get("enabled", "")),
                    str(item.get("memory_max_bytes", "")),
                ]
            )

    md_lines = [
        "# Report Evidence Summary",
        "",
        "## Artifact locations",
        "",
        "- Per-operation page faults: each cell's `operations.csv`, columns `majflt_delta` and `minflt_delta`.",
        "- Per-cell totals: benchmark run record, fields `total_majflt` and `total_minflt`.",
        "- Consolidated results: `results/all_raw.csv`, columns `major_page_faults`, `minor_page_faults`, `resident_after_cold_pages`, `requested_selected_resident_ratio`, and `successful_selected_resident_ratio`.",
        "",
    ]
    if memory_meta:
        md_lines += [
            "## Configured memory conditions",
            "",
            markdown_table(["memory_condition", "enabled", "memory_max_bytes"], memory_meta),
            "",
        ]

    compact_rows = []
    for row in evidence_rows:
        if row["group"] in {"all_completed", "baseline_only"}:
            compact_rows.append(
                [
                    row["group"],
                    row.get("memory_condition", ""),
                    row.get("cells", ""),
                    row.get("major_page_faults_median", ""),
                    row.get("minor_page_faults_median", ""),
                    row.get("resident_after_cold_pages_median", ""),
                    row.get("effective_first_query_latency_us_median", ""),
                    row.get("effective_average_query_latency_us_median", ""),
                ]
            )
    md_lines += [
        "## Compact fault/residency summary",
        "",
        markdown_table(
            [
                "group",
                "memory",
                "cells",
                "median major faults",
                "median minor faults",
                "median resident after cold",
                "median effective first us",
                "median effective avg us",
            ],
            compact_rows,
        ),
        "",
        "Full CSV: `fault_residency_summary.csv`.",
        "",
    ]
    (output_dir / "report_evidence.md").write_text("\n".join(md_lines), encoding="utf-8")
    print(output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
