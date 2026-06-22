#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import random
import re
import shlex
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    from .common import atomic_json, canonical_json, read_json, sha256_file, sha256_value, sqlite_header
except ImportError:
    from common import atomic_json, canonical_json, read_json, sha256_file, sha256_value, sqlite_header

RAW_FIELDS = "cell_id status measurement_file repetition selected_interior selected_leaf syscall_count prefetch_elapsed_us first_query_latency_us average_latency_us major_page_faults minor_page_faults resident_after_cold_pages requested_selected_resident_ratio successful_selected_resident_ratio".split()
OPERATIONS_FIELDS = "op_no op_type target_id rows_returned bytes_returned elapsed_ns majflt_delta minflt_delta".split()
PAGE_TYPES = {"interior_index", "interior_table", "leaf_index", "leaf_table", "freelist_trunk", "freelist_leaf", "overflow", "lock_page", "unknown"}
WORKLOAD_TYPES = {f"{op}_{dist}_{area}" for op in ("read", "scan") for dist in ("uniform", "zipf") for area in ("full", "window", "tail")}
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MEMORY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MEMORY_SIZE_RE = re.compile(r"^([1-9][0-9]*)(B|KiB|MiB|GiB)$")
MEMORY_UNITS = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3}
MAX_EXACT_JSON_INTEGER = 2 ** 53 - 1


def nested(config: dict[str, Any], path: str, kind: type | tuple[type, ...] | None = None) -> Any:
    value: Any = config
    for component in path.split("."):
        if not isinstance(value, dict) or component not in value:
            raise ValueError(f"missing required config field: {path}")
        value = value[component]
    if kind is not None and (not isinstance(value, kind) or isinstance(value, bool) and kind is int):
        raise ValueError(f"invalid type for config field: {path}")
    return value


def resolve(config_path: Path, value: str) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else config_path.parent / path).resolve()


def normalize_memory_conditions(config: dict[str, Any], errors: list[str]) -> list[dict[str, Any]]:
    raw = config.get("memory_conditions")
    if not isinstance(raw, list) or not raw:
        errors.append("memory_conditions must be a nonempty array")
        return []
    normalized = []
    names = set()
    for index, item in enumerate(raw):
        prefix = f"memory_conditions[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object"); continue
        name, enabled = item.get("name"), item.get("enabled")
        if not isinstance(name, str) or not MEMORY_NAME_RE.fullmatch(name): errors.append(f"{prefix}.name is invalid")
        elif name in names: errors.append(f"duplicate memory condition name: {name}")
        else: names.add(name)
        if not isinstance(enabled, bool): errors.append(f"{prefix}.enabled must be boolean"); continue
        memory_max, memory_max_bytes = item.get("memory_max"), None
        if enabled:
            match = MEMORY_SIZE_RE.fullmatch(memory_max) if isinstance(memory_max, str) else None
            if not match: errors.append(f"{prefix}.memory_max is invalid")
            else:
                memory_max_bytes = int(match.group(1)) * MEMORY_UNITS[match.group(2)]
                if memory_max_bytes > MAX_EXACT_JSON_INTEGER: errors.append(f"{prefix}.memory_max exceeds the exact JSON integer range")
        elif memory_max is not None:
            errors.append(f"{prefix}.memory_max must be omitted or null when disabled")
        normalized.append({"name": name, "enabled": enabled, "memory_max_bytes": memory_max_bytes})
    return normalized


def read_classification(path: Path, page_size: int, page_count: int) -> tuple[int, int]:
    interior = leaf = 0
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["page_number", "page_type", "file_offset"]:
            raise ValueError(f"invalid classification header: {path}")
        rows = list(reader)
    if len(rows) != page_count:
        raise ValueError(f"classification page count mismatch: {path}")
    for index, row in enumerate(rows, 1):
        if int(row["page_number"]) != index or int(row["file_offset"]) != (index - 1) * page_size or row["page_type"] not in PAGE_TYPES:
            raise ValueError(f"invalid classification row {index}: {path}")
        interior += row["page_type"].startswith("interior_")
        leaf += row["page_type"].startswith("leaf_")
    return interior, leaf


def variants(config: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    strategies = nested(config, "prefetch.strategies", list)
    for item in strategies:
        if not isinstance(item, dict) or item.get("name") not in {"baseline", "range_interior", "offset_topk_interior", "residency_topk"}:
            raise ValueError(f"invalid strategy: {item!r}")
        name = item["name"]
        if name in {"baseline", "range_interior"}:
            result.append({"strategy": name, "variant": name, "n": None, "interior_k": None, "leaf_k": None})
        elif name == "offset_topk_interior":
            sweep = item.get("n")
            if not isinstance(sweep, dict) or ("values" in sweep) == ("range" in sweep): raise ValueError("offset_topk_interior.n requires exactly one of values/range")
            if "values" in sweep: values = sweep["values"]
            else:
                spec = sweep["range"]
                values = list(range(spec["start"], spec["end_exclusive"], spec["step"]))
            for value in values: result.append({"strategy": name, "variant": f"n{value}", "n": value, "interior_k": None, "leaf_k": None})
        else:
            for value in item.get("variants", []):
                ik, lk = value.get("interior_k"), value.get("leaf_k")
                result.append({"strategy": name, "variant": value.get("label", f"interior{ik}_leaf{lk}"), "n": None, "interior_k": ik, "leaf_k": lk})
    return result


def strategy_key(variant: dict[str, Any]) -> str:
    strategy = variant["strategy"]
    if strategy in {"baseline", "range_interior"}:
        return strategy
    if strategy == "offset_topk_interior":
        return f"offset_topk_interior_n{variant['n']}"
    safe_variant = re.sub(r"[^a-z0-9_-]+", "-", str(variant["variant"]).lower()).strip("-_") or "variant"
    return f"residency_topk_{safe_variant}_i{variant['interior_k']}_l{variant['leaf_k']}"


def validate(config: dict[str, Any], config_path: Path) -> dict[str, Any]:
    errors: list[str] = []
    required = {
        "schema_version": int, "experiment.id": str, "experiment.output_root": str, "experiment.resume": bool,
        "paths.workloads_directory": str, "paths.benchmark_harness": str, "paths.prefetch_runner": str,
        "paths.classify_pages": str, "paths.residency_checker": str, "paths.drop_caches_script": str,
        "layouts": dict, "workloads.types": list, "workloads.sampling_seed": int,
        "workloads.training.pool_start": int, "workloads.training.pool_end_inclusive": int, "workloads.training.count": int,
        "workloads.measurement.pool_start": int, "workloads.measurement.pool_end_inclusive": int, "workloads.measurement.count": int,
        "workloads.measurement.repetitions": int, "prefetch.backends": list, "prefetch.pread_chunk_bytes": int,
        "prefetch.strategies": list, "cold_protocol": dict, "cold_protocol.cold_advice": str,
        "cold_protocol.sqlite_open_timing": str, "cold_protocol.schema_init_timing": str,
        "cold_protocol.drop_caches_use_sudo": bool, "memory_conditions": list,
        "execution.layout_order": list, "execution.strategy_order": list, "execution.cell_timeout_seconds": int,
        "statistics.percentiles": list, "statistics.percentile_method": str,
    }
    for path, kind in required.items():
        try: nested(config, path, kind)
        except (ValueError, TypeError) as exc: errors.append(str(exc))
    if errors: raise ValueError("\n".join(errors))
    if config["schema_version"] != 1: errors.append("schema_version must be 1")
    if not ID_RE.fullmatch(nested(config, "experiment.id")): errors.append("experiment.id is invalid")
    backends = nested(config, "prefetch.backends")
    valid_backend_values = bool(backends) and all(isinstance(backend, str) and backend in {"madvise", "pread"} for backend in backends)
    if not valid_backend_values or (valid_backend_values and len(backends) != len(set(backends))):
        errors.append("prefetch.backends must be a nonempty, duplicate-free array of madvise/pread values")
    if nested(config, "statistics.percentile_method") != "nearest_rank": errors.append("only nearest_rank percentile_method is supported")
    percentiles = nested(config, "statistics.percentiles")
    if not all(isinstance(p, int) and not isinstance(p, bool) and 0 < p <= 100 for p in percentiles) or not {25, 50, 75, 99}.issubset(percentiles):
        errors.append("statistics.percentiles must contain valid 25, 50, 75, and 99 entries")
    expected_cold = {"cold_advice": "none", "sqlite_open_timing": "before-cold", "schema_init_timing": "before-cold"}
    for key, expected in expected_cold.items():
        if config["cold_protocol"].get(key) != expected: errors.append(f"cold_protocol.{key} must be {expected}")
    for path in ("workloads.training.count", "workloads.measurement.count", "workloads.measurement.repetitions", "execution.cell_timeout_seconds"):
        if nested(config, path) <= 0: errors.append(f"{path} must be positive")
    memory_conditions = normalize_memory_conditions(config, errors)
    workload_types = nested(config, "workloads.types")
    if not workload_types or len(workload_types) != len(set(workload_types)) or any(t not in WORKLOAD_TYPES for t in workload_types): errors.append("workloads.types contains invalid or duplicate values")
    paths = {key: resolve(config_path, nested(config, f"paths.{key}")) for key in ("workloads_directory", "benchmark_harness", "prefetch_runner", "classify_pages", "residency_checker", "drop_caches_script")}
    if not paths["workloads_directory"].is_dir(): errors.append(f"workloads directory not found: {paths['workloads_directory']}")
    else:
        for required_file in ("README.md", "SUMMARY.csv"):
            if not (paths["workloads_directory"] / required_file).is_file(): errors.append(f"workloads directory missing {required_file}")
    for key in ("benchmark_harness", "prefetch_runner", "classify_pages", "residency_checker", "drop_caches_script"):
        if not paths[key].is_file(): errors.append(f"tool not found: {paths[key]}")
        elif os.name != "nt" and not os.access(paths[key], os.X_OK): errors.append(f"tool is not executable: {paths[key]}")
    layouts: dict[str, Any] = {}
    order = nested(config, "execution.layout_order")
    if not config["layouts"]:
        errors.append("layouts must be a nonempty object")
    if (not all(isinstance(name, str) for name in order) or len(order) != len(set(order))
            or set(order) != set(config["layouts"])):
        errors.append("execution.layout_order must contain every enabled layout exactly once")
    for name in order:
        spec = config["layouts"].get(name, {})
        try:
            db, classification, metadata = (resolve(config_path, spec[k]) for k in ("database", "classification", "metadata"))
            if not all(p.is_file() for p in (db, classification, metadata)): raise ValueError("layout artifacts are missing")
            page_size, page_count = sqlite_header(db); meta = read_json(metadata)
            if meta.get("output_db_sha256") != sha256_file(db) or meta.get("classification_sha256") != sha256_file(classification): raise ValueError("layout metadata hashes do not match")
            ni, nl = read_classification(classification, page_size, page_count)
            layouts[name] = {"database": db, "classification": classification, "metadata": metadata, "page_size": page_size, "page_count": page_count, "eligible_interior": ni, "eligible_leaf": nl, "database_sha256": sha256_file(db), "classification_sha256": sha256_file(classification)}
        except Exception as exc: errors.append(f"layout {name}: {exc}")
    try: expanded = variants(config)
    except Exception as exc: errors.append(str(exc)); expanded = []
    strategy_order = nested(config, "execution.strategy_order")
    if (not all(isinstance(name, str) for name in strategy_order)
            or len(strategy_order) != len(set(strategy_order))
            or set(strategy_order) != {v["strategy"] for v in expanded}):
        errors.append("execution.strategy_order must contain every configured strategy exactly once")
    if sum(v["strategy"] == "baseline" for v in expanded) != 1: errors.append("exactly one baseline strategy is required")
    if len({(v["strategy"], v["variant"], v["n"], v["interior_k"], v["leaf_k"]) for v in expanded}) != len(expanded): errors.append("duplicate strategy cells exist")
    keys = [strategy_key(v) for v in expanded]
    if len(keys) != len(set(keys)): errors.append("strategy-key values are not unique")
    for variant in expanded:
        for name, layout in layouts.items():
            if variant["n"] is not None and (not isinstance(variant["n"], int) or isinstance(variant["n"], bool) or not 0 < variant["n"] <= layout["eligible_interior"]): errors.append(f"{name}/{variant['variant']}: invalid N")
            for key, maximum in (("interior_k", layout["eligible_interior"]), ("leaf_k", layout["eligible_leaf"])):
                value = variant[key]
                if value is not None and (not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= maximum): errors.append(f"{name}/{variant['variant']}: invalid {key}")
            if variant["strategy"] == "residency_topk" and variant["interior_k"] == variant["leaf_k"] == 0: errors.append("residency_topk 0/0 duplicates baseline")
            if variant["strategy"] == "range_interior" and not layout["eligible_interior"]: errors.append(f"{name}: range_interior has no eligible pages")
    chunk = nested(config, "prefetch.pread_chunk_bytes")
    if chunk <= 0 or any(chunk % layout["page_size"] for layout in layouts.values()): errors.append("prefetch.pread_chunk_bytes must be positive and page aligned")
    pools: dict[str, dict[str, list[Path]]] = {}
    for workload_type in workload_types:
        pools[workload_type] = {}
        for phase in ("training", "measurement"):
            start, end, count = (nested(config, f"workloads.{phase}.{key}") for key in ("pool_start", "pool_end_inclusive", "count"))
            if start > end or count > end - start + 1: errors.append(f"invalid {phase} pool/count for {workload_type}"); continue
            files = [paths["workloads_directory"] / f"{workload_type}_{index:03d}.txt" for index in range(start, end + 1)]
            missing = [str(p) for p in files if not p.is_file()]
            if missing: errors.append(f"missing workload files: {', '.join(missing[:3])}")
            pools[workload_type][phase] = files
    if errors: raise ValueError("\n".join(errors))
    return {"paths": paths, "layouts": layouts, "variants": expanded, "pools": pools, "memory_conditions": memory_conditions}


def select_workloads(config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    rng = random.Random(nested(config, "workloads.sampling_seed")); selected = {}
    for workload_type in nested(config, "workloads.types"):
        selected[workload_type] = {}
        for phase in ("training", "measurement"):
            count = nested(config, f"workloads.{phase}.count")
            picks = rng.sample(context["pools"][workload_type][phase], count)
            selected[workload_type][phase] = [{"path": str(p.resolve()), "name": p.name, "sha256": sha256_file(p)} for p in picks]
    return selected


def tool_hashes(paths: dict[str, Path]) -> dict[str, dict[str, str]]:
    result = {name: {"path": str(path), "sha256": sha256_file(path)} for name, path in paths.items() if name != "workloads_directory"}
    source_dir = Path(__file__).resolve().parent
    for name in ("orchestrator.py", "aggregate_training.py", "summarize_results.py", "plot_tradeoff.py", "generate_report.py", "common.py"):
        path = source_dir / name
        result[name] = {"path": str(path), "sha256": sha256_file(path)}
    return result


def environment_metadata(layouts: dict[str, Any]) -> dict[str, Any]:
    cpu_model = platform.processor() or None
    total_ram = None
    try:
        proc_cpu = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
        match = re.search(r"^model name\s*:\s*(.+)$", proc_cpu, re.MULTILINE)
        if match: cpu_model = match.group(1).strip()
        meminfo = Path("/proc/meminfo").read_text(encoding="utf-8")
        total_ram = int(re.search(r"^MemTotal:\s+(\d+) kB$", meminfo, re.MULTILINE).group(1)) * 1024
    except (OSError, AttributeError):
        pass
    first_db = next(iter(layouts.values()))["database"]
    filesystem_type = storage = None
    if os.name != "nt":
        try: filesystem_type = subprocess.run(["findmnt", "-no", "FSTYPE", "--target", str(first_db)], check=True, capture_output=True, text=True).stdout.strip()
        except (OSError, subprocess.SubprocessError): pass
        try: storage = subprocess.run(["lsblk", "-J", "-o", "NAME,TYPE,SIZE,MODEL,ROTA,MOUNTPOINTS"], check=True, capture_output=True, text=True).stdout.strip()
        except (OSError, subprocess.SubprocessError): pass
    return {"linux_kernel_version": platform.release(), "hostname": socket.gethostname(), "cpu_model": cpu_model,
            "logical_cpu_count": os.cpu_count(), "total_ram_bytes": total_ram, "filesystem_type": filesystem_type,
            "storage_device_info": storage, "sqlite_version": sqlite3.sqlite_version,
            "sqlite_page_sizes": {name: value["page_size"] for name, value in layouts.items()}}


def run_process(command: list[str], stdout_path: Path, stderr_path: Path, timeout: int, *, memory: dict[str, Any] | None = None, unit: str | None = None) -> tuple[int, bool]:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    actual = command
    if memory and memory["enabled"]:
        # Scope commands are synchronous; systemd rejects --wait with --scope.
        actual = ["systemd-run", "--user", "--scope", "--collect", "--expand-environment=no", f"--unit={unit}", "-p", f"MemoryMax={memory['memory_max_bytes']}", *command]
    with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
        proc = subprocess.Popen(actual, stdout=out, stderr=err, start_new_session=not (memory and memory["enabled"]))
        try: return proc.wait(timeout=timeout), False
        except subprocess.TimeoutExpired:
            if memory and memory["enabled"]:
                subprocess.run(["systemctl", "--user", "kill", "--kill-whom=all", f"{unit}.scope"], check=False)
                proc.terminate()
            else:
                os.killpg(proc.pid, signal.SIGTERM)
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if memory and memory["enabled"]: proc.kill()
                else: os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            return proc.returncode or -1, True


def parse_record(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    summary = re.search(r"ops=(\d+) avg_latency_us=([0-9.]+) total_majflt=(\d+) total_minflt=(\d+) first_query_latency_us=([0-9.]+)", text)
    resident = re.search(r"resident SQLite pages after madvise:\s+(\d+)/(\d+)", text)
    output = re.search(r"^output=(.+)$", text, re.MULTILINE)
    if not summary or not resident or not output: raise ValueError("harness run record is incomplete")
    ranges = [(int(a), int(b)) for a, b in re.findall(r"after madvise resident range\[\d+\]=(\d+)-(\d+)", text)]
    return {"ops": int(summary[1]), "average_latency_us": float(summary[2]), "major_page_faults": int(summary[3]), "minor_page_faults": int(summary[4]), "first_query_latency_us": float(summary[5]), "resident_after_cold_pages": int(resident[1]), "after_cold_ranges": ranges, "operations_path": output.group(1).strip()}


def reusable_completed_cell(prior: dict[str, Any], cell_id: str, variant: dict[str, Any], memory_condition: dict[str, Any]) -> bool:
    """Return true only when a completed cell and its required artifacts are parseable."""
    try:
        if prior.get("status") != "completed" or prior.get("cell_id") != cell_id or not isinstance(prior.get("raw_result"), dict):
            return False
        artifacts = prior["artifacts"]
        record = Path(artifacts["run_record"])
        operations = Path(artifacts["operations_csv"])
        metrics = parse_record(record)
        if Path(metrics["operations_path"]).resolve() != operations.resolve():
            return False
        with operations.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != OPERATIONS_FIELDS or not any(True for _ in reader):
                return False
        if variant["strategy"] != "baseline":
            result = read_json(Path(artifacts["prefetch_result_json"]))
            if (result.get("cell_id") != cell_id or result.get("status") != "completed"
                    or result.get("memory_condition") != memory_condition):
                return False
        return True
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False


def selected_ratios(path: Path, ranges: list[tuple[int, int]]) -> tuple[float | None, float | None]:
    with path.open(newline="", encoding="utf-8") as handle: rows = list(csv.DictReader(handle))
    resident = lambda page: any(start <= page <= end for start, end in ranges)
    requested = [int(r["page_number"]) for r in rows]; successful = [int(r["page_number"]) for r in rows if r["prefetch_succeeded"] == "1"]
    return ((sum(map(resident, requested)) / len(requested)) if requested else None, (sum(map(resident, successful)) / len(successful)) if successful else None)


def profile_id(layout: dict[str, Any], training: list[dict[str, str]], aggregate: Path, memory_condition: dict[str, Any]) -> str:
    value = [layout["database_sha256"], layout["classification_sha256"], canonical_json(memory_condition).decode(), *[x["sha256"] for x in training], "1", sha256_file(aggregate)]
    return hashlib.sha256("\n".join(value).encode()).hexdigest()


def ensure_profile(root: Path, layout_name: str, workload_type: str, layout: dict[str, Any], training: list[dict[str, str]], memory_condition: dict[str, Any], config: dict[str, Any], context: dict[str, Any], experiment_logs: Path) -> tuple[str, Path]:
    script = Path(__file__).with_name("aggregate_training.py"); pid = profile_id(layout, training, script, memory_condition)
    target = root / layout_name / workload_type / memory_condition["name"] / pid; profile = target / "profile.json"
    profile_csv = target / "residency_counts.csv"
    if profile.is_file() and profile_csv.is_file():
        metadata = read_json(profile)
        if metadata.get("profile_csv_sha256") == sha256_file(profile_csv) and metadata.get("database_sha256") == layout["database_sha256"] and metadata.get("memory_condition") == memory_condition:
            return pid, target
    snapshots = target / "snapshots"; snapshots.mkdir(parents=True, exist_ok=True)
    for index, workload in enumerate(training, 1):
        snapshot = snapshots / f"{index:03d}.csv"
        if snapshot.is_file(): continue
        run_dir = target / "runs" / f"{index:03d}"; run_dir.mkdir(parents=True, exist_ok=True)
        command = [str(context["paths"]["benchmark_harness"]), "--db", str(layout["database"]), "--workload", workload["path"], "--output", str(run_dir / "operations.csv"), "--record-dir", str(run_dir), "--mmap-size", str(layout["database"].stat().st_size), "--cold-advice", "none", "--sqlite-open-timing", "before-cold", "--schema-init-timing", "before-cold", "--drop-caches-script", str(context["paths"]["drop_caches_script"])]
        if config["cold_protocol"].get("drop_caches_use_sudo"): command.append("--drop-caches-use-sudo")
        wrapper = run_dir / "run-training.sh"; shell_script(wrapper, command)
        code, timed_out = run_process([str(wrapper)], experiment_logs / f"training-{layout_name}-{workload_type}-{memory_condition['name']}-{index}.out", experiment_logs / f"training-{layout_name}-{workload_type}-{memory_condition['name']}-{index}.err", nested(config, "execution.cell_timeout_seconds"), memory=memory_condition, unit=f"training-{pid[:12]}-{index}")
        if timed_out or code: raise RuntimeError(f"training run failed for {layout_name}/{workload_type}/{memory_condition['name']}/{index}")
        subprocess.run([str(context["paths"]["residency_checker"]), str(layout["database"]), str(snapshot)], check=True)
    subprocess.run([sys.executable, str(script), "--classification", str(layout["classification"]), "--snapshots", *map(str, sorted(snapshots.glob("*.csv"))), "--output-dir", str(target), "--layout", layout_name, "--database", str(layout["database"]), "--workload-type", workload_type, "--memory-condition-json", canonical_json(memory_condition).decode(), "--training-workloads", *[x["path"] for x in training]], check=True)
    return pid, target


def shell_script(path: Path, command: list[str]) -> None:
    path.write_text("#!/bin/sh\nset -eu\nexec " + shlex.join(command) + "\n", encoding="utf-8", newline="\n"); path.chmod(0o700)


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--config", required=True, type=Path); args = parser.parse_args(); config_path = args.config.resolve()
    try: config = read_json(config_path); context = validate(config, config_path)
    except Exception as exc: print(f"preflight: {exc}", file=sys.stderr); return 2
    config_hash = sha256_file(config_path); experiment_root = resolve(config_path, nested(config, "experiment.output_root")) / nested(config, "experiment.id")
    if experiment_root.exists():
        existing = experiment_root / "config.json"
        if not nested(config, "experiment.resume") or not existing.is_file() or sha256_file(existing) != config_hash:
            print("experiment directory exists but resume/config hash rules are not satisfied", file=sys.stderr); return 2
    selected = select_workloads(config, context) if not experiment_root.exists() else read_json(experiment_root / "manifest.json")["workloads"]
    experiment_root.mkdir(parents=True, exist_ok=True); (experiment_root / "cells").mkdir(exist_ok=True); (experiment_root / "logs").mkdir(exist_ok=True); (experiment_root / "plots").mkdir(exist_ok=True)
    if not (experiment_root / "config.json").exists(): (experiment_root / "config.json").write_bytes(config_path.read_bytes())
    environment = environment_metadata(context["layouts"]); environment["memory_conditions"] = context["memory_conditions"]
    manifest = {"schema_version": 1, "experiment_id": nested(config, "experiment.id"), "config_sha256": config_hash, "sampling_seed": nested(config, "workloads.sampling_seed"), "workloads": selected, "memory_conditions": context["memory_conditions"], "tools": tool_hashes(context["paths"]), "environment": environment, "profiles": {}}
    profile_root = resolve(config_path, config.get("training_profiles_root", "../data/training_profiles"))
    needs_training = any(v["strategy"] == "residency_topk" for v in context["variants"])
    profiles: dict[tuple[str, str, str], Path] = {}
    if needs_training:
        for layout_name in nested(config, "execution.layout_order"):
            for workload_type in nested(config, "workloads.types"):
                for memory_condition in context["memory_conditions"]:
                    pid, path = ensure_profile(profile_root, layout_name, workload_type, context["layouts"][layout_name], selected[workload_type]["training"], memory_condition, config, context, experiment_root / "logs")
                    profiles[(layout_name, workload_type, memory_condition["name"])] = path; manifest["profiles"][f"{layout_name}/{workload_type}/{memory_condition['name']}"] = {"profile_id": pid, "path": str(path), "memory_condition": memory_condition}
    atomic_json(experiment_root / "manifest.json", manifest)
    variants_by_order = sorted(context["variants"], key=lambda v: nested(config, "execution.strategy_order").index(v["strategy"]))
    variants_ordered = [v for v in variants_by_order if v["strategy"] == "baseline"] + [v for v in variants_by_order if v["strategy"] != "baseline"]
    raw_rows = []
    backends = nested(config, "prefetch.backends")
    baseline_variant = next(v for v in variants_ordered if v["strategy"] == "baseline")
    execution_units = [(baseline_variant, name, workload_type, memory_condition, None) for name in nested(config, "execution.layout_order") for workload_type in nested(config, "workloads.types") for memory_condition in context["memory_conditions"]]
    execution_units += [(variant, name, workload_type, memory_condition, backend) for name in nested(config, "execution.layout_order") for workload_type in nested(config, "workloads.types") for memory_condition in context["memory_conditions"] for variant in variants_ordered if variant["strategy"] != "baseline" for backend in backends]
    for variant, layout_name, workload_type, memory_condition, backend in execution_units:
            for layout in (context["layouts"][layout_name],):
                for workload in selected[workload_type]["measurement"]:
                    for repetition in range(1, nested(config, "workloads.measurement.repetitions") + 1):
                        identity = {"database_sha256": layout["database_sha256"], "layout": layout_name, "training_workloads": [x["sha256"] for x in selected[workload_type]["training"]], "measurement_workload": workload["sha256"], **variant, "backend": backend, "pread_chunk_bytes": nested(config, "prefetch.pread_chunk_bytes"), "cold_protocol": config["cold_protocol"], "memory_condition": memory_condition, "repetition": repetition, "tools": manifest["tools"]}
                        cell_id = sha256_value(identity); cell_dir = experiment_root / "cells" / cell_id; cell_json = cell_dir / "cell.json"
                        if cell_json.is_file():
                            prior = read_json(cell_json)
                            if reusable_completed_cell(prior, cell_id, variant, memory_condition):
                                raw_rows.append(prior["raw_result"]); continue
                        cell_dir.mkdir(parents=True, exist_ok=True); operations = cell_dir / "operations.csv"; record_dir = cell_dir / "record"; record_dir.mkdir(exist_ok=True)
                        prefetch_result = selected_csv = None
                        if variant["strategy"] != "baseline":
                            prefetch_result, selected_csv = cell_dir / "prefetch_result.json", cell_dir / "selected_pages.csv"
                            profile_path = profiles.get((layout_name, workload_type, memory_condition["name"]))
                            job = {"schema_version": 1, "cell_id": cell_id, "backend": backend, "strategy": variant["strategy"], "variant": variant["variant"], "memory_condition": memory_condition, "database": {"path": str(layout["database"]), "sha256": layout["database_sha256"]}, "classification": {"path": str(layout["classification"]), "sha256": layout["classification_sha256"]}, "training_profile": ({"path": str(profile_path / "residency_counts.csv"), "sha256": sha256_file(profile_path / "residency_counts.csv")} if profile_path and variant["strategy"] == "residency_topk" else None), "parameters": {"n": variant["n"], "interior_k": variant["interior_k"], "leaf_k": variant["leaf_k"], "pread_chunk_bytes": nested(config, "prefetch.pread_chunk_bytes")}, "output": {"result_json": str(prefetch_result), "selected_pages_csv": str(selected_csv)}}
                            job_path = cell_dir / "prefetch_job.json"; atomic_json(job_path, job); post = cell_dir / "post-cold.sh"; shell_script(post, [str(context["paths"]["prefetch_runner"]), "--job", str(job_path)])
                        harness = [str(context["paths"]["benchmark_harness"]), "--db", str(layout["database"]), "--workload", workload["path"], "--output", str(operations), "--record-dir", str(record_dir), "--mmap-size", str(layout["database"].stat().st_size), "--cold-advice", "none", "--sqlite-open-timing", "before-cold", "--schema-init-timing", "before-cold", "--drop-caches-script", str(context["paths"]["drop_caches_script"])]
                        if config["cold_protocol"].get("drop_caches_use_sudo"): harness.append("--drop-caches-use-sudo")
                        if variant["strategy"] != "baseline": harness += ["--post-cold-script", str(post)]
                        wrapper = cell_dir / "run-cell.sh"; shell_script(wrapper, harness); stdout_log, stderr_log = experiment_root / "logs" / f"{cell_id}.out", experiment_root / "logs" / f"{cell_id}.err"
                        code, timed_out = run_process([str(wrapper)], stdout_log, stderr_log, nested(config, "execution.cell_timeout_seconds"), memory=memory_condition, unit=f"static-exp-{cell_id[:20]}")
                        status = "timeout" if timed_out else "failed" if code else "completed"; error = None; record_path = None; metrics = {}
                        try:
                            match = re.search(r"benchmark record: (.+)", stderr_log.read_text(encoding="utf-8", errors="replace")); record_path = Path(match.group(1).strip()) if match else None
                            if status == "completed" and (not record_path or not record_path.is_file()): raise ValueError("run record path not found")
                            if status == "completed": metrics = parse_record(record_path)
                        except Exception as exc: status, error = "failed", str(exc)
                        if metrics.get("operations_path"):
                            operations = Path(metrics["operations_path"])
                        result_data = read_json(prefetch_result) if prefetch_result and prefetch_result.is_file() else {}
                        requested = successful = None
                        if status == "completed" and selected_csv: requested, successful = selected_ratios(selected_csv, metrics["after_cold_ranges"])
                        raw = {key: "" for key in RAW_FIELDS}; raw.update({"experiment_id": nested(config, "experiment.id"), "cell_id": cell_id, "status": status, "layout": layout_name, "workload_type": workload_type, "memory_condition": memory_condition["name"], "memory_limit_enabled": memory_condition["enabled"], "memory_max_bytes": memory_condition["memory_max_bytes"] if memory_condition["memory_max_bytes"] is not None else "", "measurement_file": workload["name"], "repetition": repetition, "strategy": variant["strategy"], "variant": variant["variant"], "strategy_key": strategy_key(variant), "backend": backend, "n": variant["n"] if variant["n"] is not None else "", "interior_k": variant["interior_k"] if variant["interior_k"] is not None else "", "leaf_k": variant["leaf_k"] if variant["leaf_k"] is not None else ""})
                        if status == "completed": raw.update({"selected_interior": result_data.get("selected_interior_count", ""), "selected_leaf": result_data.get("selected_leaf_count", ""), "syscall_count": result_data.get("syscall_attempted_count", ""), "prefetch_elapsed_us": result_data.get("prefetch_elapsed_us", ""), "first_query_latency_us": metrics["first_query_latency_us"], "average_latency_us": metrics["average_latency_us"], "major_page_faults": metrics["major_page_faults"], "minor_page_faults": metrics["minor_page_faults"], "resident_after_cold_pages": metrics["resident_after_cold_pages"], "requested_selected_resident_ratio": requested if requested is not None else "", "successful_selected_resident_ratio": successful if successful is not None else ""})
                        cell = {"experiment_id": nested(config, "experiment.id"), "cell_id": cell_id, "status": status, "layout": layout_name, "database_sha256": layout["database_sha256"], "workload_type": workload_type, "memory_condition": memory_condition, "training_profile_sha256": sha256_file(profiles[(layout_name, workload_type, memory_condition["name"])] / "profile.json") if variant["strategy"] == "residency_topk" else None, "measurement_file": workload, "repetition": repetition, "strategy": variant, "backend": backend, "prefetch_result": result_data or None, "harness_metrics": metrics or None, "residency_metrics": {"requested_selected_resident_ratio": requested, "successful_selected_resident_ratio": successful}, "artifacts": {"run_record": str(record_path) if record_path else "", "operations_csv": str(operations), "prefetch_result_json": str(prefetch_result) if prefetch_result else None, "stdout": str(stdout_log), "stderr": str(stderr_log)}, "error": error, "timeout": timed_out, "raw_result": raw}
                        atomic_json(cell_json, cell); raw_rows.append(raw)
    grouped_raw: dict[tuple[str, str, str, str | None, str], list[dict[str, Any]]] = {}
    variant_lookup = {(v["strategy"], v["variant"], str(v["n"] or ""), str(v["interior_k"] if v["interior_k"] is not None else ""), str(v["leaf_k"] if v["leaf_k"] is not None else "")): v for v in context["variants"]}
    for row in raw_rows:
        variant = variant_lookup[(row["strategy"], row["variant"], str(row["n"]), str(row["interior_k"]), str(row["leaf_k"]))]
        grouped_raw.setdefault((row["workload_type"], row["layout"], row["memory_condition"], row.get("backend"), strategy_key(variant)), []).append(row)
    results_root = experiment_root / "results"
    for (workload_type, layout_name, memory_name, backend, key), rows in grouped_raw.items():
        condition_root = results_root / workload_type / layout_name / "memory_conditions" / memory_name
        output = condition_root / ("baseline" if backend is None else f"backends/{backend}/{key}") / "raw.csv"
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=RAW_FIELDS, extrasaction="ignore"); writer.writeheader(); writer.writerows(rows)
    subprocess.run([sys.executable, str(Path(__file__).with_name("summarize_results.py")), "--experiment-dir", str(experiment_root)], check=True)
    subprocess.run([sys.executable, str(Path(__file__).with_name("plot_tradeoff.py")), "--experiment-dir", str(experiment_root)], check=True)
    subprocess.run([sys.executable, str(Path(__file__).with_name("generate_report.py")), "--experiment-dir", str(experiment_root)], check=True)
    atomic_json(experiment_root / "state.json", {"status": "completed", "cell_count": len(raw_rows), "completed": sum(r["status"] == "completed" for r in raw_rows), "failed": sum(r["status"] != "completed" for r in raw_rows), "updated_at": time.time()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
