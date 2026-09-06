#!/usr/bin/env python3
"""Offline validation of target-profile loading and artifact isolation."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--route-test", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    route_path = args.route_test.resolve()
    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"refusing to overwrite {output}")
    ark_path = ROOT / "targets" / "arknights" / "profile.json"
    pvz_path = ROOT / "targets" / "pvz2" / "profile.json"
    ark = json.loads(ark_path.read_text(encoding="utf-8-sig"))
    pvz = json.loads(pvz_path.read_text(encoding="utf-8-sig"))
    route = json.loads(route_path.read_text(encoding="utf-8-sig"))
    ark_report_dir = Path(route["profile_outputs"]["arknights"])
    ark_report = json.loads((ark_report_dir / "target_analysis_corrected.json").read_text(encoding="utf-8-sig"))
    checks = []

    def check(name: str, passed: bool, actual, expected):
        checks.append({"name": name, "passed": bool(passed), "actual": actual, "expected": expected})

    check("route_test_passed", route.get("status") == "passed", route.get("status"), "passed")
    check("ark_profile_is_configured", ark.get("analysis_enabled") is True and ark.get("profile_state") == "report_only", ark.get("profile_state"), "report_only")
    check("ark_identity_is_explicit", ark["target_identity"].get("package_name") == "com.hypergryph.arknights" and ark["target_identity"].get("version_name") == "2.7.61", ark["target_identity"], "package/version present")
    check("ark_source_is_055127", "target_analysis_20260810_055127" in ark["references"]["source_target_analysis"], ark["references"]["source_target_analysis"], "055127 source")
    check("ark_references_exist", all(Path(value).is_file() for value in ark["references"].values() if value), [key for key, value in ark["references"].items() if value and not Path(value).is_file()], "no missing references")
    check("ark_statuses_preserved", ark["current_capability_status"] == {
        "apk_manifest": "supported",
        "java": "partial",
        "unity_type4": "detected_but_adapter_missing",
        "il2cpp_metadata": "detected_nonstandard",
        "native_loader": "unresolved",
        "runtime_addresses": "unverified",
    }, ark["current_capability_status"], "accepted target status set")
    check("ark_report_status_preserved", ark_report["final_status"] == {
        "toolchain_health": "passed",
        "target_analysis": "partial",
        "unity": "detected_but_adapter_missing",
        "il2cpp": "detected_nonstandard",
        "native": "unresolved",
        "runtime": "unverified",
    }, ark_report["final_status"], "accepted final status set")
    check("ark_output_is_profile_scoped", ark_report_dir.name.startswith("profile_arknights_"), str(ark_report_dir), "profile_arknights_<timestamp>")
    check("pvz_profile_is_empty", pvz.get("analysis_enabled") is False and pvz["target_identity"]["package_name"] is None and pvz["storage"]["capture"] is None, pvz["profile_state"], "not_configured")
    check("pvz_has_no_report", route["profile_outputs"]["pvz2_report_count_before"] == route["profile_outputs"]["pvz2_report_count_after"] == 0, route["profile_outputs"], "0 reports before and after")
    check("no_old_054928_reference", "054928" not in json.dumps({"ark": ark, "pvz": pvz, "report": ark_report}), "no match", "no match")
    passed = all(item["passed"] for item in checks)
    output.mkdir(parents=True)
    report = {
        "schema_version": 1,
        "scope": "target_profile_isolation",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "passed" if passed else "failed",
        "read_only": True,
        "route_test": str(route_path),
        "checks": checks,
        "operations_not_performed": ["ADB", "target recollection", "target parsing", "Ghidra", "game execution", "runtime analysis"],
    }
    report_path = output / "target_profile_isolation.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (output / "TEST.md").write_text(
        "# Target Profile Isolation Test\n\n"
        f"Status: **{report['status']}**\n\n"
        "The Arknights profile loaded retained evidence and produced only a profile-scoped report. "
        "The PVZ2 profile remained unconfigured and produced no report.\n",
        encoding="utf-8",
    )
    hash_paths = [report_path, output / "TEST.md", route_path, ark_path, pvz_path, ark_report_dir / "target_analysis_corrected.json"]
    (output / "KEY_SHA256SUMS.txt").write_text(
        "".join(f"{sha256(path)}  {path}\n" for path in hash_paths), encoding="ascii"
    )
    return 0 if passed else 7


if __name__ == "__main__":
    raise SystemExit(main())
