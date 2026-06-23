#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
from collections import defaultdict
from pathlib import Path
from typing import Any

METRICS = (
    "effective_first_query_latency_us",
    "effective_average_query_latency_us",
    "average_latency_us",
    "major_page_faults",
)
FIELDS = [
    "comparison_type",
    "workload_type",
    "layout",
    "memory_condition",
    "backend",
    "strategy_key",
    "metric",
    "reference",
    "candidate",
    "pair_count",
    "median_difference",
    "mean_difference",
    "ci95_low",
    "ci95_high",
    "wins",
    "losses",
    "ties",
    "win_rate",
    "p_value",
    "q_value",
    "direction",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def number(value: Any, digits: int = 4) -> str:
    if value is None or value == "":
        return ""
    return f"{float(value):.{digits}f}"


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


def bh_q_values(rows: list[dict[str, Any]]) -> None:
    indexed = sorted([(index, float(row["p_value"])) for index, row in enumerate(rows)], key=lambda item: item[1])
    m = len(indexed)
    previous = 1.0
    for rank, (index, p_value) in reversed(list(enumerate(indexed, 1))):
        q_value = min(previous, p_value * m / rank)
        rows[index]["q_value"] = q_value
        previous = q_value


def usable(row: dict[str, str], metric: str) -> bool:
    return row.get("status") == "completed" and row.get(metric, "") != ""


def identity(row: dict[str, str], *fields: str) -> tuple[str, ...]:
    return tuple(row.get(field, "") for field in fields)


def stable_seed(*parts: Any) -> int:
    text = "\0".join(str(part) for part in parts)
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)


def paired_effect(
    comparison_type: str,
    context: dict[str, str],
    metric: str,
    reference_label: str,
    candidate_label: str,
    pairs: list[tuple[dict[str, str], dict[str, str]]],
    bootstrap_iterations: int,
    seed: int,
) -> dict[str, Any] | None:
    differences = [float(candidate[metric]) - float(reference[metric]) for reference, candidate in pairs if usable(reference, metric) and usable(candidate, metric)]
    if not differences:
        return None
    wins = sum(1 for value in differences if value < 0)
    losses = sum(1 for value in differences if value > 0)
    ties = sum(1 for value in differences if value == 0)
    ci_low, ci_high = bootstrap_ci(differences, bootstrap_iterations, seed)
    med = median(differences)
    mean = sum(differences) / len(differences)
    direction = "better" if med < 0 else "worse" if med > 0 else "tie"
    return {
        "comparison_type": comparison_type,
        **context,
        "metric": metric,
        "reference": reference_label,
        "candidate": candidate_label,
        "pair_count": len(differences),
        "median_difference": med,
        "mean_difference": mean,
        "ci95_low": ci_low,
        "ci95_high": ci_high,
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "win_rate": wins / (wins + losses) if wins + losses else "",
        "p_value": sign_test_p_value(wins, losses),
        "q_value": 1.0,
        "direction": direction,
    }


def strategy_vs_baseline(rows: list[dict[str, str]], metrics: tuple[str, ...], min_pairs: int, bootstrap_iterations: int) -> list[dict[str, Any]]:
    output = []
    baselines = {
        identity(row, "workload_type", "layout", "memory_condition", "measurement_file", "repetition"): row
        for row in rows
        if row.get("strategy_key") == "baseline"
    }
    groups: dict[tuple[str, str, str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if row.get("strategy_key") != "baseline":
            groups[identity(row, "workload_type", "layout", "memory_condition", "backend", "strategy_key")].append(row)
    for key, candidates in groups.items():
        workload_type, layout, memory_condition, backend, strategy_key = key
        pairs = []
        for candidate in candidates:
            reference = baselines.get((workload_type, layout, memory_condition, candidate["measurement_file"], candidate["repetition"]))
            if reference:
                pairs.append((reference, candidate))
        if len(pairs) < min_pairs:
            continue
        context = {"workload_type": workload_type, "layout": layout, "memory_condition": memory_condition, "backend": backend, "strategy_key": strategy_key}
        for metric in metrics:
            effect = paired_effect("strategy_vs_baseline", context, metric, "same-layout baseline", strategy_key, pairs, bootstrap_iterations, stable_seed("strategy", key, metric))
            if effect:
                output.append(effect)
    return output


def backend_comparisons(rows: list[dict[str, str]], backends: list[str], metrics: tuple[str, ...], min_pairs: int, bootstrap_iterations: int) -> list[dict[str, Any]]:
    output = []
    if len(backends) < 2:
        return output
    reference_backend = backends[0]
    groups: dict[tuple[str, str, str, str], dict[str, dict[tuple[str, str], dict[str, str]]]] = defaultdict(lambda: defaultdict(dict))
    for row in rows:
        if row.get("strategy_key") == "baseline" or row.get("backend", "") == "":
            continue
        key = identity(row, "workload_type", "layout", "memory_condition", "strategy_key")
        groups[key][row["backend"]][identity(row, "measurement_file", "repetition")] = row
    for key, by_backend in groups.items():
        if reference_backend not in by_backend:
            continue
        workload_type, layout, memory_condition, strategy_key = key
        for backend, candidates in by_backend.items():
            if backend == reference_backend:
                continue
            common = sorted(set(by_backend[reference_backend]) & set(candidates))
            pairs = [(by_backend[reference_backend][pair_key], candidates[pair_key]) for pair_key in common]
            if len(pairs) < min_pairs:
                continue
            context = {"workload_type": workload_type, "layout": layout, "memory_condition": memory_condition, "backend": backend, "strategy_key": strategy_key}
            for metric in metrics:
                effect = paired_effect("backend_vs_reference", context, metric, reference_backend, backend, pairs, bootstrap_iterations, stable_seed("backend", key, backend, metric))
                if effect:
                    output.append(effect)
    return output


def memory_comparisons(rows: list[dict[str, str]], memories: list[str], metrics: tuple[str, ...], min_pairs: int, bootstrap_iterations: int) -> list[dict[str, Any]]:
    output = []
    if len(memories) < 2:
        return output
    reference_memory = memories[0]
    groups: dict[tuple[str, str, str, str], dict[str, dict[tuple[str, str], dict[str, str]]]] = defaultdict(lambda: defaultdict(dict))
    for row in rows:
        backend = row.get("backend", "")
        strategy_key = row.get("strategy_key", "")
        key = identity(row, "workload_type", "layout", "backend", "strategy_key")
        groups[key][row["memory_condition"]][identity(row, "measurement_file", "repetition")] = row
    for key, by_memory in groups.items():
        if reference_memory not in by_memory:
            continue
        workload_type, layout, backend, strategy_key = key
        for memory, candidates in by_memory.items():
            if memory == reference_memory:
                continue
            common = sorted(set(by_memory[reference_memory]) & set(candidates))
            pairs = [(by_memory[reference_memory][pair_key], candidates[pair_key]) for pair_key in common]
            if len(pairs) < min_pairs:
                continue
            context = {"workload_type": workload_type, "layout": layout, "memory_condition": memory, "backend": backend, "strategy_key": strategy_key}
            for metric in metrics:
                effect = paired_effect("memory_vs_reference", context, metric, reference_memory, memory, pairs, bootstrap_iterations, stable_seed("memory", key, memory, metric))
                if effect:
                    output.append(effect)
    return output


def format_row(row: dict[str, Any]) -> dict[str, Any]:
    output = row.copy()
    for field in ("median_difference", "mean_difference", "ci95_low", "ci95_high", "win_rate", "p_value", "q_value"):
        output[field] = number(output[field]) if output[field] != "" else ""
    return output


def write_markdown(path: Path, rows: list[dict[str, Any]], alpha: float) -> None:
    lines = ["# 統計顯著效果摘要", ""]
    lines += [
        f"此表只列出 FDR q-value ≤ {alpha:g} 且 bootstrap 95% CI 不跨 0 的 paired effects。",
        "差值定義為 `candidate - reference`；對 latency 與 page faults 指標而言，負值代表 candidate 較好。",
        "",
    ]
    if not rows:
        lines += ["未找到符合門檻的統計顯著效果。", ""]
    else:
        headers = ["Type", "Workload", "Layout", "Memory", "Backend", "Strategy", "Metric", "Reference", "Candidate", "Pairs", "Median diff", "95% CI", "q", "Direction"]
        lines += ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
        for row in rows:
            lines.append("| " + " | ".join(str(value).replace("|", "\\|") for value in [
                row["comparison_type"], row["workload_type"], row["layout"], row["memory_condition"], row["backend"] or "—",
                row["strategy_key"], row["metric"], row["reference"], row["candidate"], row["pair_count"],
                row["median_difference"], f"{row['ci95_low']}–{row['ci95_high']}", row["q_value"], row["direction"],
            ]) + " |")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment-dir", required=True, type=Path)
    parser.add_argument("--alpha", type=float, default=0.05)
    parser.add_argument("--min-pairs", type=int, default=5)
    parser.add_argument("--bootstrap-iterations", type=int, default=1000)
    args = parser.parse_args()
    root = args.experiment_dir.resolve()
    raw_path = root / "results" / "all_raw.csv"
    config_path = root / "config.json"
    if not raw_path.is_file():
        parser.error(f"required input is missing: {raw_path}")
    if not config_path.is_file():
        parser.error(f"required input is missing: {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    rows = read_csv(raw_path)
    metrics = tuple(metric for metric in METRICS if any(row.get(metric, "") != "" for row in rows))
    effects = []
    effects.extend(strategy_vs_baseline(rows, metrics, args.min_pairs, args.bootstrap_iterations))
    effects.extend(backend_comparisons(rows, config["prefetch"]["backends"], metrics, args.min_pairs, args.bootstrap_iterations))
    effects.extend(memory_comparisons(rows, [item["name"] for item in config["memory_conditions"]], metrics, args.min_pairs, args.bootstrap_iterations))
    bh_q_values(effects)
    significant = [
        row for row in effects
        if float(row["q_value"]) <= args.alpha
        and not (float(row["ci95_low"]) <= 0 <= float(row["ci95_high"]))
    ]
    significant.sort(key=lambda row: (row["comparison_type"], row["workload_type"], row["memory_condition"], row["layout"], row["metric"], abs(float(row["q_value"]))))
    formatted = [format_row(row) for row in significant]
    write_csv(root / "significant_effects.csv", formatted)
    temporary = root / f".significant_effects.md.{os.getpid()}.tmp"
    write_markdown(temporary, formatted, args.alpha)
    os.replace(temporary, root / "significant_effects.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
