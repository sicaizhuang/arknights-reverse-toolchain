#!/usr/bin/env python3
"""Bounded route smoke checks for retained Arknights phase-one artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


MODULES = ("unity", "java", "java-errors", "native", "correlate")
SCRIPT_KEYS = {"java-errors": "java_errors"}
REPORT_KEYS = {
    "unity": "unity_report",
    "java": "java_analysis",
    "java-errors": "java_error_classification",
    "native": "native_report",
    "correlate": "cross_layer_report",
}
SQLITE_KEYS = {
    "unity": "unity_index",
    "java": "java_index",
    "native": "native_index",
    "correlate": "cross_layer_index",
}


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def add_check(checks: list[dict], name: str, actual: object, expected: object) -> None:
    checks.append({"name": name, "passed": actual == expected, "actual": actual, "expected": expected})


def sqlite_contract(path: Path, queries: dict[str, str]) -> tuple[str, dict[str, int]]:
    database = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        quick_check = database.execute("PRAGMA quick_check").fetchone()[0]
        counts = {name: int(database.execute(query).fetchone()[0]) for name, query in queries.items()}
        return quick_check, counts
    finally:
        database.close()


def required_paths(module: str, static: dict, script: Path, report: Path, database: Path | None) -> list[Path]:
    inputs = static["inputs"]
    outputs = static["outputs"]
    paths = [script, report]
    if database:
        paths.append(database)
    if module == "unity":
        paths.append(Path(static["entrypoint"]))
    elif module == "java":
        paths.extend(Path(inputs[key]) for key in ("base_apk", "jadx_root", "jadx_command", "jadx_stdout", "jadx_stderr"))
    elif module == "java-errors":
        paths.extend(Path(outputs[key]) for key in ("java_index", "java_analysis"))
        paths.extend(Path(inputs[key]) for key in ("jadx_stdout", "jadx_stderr"))
    elif module == "native":
        paths.extend(Path(inputs[key]) for key in ("current_elf", "decoded_arm64_root", "apk_provenance"))
    elif module == "correlate":
        paths.extend(Path(outputs[key]) for key in ("unity_index", "java_index", "native_index", "il2cpp_provenance"))
        paths.extend(Path(inputs[key]) for key in ("jadx_root", "legacy_dump_cs"))
    return paths


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--module", choices=MODULES, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--help-timeout", type=int, default=15)
    args = parser.parse_args()

    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"Refusing existing output: {output}")
    output.mkdir(parents=True)
    logs = output / "logs"
    logs.mkdir()

    profile = load_json(args.profile.resolve())
    if profile.get("profile_id") != "arknights" or profile.get("analysis_enabled") is not True:
        raise SystemExit("Only the enabled Arknights profile is accepted")
    static = profile["phase1_static"]
    expectations = static["smoke_expectations"][args.module]
    script_key = SCRIPT_KEYS.get(args.module, args.module)
    script = Path(static["scripts"][script_key]).resolve()
    python = Path(static["python"]).resolve()
    report_path = Path(static["outputs"][REPORT_KEYS[args.module]]).resolve()
    database_path = Path(static["outputs"][SQLITE_KEYS[args.module]]).resolve() if args.module in SQLITE_KEYS else None

    stdout_path = logs / "analyzer_help.stdout.txt"
    stderr_path = logs / "analyzer_help.stderr.txt"
    command = [str(python), str(script), "--help"]
    started = time.monotonic()
    timed_out = False
    launch_error = None
    exit_code = None
    stdout = ""
    stderr = ""
    try:
        result = subprocess.run(command, capture_output=True, timeout=args.help_timeout, check=False)
        exit_code = result.returncode
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = (exc.stdout or b"").decode("utf-8", errors="replace")
        stderr = (exc.stderr or b"").decode("utf-8", errors="replace")
    except OSError as exc:
        launch_error = f"{type(exc).__name__}: {exc}"
    duration_ms = int((time.monotonic() - started) * 1000)
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    command_record = {
        "name": f"{args.module}_analyzer_help",
        "command": command,
        "executable": command[0],
        "arguments": command[1:],
        "exit_code": exit_code,
        "timed_out": timed_out,
        "launch_error": launch_error,
        "duration_ms": duration_ms,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    }
    command_path = output / "command.json"
    write_json(command_path, command_record)

    checks: list[dict] = []
    add_check(checks, "profile_id", profile.get("profile_id"), "arknights")
    add_check(checks, "analysis_enabled", profile.get("analysis_enabled"), True)
    add_check(checks, "analyzer_help_exit_code", exit_code, 0)
    add_check(checks, "analyzer_help_timed_out", timed_out, False)
    add_check(checks, "analyzer_help_launch_error", launch_error, None)
    paths = required_paths(args.module, static, script, report_path, database_path)
    for path in paths:
        add_check(checks, f"path_exists:{path}", path.exists(), True)

    report = load_json(report_path) if report_path.is_file() else {}
    add_check(checks, "retained_report_status", report.get("status"), expectations["status"])
    database_result = None

    if args.module == "unity" and database_path:
        quick, counts = sqlite_contract(database_path, {
            "bundles": "SELECT COUNT(*) FROM bundles",
            "objects": "SELECT COUNT(*) FROM objects",
            "dependencies": "SELECT COUNT(*) FROM dependencies",
            "object_errors": "SELECT COUNT(*) FROM object_errors",
        })
        database_result = {"path": str(database_path), "quick_check": quick, "counts": counts}
        add_check(checks, "sqlite_quick_check", quick, "ok")
        for key in ("bundles", "objects", "dependencies", "object_errors"):
            add_check(checks, f"sqlite_{key}", counts[key], expectations[key])
        add_check(checks, "report_unityfs_total", report.get("coverage", {}).get("unityfs_total"), expectations["bundles"])
        add_check(checks, "report_object_count", report.get("coverage", {}).get("object_count"), expectations["objects"])
    elif args.module == "java" and database_path:
        quick, counts = sqlite_contract(database_path, {key: f"SELECT COUNT(*) FROM {key}" for key in ("files", "classes", "methods", "fields")})
        database_result = {"path": str(database_path), "quick_check": quick, "counts": counts}
        add_check(checks, "sqlite_quick_check", quick, "ok")
        for key in ("files", "classes", "methods", "fields"):
            add_check(checks, f"sqlite_{key}", counts[key], expectations[key])
        add_check(checks, "jadx_reported_errors", report.get("jadx_errors", {}).get("reported_total"), 111)
    elif args.module == "java-errors":
        cli = report.get("jadx_cli_error_domain", {})
        generated = report.get("generated_source_marker_domain", {})
        add_check(checks, "reported_total", cli.get("reported_total"), expectations["reported_total"])
        add_check(checks, "unlocalized_reported_errors", cli.get("unlocalized_reported_errors"), expectations["unlocalized_reported_errors"])
        add_check(checks, "confirmed_issue_group_count", generated.get("confirmed_issue_group_count"), expectations["confirmed_issue_group_count"])
    elif args.module == "native" and database_path:
        quick, counts = sqlite_contract(database_path, {
            "files": "SELECT COUNT(*) FROM files", "sections": "SELECT COUNT(*) FROM sections",
            "relocations": "SELECT COUNT(*) FROM relocations", "strings": "SELECT COUNT(*) FROM strings",
        })
        database_result = {"path": str(database_path), "quick_check": quick, "counts": counts}
        add_check(checks, "sqlite_quick_check", quick, "ok")
        for key in ("files", "sections", "relocations", "strings"):
            add_check(checks, f"sqlite_{key}", counts[key], expectations[key])
        add_check(checks, "legacy_addresses_used", report.get("current_function_policy", {}).get("legacy_addresses_used"), False)
        add_check(checks, "manual_function_boundaries_created", report.get("current_function_policy", {}).get("manual_function_boundaries_created"), False)
    elif args.module == "correlate" and database_path:
        quick, counts = sqlite_contract(database_path, {
            "terms": "SELECT COUNT(*) FROM terms", "linked_terms": "SELECT COUNT(*) FROM linked_terms",
        })
        database_result = {"path": str(database_path), "quick_check": quick, "counts": counts}
        add_check(checks, "sqlite_quick_check", quick, "ok")
        for key in ("terms", "linked_terms"):
            add_check(checks, f"sqlite_{key}", counts[key], expectations[key])
        policy = report.get("status_policy", {}).get("correlation", "")
        add_check(checks, "semantic_edges_not_claimed", "not proven" in policy, True)

    passed = all(check["passed"] for check in checks)
    smoke_path = output / "static_module_smoke.json"
    smoke = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "arknights_phase1_static_route_smoke",
        "module": args.module,
        "passed": passed,
        "status": "passed" if passed else "failed",
        "analysis_reexecuted": False,
        "target_analysis_status": report.get("status"),
        "retained_report": str(report_path),
        "database_validation": database_result,
        "command": command_record,
        "checks": checks,
        "state_policy": "Smoke pass validates routing and retained result contracts; it does not promote partial, blocked, failed, or unverified target states.",
    }
    write_json(smoke_path, smoke)

    hashed = [smoke_path, command_path, stdout_path, stderr_path, args.profile.resolve(), script]
    manifest_path = output / "KEY_SHA256SUMS.txt"
    manifest_path.write_text("".join(f"{sha256(path)} *{path}\n" for path in hashed), encoding="utf-8-sig")
    print(json.dumps({"module": args.module, "passed": passed, "target_analysis_status": report.get("status"), "output": str(output)}, ensure_ascii=False))
    return 0 if passed else 7


if __name__ == "__main__":
    sys.exit(main())
