from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def external_refs(value: Any, result: dict[str, dict[str, str]]) -> None:
    if isinstance(value, dict):
        external = value.get("External")
        if isinstance(external, dict):
            cab = external.get("file_name") or external.get("path_name")
            if isinstance(cab, str) and cab:
                result.setdefault(cab, {"file_name": cab, "path_name": str(external.get("path_name", ""))})
        for child in value.values():
            external_refs(child, result)
    elif isinstance(value, list):
        for child in value:
            external_refs(child, result)


def bundle_records(probe: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["relative_path"]): item for item in probe.get("records", [])}


def main() -> int:
    parser = argparse.ArgumentParser(description="Join bounded PC skill prefab, effect assets and current code evidence")
    parser.add_argument("--skill-probe", type=Path, required=True, help="pc_asset_probe.json for the exact skill prefab")
    parser.add_argument("--skill-inventory", type=Path, required=True, help="JSON asset map for the exact skill prefab")
    parser.add_argument("--skill-export", type=Path, required=True, help="JSON export directory for the exact skill prefab")
    parser.add_argument("--effects-probe", type=Path, required=True, help="pc_asset_probe.json for the S2 effect bundle")
    parser.add_argument("--effects-inventory", type=Path, required=True, help="JSON asset map for S2 effect containers")
    parser.add_argument("--effects-export", type=Path, required=True, help="JSON export directory for S2 effects")
    parser.add_argument("--code-edges", type=Path, required=True, help="verified current-PC SkillData edge report")
    parser.add_argument("--dependency-probe", type=Path, help="optional probe report for a shared PC dependency bundle")
    parser.add_argument("--external-cab-probe", type=Path, help="exact external CAB/PathID resolver report")
    parser.add_argument("--monoscript-closure", type=Path, help="resolved PC MonoBehaviour TypeTree closure report")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--operator-id", default="char_4145_ulpia")
    parser.add_argument("--skill-id", default="skchr_ulpia_2")
    args = parser.parse_args()

    skill_probe = load_json(args.skill_probe)
    effects_probe = load_json(args.effects_probe)
    skill_inventory = load_json(args.skill_inventory)
    effects_inventory = load_json(args.effects_inventory)
    code_edges = load_json(args.code_edges)
    skill_records = bundle_records(skill_probe)
    effect_records = bundle_records(effects_probe)
    dependency_probe = load_json(args.dependency_probe) if args.dependency_probe else None
    external_cab_probe = load_json(args.external_cab_probe) if args.external_cab_probe else None
    monoscript_closure = load_json(args.monoscript_closure) if args.monoscript_closure else None

    refs: dict[str, dict[str, str]] = {}
    for path in sorted(args.skill_export.rglob("*.json")):
        external_refs(load_json(path), refs)

    skill_entries = skill_inventory.get("AssetEntries", [])
    effect_entries = [
        entry for entry in effects_inventory.get("AssetEntries", [])
        if "ulpia_skill_02_" in str(entry.get("Container", ""))
    ]
    effect_types = Counter(str(entry.get("Type", "unknown")) for entry in effect_entries)
    effect_containers = sorted({str(entry.get("Container")) for entry in effect_entries})
    wave_entries = [
        {
            "name": entry.get("Name"),
            "type": entry.get("Type"),
            "container": entry.get("Container"),
            "path_id": entry.get("PathID"),
        }
        for entry in effect_entries
        if str(entry.get("Name", "")).lower() in {"wave01", "wave01 (1)", "flow_01 (1)", "flow_02 (1)", "flow_02 (2)", "s2_wave01"}
    ]
    code_edge_subset = [
        edge for edge in code_edges.get("edges", [])
        if any(token in json.dumps(edge, ensure_ascii=False) for token in ("SkillData", "GetSkillData", "TryGetSkill", "Blackboard"))
    ]

    source_bundles = []
    records = list(skill_records.values()) + list(effect_records.values())
    if dependency_probe:
        records.extend(dependency_probe.get("records", []))
    for record in records:
        source_bundles.append({
            "relative_path": record.get("relative_path"),
            "bytes": record.get("bytes"),
            "sha256": record.get("sha256"),
            "status": record.get("status"),
        })

    if external_cab_probe:
        host_sources = {str(item.get("source")) for item in external_cab_probe.get("cab_hosts", [])}
        for record in external_cab_probe.get("records", []):
            if str(record.get("source")) not in host_sources:
                continue
            source_bundles.append({
                "relative_path": record.get("source"),
                "bytes": record.get("bytes"),
                "sha256": record.get("sha256"),
                "status": record.get("status"),
            })

    unique_bundles: dict[tuple[Any, Any], dict[str, Any]] = {}
    for record in source_bundles:
        unique_bundles[(record.get("sha256"), record.get("relative_path"))] = record
    source_bundles = list(unique_bundles.values())

    resolved_cabs = {
        str(item.get("serialized_file"))
        for item in (external_cab_probe or {}).get("cab_hosts", [])
        if item.get("serialized_file")
    }
    unresolved_external = sorted(cab for cab in refs if cab not in resolved_cabs)
    scripts_closed = bool(
        monoscript_closure
        and monoscript_closure.get("status") == "passed_pc_monoscript_object_closure"
        and not monoscript_closure.get("missing_path_ids")
        and not monoscript_closure.get("failures")
    )
    passed = not unresolved_external and scripts_closed
    limitations = [
        "effect shader execution and runtime invocation are not proven by static exports",
        "no Android assets or legacy RVA are used",
    ]
    if not scripts_closed:
        limitations.insert(0, "skill prefab MonoBehaviour TypeTrees are not fully closed against current PC MonoScripts")
    report = {
        "schema_version": 2,
        "status": "passed_pc_static_skill_effect_join" if passed else "partial_pc_static_skill_effect_join",
        "operator_id": args.operator_id,
        "skill_id": args.skill_id,
        "platform": "windows-x64",
        "source_bundles": source_bundles,
        "skill_prefab": {
            "entry_count": len(skill_entries),
            "entries": skill_entries,
            "export_json_count": len(list(args.skill_export.rglob("*.json"))),
            "external_refs_from_export": sorted(refs),
            "unresolved_external_refs": unresolved_external,
        },
        "resolved_monoscripts": {
            "status": (monoscript_closure or {}).get("status", "not_provided"),
            "objects": (monoscript_closure or {}).get("objects", []),
            "external_cab_status": (external_cab_probe or {}).get("status", "not_provided"),
            "external_cab_hosts": (external_cab_probe or {}).get("cab_hosts", []),
            "missing_path_ids": (external_cab_probe or {}).get("missing_path_ids", []),
        },
        "s2_effects": {
            "entry_count": len(effect_entries),
            "object_type_counts": dict(sorted(effect_types.items())),
            "probe_export_type_counts": next(iter(effect_records.values()), {}).get("export_type_counts", {}),
            "containers": effect_containers,
            "wave_and_flow_entries": wave_entries,
            "export_json_count": len(list(args.effects_export.rglob("*.json"))),
            "material_evidence": [
                {"name": entry.get("Name"), "container": entry.get("Container"), "path_id": entry.get("PathID")}
                for entry in effect_entries if entry.get("Name") == "s2_wave01"
            ],
        },
        "current_pc_code_path": {
            "verified_edge_count": len(code_edge_subset),
            "edges": code_edge_subset,
            "runtime_invocation_proven": False,
        },
        "limitations": limitations,
        "evidence_policy": {
            "pc_only": True,
            "android_substitution": False,
            "legacy_rva_as_current": False,
            "runtime_execution": False,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "output": str(args.output), "skill_entries": len(skill_entries), "effect_entries": len(effect_entries), "external_refs": len(unresolved_external), "scripts_closed": scripts_closed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
