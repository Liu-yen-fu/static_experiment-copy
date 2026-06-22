#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

STATUSES = ("completed", "failed", "timeout", "invalid")
DIST_METRICS = ("prefetch_elapsed_us", "first_query_latency_us", "effective_first_query_latency_us", "average_latency_us", "major_page_faults", "minor_page_faults", "requested_selected_resident_ratio", "successful_selected_resident_ratio")


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def md(value: Any) -> str:
    if value is None or value == "":
        return "N/A"
    return str(value).replace("|", "\\|").replace("\n", " ")


def number(value: Any, digits: int = 2, suffix: str = "") -> str:
    if value is None or value == "":
        return "N/A"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return md(value)
    return f"{numeric:.{digits}f}{suffix}"


def interval(row: dict[str, str] | None, suffix: str = "") -> str:
    if not row:
        return "N/A"
    return f"{number(row.get('p25'), suffix=suffix)}–{number(row.get('p75'), suffix=suffix)}"


def link(label: str, path: Path, root: Path) -> str:
    relative = Path(os.path.relpath(path, root)).as_posix()
    return f"[{md(label)}](<{relative}>)"


def table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    output.extend("| " + " | ".join(md(value) for value in row) + " |" for row in rows)
    return output


def metric_map(rows: list[dict[str, str]], key: str = "metric") -> dict[str, dict[str, str]]:
    return {row[key]: row for row in rows}


def workload_index(name: str) -> str:
    match = re.search(r"_(\d{3})\.txt$", name)
    return match.group(1) if match else "N/A"


def storage_summary(raw: Any) -> str:
    if not raw:
        return "N/A"
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        devices = []
        for device in value.get("blockdevices", []):
            if device.get("type") not in {"loop", "rom"}:
                devices.append(f"{device.get('name', 'N/A')} ({device.get('size', 'N/A')}, {device.get('model') or 'N/A'})")
        return ", ".join(devices) or "N/A"
    except (TypeError, ValueError, json.JSONDecodeError):
        return md(raw)


def current_cells(root: Path) -> tuple[list[dict[str, str]], dict[str, dict[str, Any]]]:
    raw_rows: list[dict[str, str]] = []
    metadata: dict[str, dict[str, Any]] = {}
    results = root / "results"
    raw_files = sorted([*results.glob("*/*/memory_conditions/*/baseline/raw.csv"), *results.glob("*/*/memory_conditions/*/backends/*/*/raw.csv")])
    for raw_path in raw_files:
        parts = raw_path.relative_to(results).parts
        workload_type, layout, _, memory_condition = parts[:4]
        backend, strategy_key = (None, "baseline") if parts[4] == "baseline" else (parts[5], parts[6])
        for row in read_csv(raw_path):
            row.update({"workload_type": workload_type, "layout": layout, "memory_condition": memory_condition, "backend": backend, "strategy_key": strategy_key})
            raw_rows.append(row)
            cell_path = root / "cells" / row["cell_id"] / "cell.json"
            if cell_path.is_file():
                metadata[row["cell_id"]] = read_json(cell_path)
    return raw_rows, metadata


def append_strategy_section(lines: list[str], heading: str, comparison: list[dict[str, str]]) -> None:
    grouped: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    for row in comparison:
        grouped[row["strategy_key"]][row["metric"]] = row
    lines += ["", heading, ""]
    strategy_rows = []
    for strategy_key, metrics in grouped.items():
        baseline = strategy_key == "baseline"
        prefetch, first, effective, average = metrics.get("prefetch_elapsed_us"), metrics.get("first_query_latency_us"), metrics.get("effective_first_query_latency_us"), metrics.get("average_latency_us")
        requested, successful = metrics.get("requested_selected_resident_ratio"), metrics.get("successful_selected_resident_ratio")
        major, minor = metrics.get("major_page_faults"), metrics.get("minor_page_faults")
        strategy_rows.append([strategy_key, "—" if baseline else number(prefetch.get("median") if prefetch else None, suffix=" µs"), number(first.get("median") if first else None, suffix=" µs"), number(first.get("improvement_percent") if first else None, suffix="%"), number(effective.get("median") if effective else None, suffix=" µs"), number(effective.get("improvement_percent") if effective else None, suffix="%"), number(average.get("median") if average else None, suffix=" µs"), number(average.get("improvement_percent") if average else None, suffix="%"), "—" if baseline else number(requested.get("median") if requested else None, 4), "—" if baseline else number(successful.get("median") if successful else None, 4), number(major.get("median") if major else None), number(minor.get("median") if minor else None)])
    lines += ["`Effective first-query latency = prefetch elapsed + first-query latency`；其改善率使用相同measurement file與repetition的baseline first-query latency配對計算。", ""]
    lines += table(["Strategy key", "Prefetch median", "First-query median", "First-query改善", "Effective first-query median", "Effective first-query改善", "Average-query median", "Average-query改善", "Requested resident ratio", "Successful resident ratio", "Major faults", "Minor faults"], strategy_rows)
    lines += ["", "Distribution 詳細：", ""]
    distribution = []
    for strategy_key, metrics in grouped.items():
        for metric in DIST_METRICS:
            row = metrics.get(metric)
            if row:
                suffix = " µs" if metric in {"prefetch_elapsed_us", "first_query_latency_us", "effective_first_query_latency_us", "average_latency_us"} else ""
                distribution.append([strategy_key, metric, number(row.get("p25"), suffix=suffix), number(row.get("median"), suffix=suffix), number(row.get("p75"), suffix=suffix), number(row.get("p99"), suffix=suffix)])
    lines += table(["Strategy key", "Metric", "P25", "Median", "P75", "P99"], distribution)


def generate(root: Path) -> str:
    config = read_json(root / "config.json")
    manifest = read_json(root / "manifest.json")
    raw_rows, cells = current_cells(root)
    counts = Counter(row.get("status", "invalid") for row in raw_rows)
    environment = manifest.get("environment", {})
    experiment = config["experiment"]
    workloads = config["workloads"]
    memory_conditions = config["memory_conditions"]
    page_sizes = environment.get("sqlite_page_sizes", {})
    lines = [f"# 實驗報告：{md(experiment['id'])}", "", "## 實驗摘要", ""]
    lines += table(["項目", "值"], [
        ["Experiment ID", experiment["id"]], ["Prefetch backends", " → ".join(config["prefetch"]["backends"])],
        ["Enabled layouts", ", ".join(config["execution"]["layout_order"])], ["Workload types", ", ".join(workloads["types"])],
        ["Training file count", workloads["training"]["count"]], ["Measurement file count", workloads["measurement"]["count"]],
        ["Measurement repetitions", workloads["measurement"]["repetitions"]],
        ["Memory conditions", " → ".join(f"{item['name']} ({'enabled, MemoryMax=' + item['memory_max'] if item['enabled'] else 'unlimited'})" for item in memory_conditions)],
        ["SQLite page size", ", ".join(f"{name}={size}" for name, size in page_sizes.items()) or "N/A"],
        *[[status.capitalize(), counts.get(status, 0)] for status in STATUSES],
    ])
    lines += ["", "## 執行環境", ""]
    lines += table(["項目", "值"], [
        ["Linux kernel", environment.get("linux_kernel_version")], ["Hostname", environment.get("hostname")],
        ["CPU model", environment.get("cpu_model")], ["Logical CPU count", environment.get("logical_cpu_count")],
        ["Total RAM", number(environment.get("total_ram_bytes", 0) / 1073741824 if environment.get("total_ram_bytes") else None, 2, " GiB")],
        ["Filesystem type", environment.get("filesystem_type")], ["Storage devices", storage_summary(environment.get("storage_device_info"))],
        ["SQLite version", environment.get("sqlite_version")],
    ])
    lines += ["", "## 各 workload type 結果", ""]
    results_root = root / "results"
    for workload_type in workloads["types"]:
        workload_root = results_root / workload_type
        lines += [f"### {md(workload_type)}", ""]
        for condition in memory_conditions:
            memory_name = condition["name"]
            lines += [f"#### Memory condition：{md(memory_name)}", "", "##### Layout 比較", ""]
            layout_path = workload_root / "layout_comparisons" / f"{memory_name}.csv"
            layout_rows = read_csv(layout_path) if layout_path.is_file() else []
            by_layout: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
            for row in layout_rows: by_layout[row["layout"]][row["metric"]] = row
            layout_table = []
            for layout in config["execution"]["layout_order"]:
                metrics = by_layout.get(layout, {}); first, average = metrics.get("first_query_latency_us"), metrics.get("average_latency_us")
                layout_table.append([layout, number(first.get("median") if first else None, suffix=" µs"), interval(first, " µs"), number(first.get("p99") if first else None, suffix=" µs"), number(first.get("improvement_percent") if first else None, suffix="%"), number(average.get("median") if average else None, suffix=" µs"), number(average.get("improvement_percent") if average else None, suffix="%")])
            lines += table(["Layout", "First-query median", "First-query P25–P75", "First-query P99", "改善 vs original", "Average-query median", "Average-query改善 vs original"], layout_table)
            for layout in config["execution"]["layout_order"]:
                condition_root = workload_root / layout / "memory_conditions" / memory_name
                for backend in config["prefetch"]["backends"]:
                    comparison_path = condition_root / "backends" / backend / "strategy_comparison.csv"
                    comparison = read_csv(comparison_path) if comparison_path.is_file() else []
                    append_strategy_section(lines, f"##### {md(layout)} / {md(memory_name)} / {md(backend)}：Strategy 比較", comparison)
                backend_path = condition_root / "backend_comparison.csv"; backend_rows = read_csv(backend_path) if backend_path.is_file() else []
                grouped_backend: dict[tuple[str, str], dict[str, dict[str, str]]] = defaultdict(dict)
                for row in backend_rows: grouped_backend[(row.get("backend", ""), row["strategy_key"])][row["metric"]] = row
                lines += ["", f"##### {md(layout)} / {md(memory_name)}：Backend 比較", "", "`madvise` prefetch cost為非同步request submission時間；`pread` prefetch cost為同步read完成時間。", ""]
                backend_table = []
                for (backend, strategy_key), metrics in grouped_backend.items():
                    prefetch, first, effective, average = metrics.get("prefetch_elapsed_us"), metrics.get("first_query_latency_us"), metrics.get("effective_first_query_latency_us"), metrics.get("average_latency_us"); requested, successful = metrics.get("requested_selected_resident_ratio"), metrics.get("successful_selected_resident_ratio")
                    backend_table.append([backend or "—", strategy_key, "—" if strategy_key == "baseline" else number(prefetch.get("median") if prefetch else None, suffix=" µs"), "—" if strategy_key == "baseline" else interval(prefetch, " µs"), number(first.get("median") if first else None, suffix=" µs"), number(first.get("improvement_percent") if first else None, suffix="%"), number(effective.get("median") if effective else None, suffix=" µs"), number(effective.get("improvement_percent") if effective else None, suffix="%"), number(average.get("median") if average else None, suffix=" µs"), number(average.get("improvement_percent") if average else None, suffix="%"), "—" if strategy_key == "baseline" else number(requested.get("median") if requested else None, 4), "—" if strategy_key == "baseline" else number(successful.get("median") if successful else None, 4)])
                lines += table(["Backend", "Strategy key", "Prefetch median", "Prefetch P25–P75", "First-query median", "First-query改善", "Effective first-query median", "Effective first-query改善", "Average-query median", "Average-query改善", "Requested resident ratio", "Successful resident ratio"], backend_table)
        if len(memory_conditions) > 1:
            reference = memory_conditions[0]["name"]
            lines += ["", f"#### Memory condition 比較（reference：{md(reference)}）", ""]
            for layout in config["execution"]["layout_order"]:
                comparison_path = workload_root / layout / "memory_comparison.csv"; comparison = read_csv(comparison_path) if comparison_path.is_file() else []
                grouped_memory: dict[tuple[str, str, str], dict[str, dict[str, str]]] = defaultdict(dict)
                for row in comparison: grouped_memory[(row["memory_condition"], row.get("backend", ""), row["strategy_key"])][row["metric"]] = row
                memory_rows = []
                for (condition, backend, strategy_key), metrics in grouped_memory.items():
                    prefetch, first, effective, average = metrics.get("prefetch_elapsed_us"), metrics.get("first_query_latency_us"), metrics.get("effective_first_query_latency_us"), metrics.get("average_latency_us")
                    memory_rows.append([condition, backend or "—", strategy_key, number(prefetch.get("median") if prefetch else None, suffix=" µs"), number(first.get("median") if first else None, suffix=" µs"), number(first.get("change_percent") if first else None, suffix="%"), number(effective.get("median") if effective else None, suffix=" µs"), number(effective.get("change_percent") if effective else None, suffix="%"), number(average.get("median") if average else None, suffix=" µs"), number(average.get("change_percent") if average else None, suffix="%")])
                lines += [f"##### {md(layout)}", ""] + table(["Memory condition", "Backend", "Strategy key", "Prefetch median", "First-query median", "First-query change", "Effective first-query median", "Effective first-query change", "Average-query median", "Average-query change"], memory_rows)
    lines += ["", "## Prefetch cost 與 first-query improvement trade-off", ""]
    points_path = root / "plots" / "tradeoff_points.csv"
    point_rows = read_csv(points_path) if points_path.is_file() else []
    for backend in config["prefetch"]["backends"]:
        backend_points = [row for row in point_rows if row["backend"] == backend]
        lines += [f"### {md(backend)}", "", f"![{md(backend)} prefetch trade-off](plots/tradeoff_{backend}.png)", ""]
        lines += table(["Workload type", "Layout", "Memory condition", "Strategy key", "Prefetch median（P25–P75）", "First-query improvement median（P25–P75）"], [[row["workload_type"], row["layout"], row["memory_condition"], row["strategy_key"], f"{number(row['prefetch_median_us'])} µs（{number(row['prefetch_p25_us'])}–{number(row['prefetch_p75_us'])}）", f"{number(row['first_query_improvement_median'])}%（{number(row['first_query_improvement_p25'])}–{number(row['first_query_improvement_p75'])}）"] for row in backend_points])
    lines += ["", "## Cell 狀態", ""]
    lines += table(["Status", "數量"], [[status, counts.get(status, 0)] for status in STATUSES])
    failures = [row for row in raw_rows if row.get("status") in {"failed", "timeout", "invalid"}]
    if failures:
        lines += ["", "### 未完成 cells", ""]
        failure_rows = []
        for row in failures:
            err = root / "logs" / f"{row['cell_id']}.err"
            failure_rows.append([row["cell_id"], row["measurement_file"], row["layout"], row["memory_condition"], row["backend"] or "—", row["strategy_key"], link("stderr", err, root) if err.is_file() else "N/A"])
        lines += table(["Cell ID", "Workload", "Layout", "Memory condition", "Backend", "Strategy", "Log"], failure_rows)
    lines += ["", "## Training 與 measurement workload 清單", ""]
    repetitions = workloads["measurement"]["repetitions"]
    for workload_type, phases in manifest.get("workloads", {}).items():
        lines += [f"### {md(workload_type)}", ""]
        workload_rows = []
        for phase in ("training", "measurement"):
            for order, item in enumerate(phases.get(phase, []), 1):
                workload_rows.append([phase, order, workload_index(item["name"]), item["name"], item.get("sha256", "N/A"), repetitions if phase == "measurement" else 1])
        lines += table(["用途", "抽樣順序", "Index", "檔名", "SHA-256", "Repetitions"], workload_rows)
    lines += ["", "## Artifacts 連結", "", f"- {link('Experiment config', root / 'config.json', root)}", f"- {link('Experiment manifest', root / 'manifest.json', root)}"]
    lines.append(f"- {link('All raw results', results_root / 'all_raw.csv', root)}")
    for workload_type in workloads["types"]:
        workload_root = results_root / workload_type
        for condition in memory_conditions:
            memory_name = condition["name"]
            lines.append(f"- {link(f'{workload_type}/{memory_name} layout comparison', workload_root / 'layout_comparisons' / f'{memory_name}.csv', root)}")
            for layout in config["execution"]["layout_order"]:
                condition_root = workload_root / layout / "memory_conditions" / memory_name
                for backend in config["prefetch"]["backends"]:
                    lines.append(f"- {link(f'{workload_type}/{layout}/{memory_name}/{backend} strategy comparison', condition_root / 'backends' / backend / 'strategy_comparison.csv', root)}")
                lines.append(f"- {link(f'{workload_type}/{layout}/{memory_name} backend comparison', condition_root / 'backend_comparison.csv', root)}")
        for layout in config["execution"]["layout_order"]:
            lines.append(f"- {link(f'{workload_type}/{layout} memory comparison', workload_root / layout / 'memory_comparison.csv', root)}")
    lines.append(f"- {link('Trade-off data', points_path, root)}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment-dir", required=True, type=Path)
    args = parser.parse_args()
    root = args.experiment_dir.resolve()
    for required in ("config.json", "manifest.json", "results", "plots/tradeoff_points.csv"):
        if not (root / required).exists():
            parser.error(f"required report input is missing: {root / required}")
    config = read_json(root / "config.json")
    for backend in config["prefetch"]["backends"]:
        required = root / "plots" / f"tradeoff_{backend}.png"
        if not required.is_file():
            parser.error(f"required report input is missing: {required}")
    output = root / "report.md"
    temporary = root / f".report.md.{os.getpid()}.tmp"
    temporary.write_text(generate(root), encoding="utf-8", newline="\n")
    os.replace(temporary, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
