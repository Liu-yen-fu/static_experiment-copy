#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import math
import random
from collections import defaultdict
from pathlib import Path
from typing import Any

METRICS = (
    "effective_first_query_latency_us",
    "effective_average_query_latency_us",
)

FIELDS = [
    "comparison_type",
    "workload_type",
    "memory_condition",
    "layout",
    "backend",
    "strategy_key",
    "metric",
    "reference",
    "candidate",
    "pair_count",
    "reference_median",
    "candidate_median",
    "median_difference_us",
    "mean_difference_us",
    "median_improvement_percent",
    "mean_improvement_percent",
    "ci95_difference_low",
    "ci95_difference_high",
    "wins",
    "losses",
    "ties",
    "win_rate",
    "p_value",
    "q_value",
    "direction",
    "significant",
    "significant_better",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in FIELDS})


def median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * pct
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def bootstrap_ci(values: list[float], iterations: int, seed: int) -> tuple[float, float]:
    rng = random.Random(seed)
    n = len(values)
    medians = []
    for _ in range(iterations):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        medians.append(median(sample))
    return percentile(medians, 0.025), percentile(medians, 0.975)


def sign_test_p_value(wins: int, losses: int) -> float:
    n = wins + losses
    if n == 0:
        return 1.0
    observed = min(wins, losses)
    tail = sum(math.comb(n, k) for k in range(observed + 1)) / (2**n)
    return min(1.0, 2 * tail)


def add_bh_q_values(rows: list[dict[str, Any]]) -> None:
    indexed = sorted(
        [(index, float(row["p_value"])) for index, row in enumerate(rows)],
        key=lambda item: item[1],
    )
    previous = 1.0
    m = len(indexed)
    for rank, (index, p_value) in reversed(list(enumerate(indexed, 1))):
        q_value = min(previous, p_value * m / rank)
        rows[index]["q_value"] = q_value
        previous = q_value


def stable_seed(*parts: Any) -> int:
    text = "\0".join(str(part) for part in parts)
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)


def number(value: Any, digits: int = 4) -> str:
    if value == "" or value is None:
        return ""
    return f"{float(value):.{digits}f}"


def percent(value: Any, digits: int = 2) -> str:
    if value == "" or value is None:
        return ""
    return f"{float(value):.{digits}f}%"


def usable(row: dict[str, str], metric: str) -> bool:
    return row.get("status") == "completed" and row.get(metric, "") != ""


def pair_key(row: dict[str, str]) -> tuple[str, str]:
    return row.get("measurement_file", ""), row.get("repetition", "")


def is_baseline(row: dict[str, str]) -> bool:
    return row.get("strategy_key") == "baseline"


def is_original_baseline(row: dict[str, str]) -> bool:
    return row.get("layout") == "original" and is_baseline(row)


def comparison_row(
    comparison_type: str,
    context: dict[str, str],
    metric: str,
    reference_label: str,
    candidate_label: str,
    pairs: list[tuple[dict[str, str], dict[str, str]]],
    bootstrap_iterations: int,
    seed: int,
) -> dict[str, Any] | None:
    usable_pairs = [
        (reference, candidate)
        for reference, candidate in pairs
        if usable(reference, metric) and usable(candidate, metric)
    ]
    if not usable_pairs:
        return None
    reference_values = [float(reference[metric]) for reference, _ in usable_pairs]
    candidate_values = [float(candidate[metric]) for _, candidate in usable_pairs]
    differences = [candidate - reference for reference, candidate in zip(reference_values, candidate_values)]
    improvements = [
        (reference - candidate) / reference * 100
        for reference, candidate in zip(reference_values, candidate_values)
        if reference != 0
    ]
    wins = sum(1 for value in differences if value < 0)
    losses = sum(1 for value in differences if value > 0)
    ties = sum(1 for value in differences if value == 0)
    ci_low, ci_high = bootstrap_ci(differences, bootstrap_iterations, seed)
    median_difference = median(differences)
    direction = "better" if median_difference < 0 else "worse" if median_difference > 0 else "tie"
    return {
        "comparison_type": comparison_type,
        **context,
        "metric": metric,
        "reference": reference_label,
        "candidate": candidate_label,
        "pair_count": len(usable_pairs),
        "reference_median": median(reference_values),
        "candidate_median": median(candidate_values),
        "median_difference_us": median_difference,
        "mean_difference_us": sum(differences) / len(differences),
        "median_improvement_percent": median(improvements) if improvements else "",
        "mean_improvement_percent": sum(improvements) / len(improvements) if improvements else "",
        "ci95_difference_low": ci_low,
        "ci95_difference_high": ci_high,
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "win_rate": wins / (wins + losses) if wins + losses else "",
        "p_value": sign_test_p_value(wins, losses),
        "q_value": 1.0,
        "direction": direction,
        "significant": "no",
        "significant_better": "no",
    }


def annotate_significance(rows: list[dict[str, Any]], alpha: float) -> None:
    add_bh_q_values(rows)
    for row in rows:
        ci_low = float(row["ci95_difference_low"])
        ci_high = float(row["ci95_difference_high"])
        significant = float(row["q_value"]) <= alpha and not (ci_low <= 0 <= ci_high)
        row["significant"] = "yes" if significant else "no"
        row["significant_better"] = "yes" if significant and row["direction"] == "better" else "no"


def layout_only_comparisons(rows: list[dict[str, str]], metrics: tuple[str, ...], min_pairs: int, bootstrap_iterations: int) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    references: dict[tuple[str, str, tuple[str, str]], dict[str, str]] = {}
    candidates: dict[tuple[str, str, str], dict[tuple[str, str], dict[str, str]]] = defaultdict(dict)
    for row in rows:
        if not is_baseline(row):
            continue
        workload_type = row.get("workload_type", "")
        memory_condition = row.get("memory_condition", "")
        layout = row.get("layout", "")
        if layout == "original":
            references[(workload_type, memory_condition, pair_key(row))] = row
        else:
            candidates[(workload_type, memory_condition, layout)][pair_key(row)] = row
    for key, by_pair in sorted(candidates.items()):
        workload_type, memory_condition, layout = key
        common = sorted(
            pair for pair in by_pair
            if (workload_type, memory_condition, pair) in references
        )
        if len(common) < min_pairs:
            continue
        pairs = [
            (references[(workload_type, memory_condition, pair)], by_pair[pair])
            for pair in common
        ]
        context = {
            "workload_type": workload_type,
            "memory_condition": memory_condition,
            "layout": layout,
            "backend": "",
            "strategy_key": "baseline",
        }
        for metric in metrics:
            row = comparison_row(
                "layout_only_vs_original_baseline",
                context,
                metric,
                "original/no-prefetch baseline",
                f"{layout}/no-prefetch baseline",
                pairs,
                bootstrap_iterations,
                stable_seed("layout-only", key, metric),
            )
            if row:
                output.append(row)
    return output


def same_layout_prefetch_comparisons(rows: list[dict[str, str]], metrics: tuple[str, ...], min_pairs: int, bootstrap_iterations: int) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    references: dict[tuple[str, str, str, tuple[str, str]], dict[str, str]] = {}
    candidates: dict[tuple[str, str, str, str, str], dict[tuple[str, str], dict[str, str]]] = defaultdict(dict)
    for row in rows:
        workload_type = row.get("workload_type", "")
        memory_condition = row.get("memory_condition", "")
        layout = row.get("layout", "")
        if is_baseline(row):
            references[(workload_type, memory_condition, layout, pair_key(row))] = row
        else:
            key = (
                workload_type,
                memory_condition,
                layout,
                row.get("backend", ""),
                row.get("strategy_key", ""),
            )
            candidates[key][pair_key(row)] = row
    for key, by_pair in sorted(candidates.items()):
        workload_type, memory_condition, layout, backend, strategy_key = key
        common = sorted(
            pair for pair in by_pair
            if (workload_type, memory_condition, layout, pair) in references
        )
        if len(common) < min_pairs:
            continue
        pairs = [
            (references[(workload_type, memory_condition, layout, pair)], by_pair[pair])
            for pair in common
        ]
        context = {
            "workload_type": workload_type,
            "memory_condition": memory_condition,
            "layout": layout,
            "backend": backend,
            "strategy_key": strategy_key,
        }
        for metric in metrics:
            row = comparison_row(
                "same_layout_prefetch_vs_baseline",
                context,
                metric,
                f"{layout}/no-prefetch baseline",
                f"{layout}/{backend}/{strategy_key}",
                pairs,
                bootstrap_iterations,
                stable_seed("same-layout-prefetch", key, metric),
            )
            if row:
                output.append(row)
    return output


def strategy_family(strategy_key: str) -> str:
    if strategy_key == "range_interior":
        return "range_interior"
    if strategy_key.startswith("offset_topk_interior"):
        return "offset_topk_interior"
    if strategy_key.startswith("residency_topk"):
        return "residency_topk"
    return strategy_key


def count_by(rows: list[dict[str, Any]], *fields: str) -> list[tuple[tuple[str, ...], int]]:
    counts: dict[tuple[str, ...], int] = defaultdict(int)
    for row in rows:
        counts[tuple(str(row.get(field, "")) for field in fields)] += 1
    return sorted(counts.items(), key=lambda item: (item[0], item[1]))


def markdown_table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    if not rows:
        return ["_No rows._", ""]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(value).replace("|", "\\|") for value in row) + " |")
    lines.append("")
    return lines


def write_markdown(path: Path, layout_rows: list[dict[str, Any]], prefetch_rows: list[dict[str, Any]], alpha: float) -> None:
    significant_layout = [row for row in layout_rows if row["significant"] == "yes"]
    better_layout = [row for row in layout_rows if row["significant_better"] == "yes"]
    significant_prefetch = [row for row in prefetch_rows if row["significant"] == "yes"]
    better_prefetch = [row for row in prefetch_rows if row["significant_better"] == "yes"]

    lines: list[str] = [
        "# Layout-only 與 same-layout prefetch 比較",
        "",
        "這份摘要使用既有 `all_raw.csv`，不需要重跑 benchmark。",
        "差值定義為 `candidate latency - reference latency`；負值代表 candidate 較快。",
        "改善率定義為 `(reference latency - candidate latency) / reference latency × 100`；正值代表 candidate 較快。",
        f"顯著門檻為 FDR q-value ≤ {alpha:g}，且 bootstrap 95% CI 不跨 0。",
        "",
        "## 比較類型",
        "",
        "- `layout-only`：比較 `vacuum` 或 `rewrite` 的 no-prefetch baseline，相對 `original` 的 no-prefetch baseline。",
        "- `same-layout prefetch`：比較同一個 layout 內，某個 prefetch candidate 相對該 layout 的 no-prefetch baseline。",
        "",
        "## 整體摘要",
        "",
    ]
    lines += markdown_table(
        ["Comparison", "Rows", "Significant rows", "Significant better rows"],
        [
            ["layout-only", len(layout_rows), len(significant_layout), len(better_layout)],
            ["same-layout prefetch", len(prefetch_rows), len(significant_prefetch), len(better_prefetch)],
        ],
    )

    lines += ["## Layout-only 顯著改善數量", ""]
    layout_counts = count_by(better_layout, "metric", "layout")
    lines += markdown_table(
        ["Metric", "Layout", "Significant better rows"],
        [[metric, layout, count] for (metric, layout), count in layout_counts],
    )

    lines += ["## Same-layout prefetch 顯著改善數量", ""]
    prefetch_counts: dict[tuple[str, str, str], int] = defaultdict(int)
    for row in better_prefetch:
        prefetch_counts[(row["metric"], row["backend"], strategy_family(row["strategy_key"]))] += 1
    lines += markdown_table(
        ["Metric", "Backend", "Strategy family", "Significant better rows"],
        [[metric, backend, family, count] for (metric, backend, family), count in sorted(prefetch_counts.items())],
    )

    lines += ["## Top layout-only improvements", ""]
    top_layout = sorted(
        better_layout,
        key=lambda row: float(row["median_improvement_percent"]),
        reverse=True,
    )[:20]
    lines += markdown_table(
        ["Workload", "Memory", "Metric", "Candidate", "Median improvement", "Median diff", "q"],
        [
            [
                row["workload_type"],
                row["memory_condition"],
                row["metric"],
                row["candidate"],
                percent(row["median_improvement_percent"]),
                number(row["median_difference_us"]),
                number(row["q_value"]),
            ]
            for row in top_layout
        ],
    )

    lines += ["## Top same-layout prefetch improvements", ""]
    top_prefetch = sorted(
        better_prefetch,
        key=lambda row: float(row["median_improvement_percent"]),
        reverse=True,
    )[:20]
    lines += markdown_table(
        ["Workload", "Memory", "Layout", "Backend", "Strategy", "Metric", "Median improvement", "Median diff", "q"],
        [
            [
                row["workload_type"],
                row["memory_condition"],
                row["layout"],
                row["backend"],
                row["strategy_key"],
                row["metric"],
                percent(row["median_improvement_percent"]),
                number(row["median_difference_us"]),
                number(row["q_value"]),
            ]
            for row in top_prefetch
        ],
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def format_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    formatted = []
    numeric_fields = {
        "reference_median",
        "candidate_median",
        "median_difference_us",
        "mean_difference_us",
        "median_improvement_percent",
        "mean_improvement_percent",
        "ci95_difference_low",
        "ci95_difference_high",
        "win_rate",
        "p_value",
        "q_value",
    }
    for row in rows:
        item = row.copy()
        for field in numeric_fields:
            if item.get(field, "") != "":
                item[field] = number(item[field], 6 if field in {"p_value", "q_value"} else 4)
        formatted.append(item)
    return formatted


def resolve_input(args: argparse.Namespace) -> Path:
    if args.input:
        return args.input
    root = args.experiment_dir
    candidates = [
        root / "results" / "all_raw.csv",
        root / "all_raw.csv",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"no all_raw.csv found under {root}")


def default_output_dir(input_path: Path, experiment_dir: Path | None) -> Path:
    if experiment_dir:
        return experiment_dir / "results" / "component_comparisons"
    if input_path.parent.name == "results":
        return input_path.parent / "component_comparisons"
    return input_path.parent / "results" / "component_comparisons"


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze layout-only and same-layout prefetch effects from all_raw.csv.")
    parser.add_argument("--experiment-dir", type=Path, help="Experiment directory containing all_raw.csv or results/all_raw.csv.")
    parser.add_argument("--input", type=Path, help="Path to all_raw.csv.")
    parser.add_argument("--output-dir", type=Path, help="Directory for component comparison outputs.")
    parser.add_argument("--alpha", type=float, default=0.05)
    parser.add_argument("--min-pairs", type=int, default=5)
    parser.add_argument("--bootstrap-iterations", type=int, default=1000)
    args = parser.parse_args()

    if not args.input and not args.experiment_dir:
        parser.error("one of --experiment-dir or --input is required")

    input_path = resolve_input(args)
    output_dir = args.output_dir or default_output_dir(input_path, args.experiment_dir)
    rows = read_csv(input_path)
    metrics = tuple(metric for metric in METRICS if any(row.get(metric, "") != "" for row in rows))
    if not metrics:
        parser.error(f"none of the expected metrics exist in {input_path}: {', '.join(METRICS)}")

    layout_rows = layout_only_comparisons(rows, metrics, args.min_pairs, args.bootstrap_iterations)
    prefetch_rows = same_layout_prefetch_comparisons(rows, metrics, args.min_pairs, args.bootstrap_iterations)
    annotate_significance(layout_rows + prefetch_rows, args.alpha)

    layout_rows.sort(key=lambda row: (row["workload_type"], row["memory_condition"], row["layout"], row["metric"]))
    prefetch_rows.sort(key=lambda row: (row["workload_type"], row["memory_condition"], row["layout"], row["backend"], row["strategy_key"], row["metric"]))

    write_csv(output_dir / "layout_only_comparison.csv", format_rows(layout_rows))
    write_csv(output_dir / "same_layout_prefetch_comparison.csv", format_rows(prefetch_rows))
    write_markdown(output_dir / "component_comparison_summary.md", layout_rows, prefetch_rows, args.alpha)

    print(f"input={input_path}")
    print(f"output_dir={output_dir}")
    print(f"layout_only_rows={len(layout_rows)}")
    print(f"same_layout_prefetch_rows={len(prefetch_rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
