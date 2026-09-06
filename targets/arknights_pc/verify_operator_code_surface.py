#!/usr/bin/env python3
"""Build a fail-closed code/resource surface report for one operator skill."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--operator-id", required=True)
    ap.add_argument("--skill-id", required=True)
    ap.add_argument("--current-dump", type=Path, required=True)
    ap.add_argument("--code-index", type=Path, required=True)
    ap.add_argument("--edge-report", type=Path, required=True)
    ap.add_argument("--resource-report", type=Path)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    for path in (args.current_dump, args.code_index, args.edge_report):
        if not path.is_file():
            raise SystemExit(f"missing input: {path}")
    if args.resource_report and not args.resource_report.is_file():
        raise SystemExit(f"missing resource report: {args.resource_report}")
    if args.output.exists():
        raise SystemExit(f"refusing existing output: {args.output}")

    codename = args.operator_id.rsplit("_", 1)[-1].lower()
    skill_token = args.skill_id.lower().replace("skchr_", "").replace("_skill", "")
    current_text = args.current_dump.read_text(encoding="utf-8", errors="replace")
    class_re = re.compile(r"^public (?:sealed )?class ([A-Za-z0-9_]+)", re.MULTILINE)
    class_names = [m.group(1) for m in class_re.finditer(current_text) if codename in m.group(1).lower()]
    exact_current = [name for name in class_names if skill_token in name.lower()]

    with sqlite3.connect(args.code_index) as db:
        rows = db.execute(
            """SELECT full_name, kind, base_types, source_path FROM legacy_types
               WHERE lower(full_name) LIKE ? ORDER BY full_name""",
            (f"%{codename}%",),
        ).fetchall()
        legacy_types = [
            {"full_name": r[0], "kind": r[1], "base_types": r[2], "source_path": r[3]}
            for r in rows
        ]
        skill_rows = db.execute(
            """SELECT lt.full_name, lm.name, lm.old_rva, cm.current_rva, mm.confidence, mm.status
               FROM legacy_methods lm
               JOIN legacy_types lt ON lt.id = lm.type_id
               LEFT JOIN method_matches mm ON mm.legacy_method_id = lm.id
               LEFT JOIN current_methods cm ON cm.id = mm.current_method_id
               WHERE lower(lt.full_name) LIKE ? AND lower(lm.name) LIKE ?
               ORDER BY lt.full_name, lm.name""",
            (f"%{codename}%", f"%{skill_token}%"),
        ).fetchall()
        skill_methods = [
            {"type_name": r[0], "method": r[1], "old_rva": r[2], "current_rva": r[3], "confidence": r[4], "status": r[5]}
            for r in skill_rows
        ]

    edge = load_json(args.edge_report)
    resource = load_json(args.resource_report) if args.resource_report else None
    exact_skill_present = bool(exact_current)
    status = "passed_operator_code_surface"
    if not exact_skill_present:
        status = "partial_code_surface_no_exact_skill_class"
    if not skill_methods and not legacy_types:
        status = "blocked_no_legacy_skill_reference"
    result = {
        "schema_version": 1,
        "status": status,
        "method": "current_dump_class_scan_plus_legacy_index_skill_lookup_plus_current_edge_report",
        "target": {"operator_id": args.operator_id, "skill_id": args.skill_id, "codename": codename, "skill_token": skill_token},
        "sources": {
            "current_dump": {"path": str(args.current_dump.resolve()), "sha256": sha256(args.current_dump.resolve())},
            "code_index": {"path": str(args.code_index.resolve()), "sha256": sha256(args.code_index.resolve())},
            "edge_report": {"path": str(args.edge_report.resolve()), "sha256": sha256(args.edge_report.resolve())},
            "resource_report": ({"path": str(args.resource_report.resolve()), "sha256": sha256(args.resource_report.resolve())} if args.resource_report else None),
        },
        "current_dump": {"codename_class_matches": class_names, "exact_skill_class_matches": exact_current},
        "legacy_reference": {"types": legacy_types, "skill_methods": skill_methods},
        "current_edge_coverage": edge.get("statistics", {}),
        "resource_status": resource.get("status") if resource else "not_supplied",
        "evidence_policy": {
            "current_pc_class_presence_proven": exact_skill_present,
            "current_pc_static_edge_proven": bool(edge.get("evidence_policy", {}).get("current_pc_static_edge_proven")),
            "legacy_calls_called_by_current": False,
            "runtime_invocation_proven": False,
            "android_address_reuse": False,
        },
        "limitations": [
            "An exact skill class in the current dump is required before claiming current code coverage.",
            "Mapped legacy methods remain structural candidates until current-PC pseudocode resolves their direct calls.",
            "No runtime invocation or execution evidence is produced by this report.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8-sig")
    print(json.dumps({"status": status, "current_exact_skill_classes": len(exact_current), "legacy_skill_methods": len(skill_methods)}))
    return 0 if status == "passed_operator_code_surface" else 1


if __name__ == "__main__":
    raise SystemExit(main())
