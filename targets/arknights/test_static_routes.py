#!/usr/bin/env python3
"""Exercise every target static route without rerunning target analysis."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROUTES = (
    ("static_unity", "unity", ("static", "-StaticModule", "unity")),
    ("java", "java", ("java",)),
    ("static_java_errors", "java-errors", ("static", "-StaticModule", "java-errors")),
    ("native", "native", ("native",)),
    ("correlate", "correlate", ("correlate",)),
)

RETIRED_ANDROID_ROUTES = (
    ("il2cpp", ("il2cpp",)),
    ("static_il2cpp", ("static", "-StaticModule", "il2cpp")),
)


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


def snapshot(directory: Path) -> dict[str, dict]:
    if not directory.is_dir():
        return {}
    return {
        str(path.resolve()): {"size": path.stat().st_size, "sha256": sha256(path)}
        for path in sorted(directory.rglob("*")) if path.is_file()
    }


def run_command(command: list[str], logs: Path, name: str, timeout: int) -> dict:
    stdout_path = logs / f"{name}.stdout.txt"
    stderr_path = logs / f"{name}.stderr.txt"
    started = time.monotonic()
    timed_out = False
    launch_error = None
    exit_code = None
    stdout = ""
    stderr = ""
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
        exit_code = result.returncode
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = (exc.stdout or b"").decode("utf-8", errors="replace")
        stderr = (exc.stderr or b"").decode("utf-8", errors="replace")
    except OSError as exc:
        launch_error = f"{type(exc).__name__}: {exc}"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    return {
        "name": name,
        "command": command,
        "executable": command[0],
        "arguments": command[1:],
        "exit_code": exit_code,
        "timed_out": timed_out,
        "launch_error": launch_error,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    root = args.root.resolve()
    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"Refusing existing output: {output}")
    output.mkdir(parents=True)
    logs = output / "logs"
    logs.mkdir()
    routes_root = output / "routes"
    routes_root.mkdir()

    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    cli = root / "scripts" / "toolchain.ps1"
    arknights_profile = load_json(root / "targets" / "arknights" / "profile.json")
    pvz2_root = root / "targets" / "pvz2"
    pvz2_profile = load_json(pvz2_root / "profile.json")
    pvz2_before = snapshot(pvz2_root)
    commands = []
    arknights_results = []

    for route_name, module, route_args in ROUTES:
        route_output = routes_root / route_name
        command = [
            str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(cli),
            *route_args, "-Profile", "arknights", "-SmokeTest", "-Output", str(route_output),
        ]
        command_result = run_command(command, logs, f"arknights_{route_name}", args.timeout)
        commands.append(command_result)
        smoke_path = route_output / "static_module_smoke.json"
        smoke = load_json(smoke_path) if smoke_path.is_file() else None
        checks = {
            "exit_code_zero": command_result["exit_code"] == 0,
            "not_timed_out": command_result["timed_out"] is False,
            "no_launch_error": command_result["launch_error"] is None,
            "smoke_report_exists": smoke is not None,
            "smoke_passed": bool(smoke and smoke.get("passed") is True),
            "module_matches": bool(smoke and smoke.get("module") == module),
            "analysis_not_reexecuted": bool(smoke and smoke.get("analysis_reexecuted") is False),
            "target_state_preserved": bool(smoke and smoke.get("target_analysis_status") == arknights_profile["phase1_static"]["smoke_expectations"][module]["status"]),
        }
        arknights_results.append({
            "route": route_name, "module": module, "output": str(route_output),
            "passed": all(checks.values()), "checks": checks, "command": command_result,
            "smoke_report": str(smoke_path) if smoke else None,
        })

    pvz2_results = []
    pvz2_output_root = output / "pvz2_forbidden_outputs"
    for route_name, module, route_args in ROUTES:
        route_output = pvz2_output_root / route_name
        command = [
            str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(cli),
            *route_args, "-Profile", "pvz2", "-SmokeTest", "-Output", str(route_output),
        ]
        command_result = run_command(command, logs, f"pvz2_{route_name}", 30)
        commands.append(command_result)
        checks = {
            "refused_nonzero": command_result["exit_code"] not in (None, 0),
            "not_timed_out": command_result["timed_out"] is False,
            "no_output_created": not route_output.exists(),
        }
        pvz2_results.append({"route": route_name, "module": module, "passed": all(checks.values()), "checks": checks, "command": command_result})

    pvz2_after = snapshot(pvz2_root)
    retired_results = []
    retired_output_root = output / "retired_android_outputs"
    for route_name, route_args in RETIRED_ANDROID_ROUTES:
        route_output = retired_output_root / route_name
        command = [
            str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(cli),
            *route_args, "-Profile", "arknights", "-SmokeTest", "-Output", str(route_output),
        ]
        command_result = run_command(command, logs, f"retired_arknights_{route_name}", 30)
        commands.append(command_result)
        checks = {
            "refused_nonzero": command_result["exit_code"] not in (None, 0),
            "not_timed_out": command_result["timed_out"] is False,
            "no_output_created": not route_output.exists(),
            "clear_retired_reason": False,
        }
        stderr = Path(command_result["stderr_path"]).read_text(encoding="utf-8", errors="replace")
        checks["clear_retired_reason"] = "IL2CPP recovery is retired" in stderr
        retired_results.append({"route": route_name, "passed": all(checks.values()), "checks": checks, "command": command_result})

    isolation_checks = {
        "profile_not_configured": pvz2_profile.get("profile_state") == "not_configured",
        "analysis_disabled": pvz2_profile.get("analysis_enabled") is False,
        "capture_unset": pvz2_profile.get("storage", {}).get("capture") is None,
        "pvz2_profile_files_unchanged": pvz2_before == pvz2_after,
        "pvz2_outputs_absent": not pvz2_output_root.exists(),
        "all_pvz2_routes_refused": all(item["passed"] for item in pvz2_results),
    }
    all_arknights_routes = all(item["passed"] for item in arknights_results)
    all_retired_routes_refused = all(item["passed"] for item in retired_results)
    pvz2_isolation = all(isolation_checks.values())
    passed = all_arknights_routes and all_retired_routes_refused and pvz2_isolation

    command_path = output / "route_commands.json"
    write_json(command_path, {"schema_version": 1, "commands": commands})
    report_path = output / "route_smoke_tests.json"
    report = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "current_target_static_cli_routes",
        "passed": passed,
        "status": "passed" if passed else "failed",
        "analysis_reexecuted": False,
        "arknights": {"passed": all_arknights_routes, "route_count": len(arknights_results), "routes": arknights_results},
        "retired_android_il2cpp": {"passed": all_retired_routes_refused, "route_count": len(retired_results), "routes": retired_results},
        "pvz2_isolation": {"passed": pvz2_isolation, "route_count": len(pvz2_results), "checks": isolation_checks, "routes": pvz2_results},
        "state_policy": "Route smoke success does not promote retained target analysis states.",
    }
    write_json(report_path, report)
    markdown_path = output / "TESTS.md"
    markdown_path.write_text(
        "# Static Route Smoke Tests\n\n"
        f"Status: **{'passed' if passed else 'failed'}**\n\n"
        f"- Arknights routes: {sum(item['passed'] for item in arknights_results)}/{len(arknights_results)} passed.\n"
        f"- Retired Android IL2CPP routes refused: {sum(item['passed'] for item in retired_results)}/{len(retired_results)}.\n"
        f"- PVZ2 refusal routes: {sum(item['passed'] for item in pvz2_results)}/{len(pvz2_results)} passed.\n"
        "- Existing target analysis was not rerun.\n"
        "- Partial, blocked, and unverified target states were preserved.\n",
        encoding="utf-8",
    )
    generated = sorted(path for path in output.rglob("*") if path.is_file())
    hash_path = output / "KEY_SHA256SUMS.txt"
    hash_path.write_text("".join(f"{sha256(path)} *{path}\n" for path in generated), encoding="utf-8-sig")
    print(json.dumps({"passed": passed, "arknights_routes": len(arknights_results), "pvz2_refusals": len(pvz2_results), "output": str(output)}, ensure_ascii=False))
    return 0 if passed else 7


if __name__ == "__main__":
    sys.exit(main())
