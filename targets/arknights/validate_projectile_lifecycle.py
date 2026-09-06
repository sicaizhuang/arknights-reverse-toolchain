#!/usr/bin/env python3
"""Static projectile/reanimation lifecycle diagnostic.

Serialized Unity data can prove dependency shape and timing fields, but not the
native order of remove/free calls.  The report therefore keeps runtime
cleanup explicitly unverified instead of upgrading a resource closure into a
runtime claim.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


PATH_ID_RE = re.compile(r"_(?P<path>-?\d+)(?:_|\.)")


def load_objects(root: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    objects: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for path in sorted(root.rglob("*.json")):
        if path.name in {"resource_graph.json", "ark_unpacker_probe.json"}:
            continue
        match = PATH_ID_RE.search(path.name)
        if not match:
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            errors.append(f"json_decode_failed:{path.name}:{type(exc).__name__}")
            continue
        if isinstance(value, dict):
            objects[match.group("path")] = {"path": str(path), "data": value}
    return objects, errors


def walk(value: Any, prefix: str = "$"):
    yield prefix, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from walk(child, f"{prefix}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, f"{prefix}[{index}]")


def component_ids(game_object: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for _, value in walk(game_object.get("m_Component", [])):
        if isinstance(value, dict) and "m_PathID" in value:
            path_id = value.get("m_PathID")
            if isinstance(path_id, (int, str)) and str(path_id) != "0":
                result.append(str(path_id))
    return result


def find_scalar(data: Any, wanted: set[str]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for path, value in walk(data):
        if not path or not isinstance(value, (str, int, float, bool)):
            continue
        key = re.sub(r"\[\d+\]$", "", path.rsplit(".", 1)[-1]).casefold()
        if key in wanted:
            matches.append({"field": path, "value": value})
    return matches


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-root", type=Path, required=True)
    parser.add_argument("--root-name", default="projectile_magic_ball")
    parser.add_argument("--root-path-id")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    root = args.probe_root.resolve()
    objects, errors = load_objects(root)
    candidates = []
    for path_id, item in objects.items():
        data = item["data"]
        if data.get("m_Name") == args.root_name or args.root_name in Path(item["path"]).stem:
            candidates.append((path_id, item))
    if args.root_path_id:
        candidates = [(args.root_path_id, objects[args.root_path_id])] if args.root_path_id in objects else []

    report: dict[str, Any] = {
        "schema": "projectile_lifecycle_validation.v1",
        "probe_root": str(root),
        "root_name": args.root_name,
        "root_candidates": [],
        "object_count": len(objects),
        "errors": errors,
        "static": {
            "projectile_component": False,
            "motion_component": False,
            "effect_emitter_component": False,
            "main_effect": [],
            "hit_effects": [],
            "lifetime": [],
            "max_hit_num": [],
            "speed": [],
            "stop_policy": {},
        },
        "lifecycle_contract": {
            "logical_remove_flag": "unverified_native_offset",
            "visual_reanim_handle_clear": "unverified_native_offset_0x7c",
            "object_release_once": "unverified_runtime",
            "serialized_stop_policy": "missing",
            "runtime_cases": ["timeout", "hit", "target_invalid", "continuous_fire", "owner_death", "stage_end"],
        },
        "status": "blocked_missing_root_or_runtime_lifecycle_evidence",
    }
    if len(candidates) != 1:
        report["errors"].append("root_not_unique")
    else:
        path_id, item = candidates[0]
        root_data = item["data"]
        component_paths = component_ids(root_data)
        component_rows = []
        for component_path in component_paths:
            component = objects.get(component_path)
            if not component:
                report["errors"].append(f"missing_component:{component_path}")
                continue
            component_data = component["data"]
            component_rows.append({"path_id": component_path, "file": component["path"]})
            keys = {str(key).casefold() for key in component_data}
            stop_fields = {
                "_clearmaineffectwhenreached",
                "_stopgraphicprojectilewhenclearmain effect".replace(" ", ""),
                "_stopaftermaxhit",
                "_stopafterfirsthit",
                "_stopwhensourceinvalid",
                "_forcereachedwhentimeup",
                "_cleartracetargetwhenreached",
            }
            for match in find_scalar(component_data, stop_fields):
                leaf = match["field"].rsplit(".", 1)[-1]
                leaf = re.sub(r"\[\d+\]$", "", leaf)
                report["static"]["stop_policy"].setdefault(leaf, []).append(match)
            if "_maineffect" in keys:
                report["static"]["projectile_component"] = True
                report["static"]["main_effect"] = find_scalar(component_data, {"_maineffect"})
                report["static"]["lifetime"] = find_scalar(component_data, {"_lifetime"})
                report["static"]["max_hit_num"] = find_scalar(component_data, {"_maxhitnum"})
            if "_speed" in keys and "_movetype" in keys:
                report["static"]["motion_component"] = True
                report["static"]["speed"] = find_scalar(component_data, {"_speed"})
            if "_effectswhenhit" in keys:
                report["static"]["effect_emitter_component"] = True
                report["static"]["hit_effects"] = find_scalar(component_data, {"_effectswhenhit"})
        report["root_candidates"] = [{"path_id": path_id, "file": item["path"], "components": component_rows}]
        required_static_ok = all(
            report["static"][name]
            for name in ("projectile_component", "motion_component", "effect_emitter_component", "main_effect", "hit_effects", "lifetime", "max_hit_num", "speed")
        )
        policy = report["static"]["stop_policy"]
        required_policy = {
            "_clearMainEffectWhenReached",
            "_stopAfterMaxHit",
            "_stopAfterFirstHit",
            "_stopWhenSourceInvalid",
            "_forceReachedWhenTimeup",
        }
        policy_names = set(policy)
        policy_complete = required_policy.issubset(policy_names)
        report["lifecycle_contract"]["serialized_stop_policy"] = "present" if policy_complete else "partial"
        if required_static_ok:
            report["status"] = "static_dependencies_ok_runtime_lifecycle_unverified"
        else:
            report["errors"].append("required_static_component_missing")

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "static_dependencies_ok_runtime_lifecycle_unverified" else 2


if __name__ == "__main__":
    sys.exit(main())
