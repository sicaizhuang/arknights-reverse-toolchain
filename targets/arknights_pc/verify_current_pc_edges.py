#!/usr/bin/env python3
"""Verify current-PC direct call edges where bounded pseudocode exists."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import code_reference_index as cri


CALL_RE = re.compile(r"(?:FUN_|func_0x)(?:0x)?([0-9A-Fa-f]{6,16})")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def label(full_name: str, method: str) -> str:
    return f"{full_name}_{method}".replace(".", "_").replace("<", "_").replace(">", "_")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current-dump", type=Path, required=True)
    parser.add_argument("--pseudocode-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"Refusing existing output: {args.output}")

    current = cri.parse_current_dump(args.current_dump.resolve())
    rva_map: dict[int, list[dict[str, Any]]] = {}
    label_map: dict[str, list[dict[str, Any]]] = {}
    for type_row in current:
        for method in type_row["methods"]:
            if not method.get("current_rva"):
                continue
            try:
                rva = int(str(method["current_rva"]), 16)
            except ValueError:
                continue
            record = {
                "type_name": type_row["full_name"],
                "method": method["name"],
                "current_rva": f"0x{rva:X}",
                "current_va": method.get("current_va"),
                "source_line": method["source_line"],
            }
            rva_map.setdefault(rva, []).append(record)
            label_map.setdefault(label(type_row["full_name"], method["name"]), []).append(record)

    files = sorted(args.pseudocode_root.resolve().glob("*.c"))
    function_rows = []
    edge_rows = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        stem = path.stem
        caller_candidates = label_map.get(stem, [])
        caller = caller_candidates[0] if len(caller_candidates) == 1 else None
        addresses = sorted({int(match.group(1), 16) for match in CALL_RE.finditer(text)})
        # Ghidra pseudocode uses absolute Windows image VAs for FUN_ symbols.
        calls = []
        for address in addresses:
            rva = address - 0x180000000 if address >= 0x180000000 else address
            targets = rva_map.get(rva, [])
            calls.append({
                "address": f"0x{address:X}",
                "rva": f"0x{rva:X}",
                "targets": targets,
                "status": "resolved_current_method" if len(targets) == 1 else "unresolved_or_ambiguous",
            })
            if caller and len(targets) == 1:
                edge_rows.append({
                    "caller": caller,
                    "target": targets[0],
                    "evidence": "current_PC_pseudocode_direct_call_to_current_dump_RVA",
                    "pseudocode": str(path),
                    "pseudocode_sha256": sha256(path),
                })
        function_rows.append({
            "pseudocode": str(path),
            "sha256": sha256(path),
            "caller": caller,
            "caller_status": "resolved_current_method" if caller else "unresolved_pseudocode_label",
            "direct_calls": calls,
        })

    result = {
        "schema_version": 1,
        "status": "passed_current_pc_static_edge_verification" if edge_rows else "partial_no_resolved_current_pc_edges",
        "method": "current_dump_rva_map_plus_bounded_current_pc_pseudocode_direct_calls",
        "sources": {
            "current_dump": {"path": str(args.current_dump.resolve()), "sha256": sha256(args.current_dump.resolve())},
            "pseudocode_root": str(args.pseudocode_root.resolve()),
            "pseudocode_file_count": len(files),
        },
        "statistics": {
            "current_types": len(current),
            "current_methods_with_rva": sum(len(rows) for rows in rva_map.values()),
            "pseudocode_functions": len(files),
            "direct_call_sites": sum(len(row["direct_calls"]) for row in function_rows),
            "resolved_direct_call_sites": sum(sum(item["status"] == "resolved_current_method" for item in row["direct_calls"]) for row in function_rows),
            "verified_current_static_edges": len(edge_rows),
        },
        "functions": function_rows,
        "edges": edge_rows,
        "evidence_policy": {
            "current_pc_static_edge_proven": bool(edge_rows),
            "legacy_calls_called_by_current": False,
            "runtime_invocation_proven": False,
            "android_address_reuse": False,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8-sig")
    print(json.dumps({"status": result["status"], "edges": len(edge_rows), "functions": len(files)}))
    return 0 if edge_rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
