#!/usr/bin/env python3
"""Validate reusable invariants across completed operator closure reports."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ZERO_VALIDATION_FIELDS = (
    "unresolved_resource_count",
    "unresolved_pptr_count",
    "config_failure_count",
    "copy_hash_mismatch_count",
    "payload_export_failure_count",
    "negative_pollution_count",
    "zero_byte_count",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return payload


def validate(report_path: Path) -> dict[str, Any]:
    report = read_json(report_path)
    validation = report.get("validation", {})
    config = report.get("config", {})
    table_sources_path = Path(str(config.get("table_sources", "")))
    table_sources = read_json(table_sources_path) if table_sources_path.is_file() else {}
    current_tables = bool(table_sources) and all(
        isinstance(source, dict)
        and source.get("payload_source") == "current_unity_inventory_textasset"
        for source in table_sources.values()
    )
    zero_checks = {
        field: int(validation.get(field, -1)) == 0
        for field in ZERO_VALIDATION_FIELDS
    }
    checks = {
        "schema_2_or_newer": int(report.get("schema_version", 0)) >= 2,
        "completed_exact_static_closure": report.get("status") == "completed_exact_static_closure",
        "current_inventory_tables_only": current_tables,
        "six_or_more_config_tables": len(table_sources) >= 6,
        "has_object_graph": bool(report.get("object_graph", {}).get("nodes")),
        "has_source_bundles": bool(report.get("source_bundles")),
        **zero_checks,
    }
    return {
        "operator_id": report.get("operator_id"),
        "report": str(report_path.resolve()),
        "report_sha256": sha256(report_path),
        "status": report.get("status"),
        "schema_version": report.get("schema_version"),
        "counts": {
            "skills": config.get("skill_count"),
            "equipment": config.get("equipment_count"),
            "skins": config.get("skin_count"),
            "voice_lines": config.get("voice_line_count"),
            "source_bundles": len(report.get("source_bundles", [])),
            "objects": len(report.get("object_graph", {}).get("nodes", [])),
            "edges": len(report.get("object_graph", {}).get("edges", [])),
            "payload_exports": len(report.get("payload_exports", [])),
        },
        "checks": checks,
        "passed": all(checks.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.output.exists():
        raise SystemExit(f"Refusing existing output: {args.output}")
    cases = [validate(path.resolve()) for path in args.report]
    result = {
        "schema_version": 1,
        "status": "passed_operator_closure_matrix" if all(case["passed"] for case in cases) else "failed_operator_closure_matrix",
        "method": "report_invariants_and_current_inventory_provenance",
        "case_count": len(cases),
        "passed_count": sum(case["passed"] for case in cases),
        "cases": cases,
        "evidence_boundary": {
            "static_resource_closure_only": True,
            "runtime_invocation_proven": False,
            "legacy_group_references_authoritative": False,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8-sig")
    print(json.dumps({"status": result["status"], "cases": len(cases), "passed": result["passed_count"]}))
    return 0 if result["status"] == "passed_operator_closure_matrix" else 1


if __name__ == "__main__":
    raise SystemExit(main())
