#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import defaultdict
from pathlib import Path

METRICS = ["prefetch_elapsed_us", "first_query_latency_us", "effective_first_query_latency_us", "average_latency_us", "major_page_faults", "minor_page_faults", "resident_after_cold_pages", "requested_selected_resident_ratio", "successful_selected_resident_ratio"]
IMPROVEMENT_METRICS = {"first_query_latency_us", "effective_first_query_latency_us", "average_latency_us"}
RAW_FIELDS = "cell_id status measurement_file repetition selected_interior selected_leaf syscall_count prefetch_elapsed_us first_query_latency_us average_latency_us major_page_faults minor_page_faults resident_after_cold_pages requested_selected_resident_ratio successful_selected_resident_ratio".split()
ALL_RAW_FIELDS = "experiment_id cell_id status workload_type layout memory_condition memory_limit_enabled memory_max_bytes strategy_key backend n interior_k leaf_k measurement_file repetition selected_interior selected_leaf syscall_count prefetch_elapsed_us first_query_latency_us average_latency_us major_page_faults minor_page_faults resident_after_cold_pages requested_selected_resident_ratio successful_selected_resident_ratio".split()
FIELDS = ["scope", "measurement_file", "metric", "sample_count", "mean", "median", "p25", "p75", "p99", "min", "max", "comparison_basis", "improvement_percent"]
STRATEGY_COMPARISON_FIELDS = ["strategy_key", "metric", "sample_count", "mean", "median", "p25", "p75", "p99", "min", "max", "baseline_mean", "improvement_percent"]
BACKEND_COMPARISON_FIELDS = ["backend", "strategy_key", "metric", "sample_count", "mean", "median", "p25", "p75", "p99", "min", "max", "baseline_mean", "improvement_percent"]
LAYOUT_COMPARISON_FIELDS = ["layout", "metric", "sample_count", "mean", "median", "p25", "p75", "p99", "min", "max", "original_baseline_mean", "improvement_percent"]
MEMORY_COMPARISON_FIELDS = ["memory_condition", "backend", "strategy_key", "metric", "sample_count", "mean", "median", "p25", "p75", "p99", "min", "max", "reference_mean", "change_percent"]


def nearest(values: list[float], percentile: int) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile / 100 * len(ordered)) - 1)]


def stats(values: list[float]) -> dict[str, float | int]:
    return {"sample_count": len(values), "mean": sum(values) / len(values), "median": nearest(values, 50), "p25": nearest(values, 25), "p75": nearest(values, 75), "p99": nearest(values, 99), "min": min(values), "max": max(values)}


def write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def paired_improvements(samples: list[dict[str, str]], metric: str, baselines: dict[tuple[str, str, str, str, str], dict[str, str]], baseline_layout: str) -> list[float]:
    output = []
    for row in samples:
        baseline = baselines.get((baseline_layout, row["_workload_type"], row["_memory_condition"], row["measurement_file"], row["repetition"]))
        if baseline and baseline.get(metric, "") not in {"", "0"} and row.get(metric, "") != "":
            output.append((float(baseline[metric]) - float(row[metric])) / float(baseline[metric]) * 100)
    return output


def add_derived_metrics(row: dict[str, str]) -> None:
    first = row.get("first_query_latency_us", "")
    if first == "":
        row["effective_first_query_latency_us"] = ""
        return
    prefetch = row.get("prefetch_elapsed_us", "")
    if row.get("_strategy_key") == "baseline":
        prefetch = "0"
    row["effective_first_query_latency_us"] = str(float(first) + float(prefetch)) if prefetch != "" else ""


def strategy_summary(samples: list[dict[str, str]], baselines: dict[tuple[str, str, str, str, str], dict[str, str]]) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    groups = [("measurement", name, [row for row in samples if row["measurement_file"] == name]) for name in dict.fromkeys(row["measurement_file"] for row in samples)]
    groups.append(("strategy", "", samples))
    for scope, measurement, group in groups:
        for metric in METRICS:
            values = [float(row[metric]) for row in group if row.get(metric, "") != ""]
            if not values:
                continue
            result: dict[str, object] = {"scope": scope, "measurement_file": measurement, "metric": metric, **stats(values), "comparison_basis": "", "improvement_percent": ""}
            is_baseline = group[0]["_strategy_key"] == "baseline"
            compare_layout = "original" if is_baseline else group[0]["_layout"]
            should_compare = not is_baseline or group[0]["_layout"] != "original"
            improvements = paired_improvements(group, metric, baselines, compare_layout) if metric in IMPROVEMENT_METRICS and should_compare else []
            if improvements:
                result["comparison_basis"] = "original-layout baseline" if is_baseline else "same-layout paired baseline"
                result["improvement_percent"] = sum(improvements) / len(improvements)
            output.append(result)
            if improvements:
                output.append({**result, "metric": metric.removesuffix("_latency_us") + "_improvement_percent", **stats(improvements), "improvement_percent": sum(improvements) / len(improvements)})
    return output


def strategy_comparison(rows: list[dict[str, str]], baselines: dict[tuple[str, str, str, str, str], dict[str, str]], include_backend: bool = False) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    groups: dict[tuple[str | None, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        group_backend = row["_backend"] if include_backend and row["_strategy_key"] != "baseline" else None
        groups[(group_backend, row["_strategy_key"])].append(row)
    for (group_backend, strategy_key), samples in groups.items():
        baseline_samples = [row for row in rows if row["_strategy_key"] == "baseline"]
        for metric in METRICS:
            values = [float(row[metric]) for row in samples if row.get(metric, "") != ""]
            if not values:
                continue
            baseline_values = [float(row[metric]) for row in baseline_samples if row.get(metric, "") != ""]
            improvements = paired_improvements(samples, metric, baselines, samples[0]["_layout"]) if metric in IMPROVEMENT_METRICS else []
            result: dict[str, object] = {"strategy_key": strategy_key, "metric": metric, **stats(values), "baseline_mean": sum(baseline_values) / len(baseline_values) if baseline_values else "", "improvement_percent": sum(improvements) / len(improvements) if improvements else ""}
            if include_backend:
                result["backend"] = "" if strategy_key == "baseline" else group_backend
            output.append(result)
            if improvements:
                output.append({**result, "metric": metric.removesuffix("_latency_us") + "_improvement_percent", **stats(improvements), "improvement_percent": sum(improvements) / len(improvements)})
    return output


def layout_comparison(rows: list[dict[str, str]], baselines: dict[tuple[str, str, str, str, str], dict[str, str]]) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    baseline_rows = [row for row in rows if row["_strategy_key"] == "baseline"]
    originals = [row for row in baseline_rows if row["_layout"] == "original"]
    for layout in dict.fromkeys(row["_layout"] for row in baseline_rows):
        samples = [row for row in baseline_rows if row["_layout"] == layout]
        for metric in METRICS:
            values = [float(row[metric]) for row in samples if row.get(metric, "") != ""]
            if not values:
                continue
            original_values = [float(row[metric]) for row in originals if row.get(metric, "") != ""]
            improvements = paired_improvements(samples, metric, baselines, "original") if metric in IMPROVEMENT_METRICS else []
            output.append({"layout": layout, "metric": metric, **stats(values), "original_baseline_mean": sum(original_values) / len(original_values) if original_values else "", "improvement_percent": sum(improvements) / len(improvements) if improvements else ""})
    return output


def configured_strategy_keys(config: dict[str, object]) -> list[str]:
    keys = ["baseline"]
    configured = {item["name"]: item for item in config["prefetch"]["strategies"]}
    for name in config["execution"]["strategy_order"]:
        if name == "baseline": continue
        item = configured[name]
        if name == "range_interior": keys.append(name)
        elif name == "offset_topk_interior":
            sweep = item["n"]
            values = sweep.get("values")
            if values is None:
                spec = sweep["range"]; values = range(spec["start"], spec["end_exclusive"], spec["step"])
            keys.extend(f"offset_topk_interior_n{value}" for value in values)
        else:
            for value in item["variants"]:
                label = str(value.get("label", f"interior{value['interior_k']}_leaf{value['leaf_k']}")).lower()
                safe = re.sub(r"[^a-z0-9_-]+", "-", label).strip("-_") or "variant"
                keys.append(f"residency_topk_{safe}_i{value['interior_k']}_l{value['leaf_k']}")
    return keys


def memory_comparison(rows: list[dict[str, str]], reference: str) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    lookup = {(row["_memory_condition"], row["_backend"], row["_strategy_key"], row["measurement_file"], row["repetition"]): row for row in rows}
    groups: dict[tuple[str, str | None, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows: groups[(row["_memory_condition"], row["_backend"], row["_strategy_key"])].append(row)
    for (condition, backend, strategy_key), samples in groups.items():
        for metric in METRICS:
            pairs = []
            for sample in samples:
                ref = lookup.get((reference, backend, strategy_key, sample["measurement_file"], sample["repetition"]))
                if ref and sample.get(metric, "") != "" and ref.get(metric, "") != "": pairs.append((float(sample[metric]), float(ref[metric])))
            if not pairs: continue
            values, references = zip(*pairs)
            changes = [(value - ref) / ref * 100 for value, ref in pairs if ref != 0]
            output.append({"memory_condition": condition, "backend": backend or "", "strategy_key": strategy_key, "metric": metric, **stats(list(values)), "reference_mean": sum(references) / len(references), "change_percent": sum(changes) / len(changes) if changes else ""})
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment-dir", required=True, type=Path)
    args = parser.parse_args()
    results_root = args.experiment_dir / "results"
    with (args.experiment_dir / "config.json").open(encoding="utf-8") as handle: config = json.load(handle)
    with (args.experiment_dir / "manifest.json").open(encoding="utf-8") as handle: manifest = json.load(handle)
    raw_files = [*results_root.glob("*/*/memory_conditions/*/baseline/raw.csv"), *results_root.glob("*/*/memory_conditions/*/backends/*/*/raw.csv")]
    if not raw_files:
        parser.error(f"no classified raw.csv files found under {results_root}")
    all_rows: list[dict[str, str]] = []
    source_rows: list[dict[str, str]] = []
    rows_by_raw: dict[Path, list[dict[str, str]]] = {}
    for path in raw_files:
        parts = path.relative_to(results_root).parts
        workload_type, layout, _, memory_condition = parts[:4]
        if parts[4] == "baseline":
            backend, strategy_key = None, "baseline"
        else:
            _, backend, strategy_key, _ = parts[4:]
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        for row in rows:
            row.update({"_workload_type": workload_type, "_layout": layout, "_memory_condition": memory_condition, "_backend": backend, "_strategy_key": strategy_key})
            add_derived_metrics(row)
            source_rows.append(row)
        rows_by_raw[path] = [row for row in rows if row["status"] == "completed"]
        all_rows.extend(rows_by_raw[path])

    layout_order = {value: index for index, value in enumerate(config["execution"]["layout_order"])}
    workload_order = {value: index for index, value in enumerate(config["workloads"]["types"])}
    memory_order = {value["name"]: index for index, value in enumerate(config["memory_conditions"])}
    backend_order = {value: index for index, value in enumerate(config["prefetch"]["backends"])}
    strategy_order = {value: index for index, value in enumerate(configured_strategy_keys(config))}
    measurement_order = {workload: {item["name"]: index for index, item in enumerate(phases["measurement"])} for workload, phases in manifest["workloads"].items()}
    def canonical_order(row: dict[str, str]) -> tuple[int, ...]:
        baseline = row["_strategy_key"] == "baseline"
        return (0 if baseline else 1, layout_order[row["_layout"]], workload_order[row["_workload_type"]], memory_order[row["_memory_condition"]], 0 if baseline else strategy_order[row["_strategy_key"]], 0 if baseline else backend_order[row["_backend"]], measurement_order[row["_workload_type"]][row["measurement_file"]], int(row["repetition"]))
    source_rows.sort(key=canonical_order)
    memory_by_name = {value["name"]: value for value in manifest["memory_conditions"]}
    combined_raw_rows = []
    for row in source_rows:
        cell_path = args.experiment_dir / "cells" / row["cell_id"] / "cell.json"
        cell = json.loads(cell_path.read_text(encoding="utf-8"))
        condition = memory_by_name[row["_memory_condition"]]; variant = cell["strategy"]
        combined_raw_rows.append({"experiment_id": config["experiment"]["id"], "cell_id": row["cell_id"], "status": row["status"], "workload_type": row["_workload_type"], "layout": row["_layout"], "memory_condition": row["_memory_condition"], "memory_limit_enabled": str(condition["enabled"]).lower(), "memory_max_bytes": condition["memory_max_bytes"] if condition["memory_max_bytes"] is not None else "", "strategy_key": row["_strategy_key"], "backend": row["_backend"] or "", "n": variant["n"] if variant["n"] is not None else "", "interior_k": variant["interior_k"] if variant["interior_k"] is not None else "", "leaf_k": variant["leaf_k"] if variant["leaf_k"] is not None else "", **{field: row.get(field, "") for field in RAW_FIELDS if field not in {"cell_id", "status"}}})
    write_csv(results_root / "all_raw.csv", ALL_RAW_FIELDS, combined_raw_rows)

    baselines = {(row["_layout"], row["_workload_type"], row["_memory_condition"], row["measurement_file"], row["repetition"]): row for row in all_rows if row["_strategy_key"] == "baseline"}
    for raw_path, samples in rows_by_raw.items():
        write_csv(raw_path.with_name("summary.csv"), FIELDS, strategy_summary(samples, baselines) if samples else [])
    workload_layout_memory = sorted({(row["_workload_type"], row["_layout"], row["_memory_condition"]) for row in source_rows}, key=lambda x: (workload_order[x[0]], layout_order[x[1]], memory_order[x[2]]))
    for workload_type, layout, memory_condition in workload_layout_memory:
        layout_samples = [row for row in all_rows if row["_workload_type"] == workload_type and row["_layout"] == layout and row["_memory_condition"] == memory_condition]
        baseline_samples = [row for row in layout_samples if row["_strategy_key"] == "baseline"]
        condition_root = results_root / workload_type / layout / "memory_conditions" / memory_condition
        for backend in config["prefetch"]["backends"]:
            samples = [*baseline_samples, *[row for row in layout_samples if row["_backend"] == backend]]
            write_csv(condition_root / "backends" / backend / "strategy_comparison.csv", STRATEGY_COMPARISON_FIELDS, strategy_comparison(samples, baselines))
        write_csv(condition_root / "backend_comparison.csv", BACKEND_COMPARISON_FIELDS, strategy_comparison(layout_samples, baselines, include_backend=True))
    for workload_type in config["workloads"]["types"]:
        for condition in config["memory_conditions"]:
            samples = [row for row in all_rows if row["_workload_type"] == workload_type and row["_memory_condition"] == condition["name"]]
            write_csv(results_root / workload_type / "layout_comparisons" / f"{condition['name']}.csv", LAYOUT_COMPARISON_FIELDS, layout_comparison(samples, baselines))
        for layout in config["execution"]["layout_order"]:
            samples = [row for row in all_rows if row["_workload_type"] == workload_type and row["_layout"] == layout]
            write_csv(results_root / workload_type / layout / "memory_comparison.csv", MEMORY_COMPARISON_FIELDS, memory_comparison(samples, config["memory_conditions"][0]["name"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
