from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def pptrs(value: Any, field: str = "$") -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        if "m_FileID" in value and "m_PathID" in value:
            yield {
                "field": field,
                "file_id": int(value["m_FileID"]),
                "path_id": int(value["m_PathID"]),
            }
        for key, child in value.items():
            yield from pptrs(child, f"{field}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from pptrs(child, f"{field}[{index}]")


def semantic_strings(value: Any, field: str = "$") -> Iterable[dict[str, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_field = f"{field}.{key}"
            if isinstance(child, str) and child:
                yield {"field": child_field, "value": child}
            else:
                yield from semantic_strings(child, child_field)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from semantic_strings(child, f"{field}[{index}]")


def byte_hex(value: bytes | None) -> str | None:
    return value.hex().upper() if value is not None else None


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "object"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve stripped PC MonoBehaviour TypeTrees using exact PC MonoScript dependencies"
    )
    parser.add_argument("--pc-root", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--dependency", type=Path, action="append", default=[])
    parser.add_argument("--path-id", type=int, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    pc_root = args.pc_root.resolve()
    sources = [args.bundle.resolve(), *(path.resolve() for path in args.dependency)]
    for source in sources:
        if not source.is_file():
            raise FileNotFoundError(source)
        try:
            source.relative_to(pc_root)
        except ValueError as exc:
            raise ValueError(f"Source is outside the declared PC root: {source}") from exc
    if args.output.exists():
        raise FileExistsError(f"Refusing existing output: {args.output}")
    args.output.mkdir(parents=True)

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "arknights"))
    from vendor.ark_unpacker_lz4ak import install_unitypy_patch

    install_unitypy_patch()
    import UnityPy

    main_environment = UnityPy.load(str(args.bundle.resolve()))
    main_serialized_files = {getattr(reader.assets_file, "name", "") for reader in main_environment.objects}
    main_environment.files.clear()
    environment = UnityPy.load(*(str(path) for path in sources))
    target_ids = set(args.path_id)
    found_ids: set[int] = set()
    objects: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for reader in environment.objects:
        if reader.type.name != "MonoBehaviour" or reader.path_id not in target_ids:
            continue
        if getattr(reader.assets_file, "name", "") not in main_serialized_files:
            continue
        found_ids.add(reader.path_id)
        try:
            behaviour = reader.read()
            script = behaviour.m_Script.read()
            tree = reader.read_typetree()
        except Exception as exc:
            failures.append({
                "path_id": reader.path_id,
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue

        full_name = ".".join(
            part for part in (getattr(script, "m_Namespace", ""), getattr(script, "m_ClassName", "")) if part
        )
        tree_path = args.output / "objects" / f"{safe_name(full_name)}__pathid_{reader.path_id}.json"
        tree_path.parent.mkdir(parents=True, exist_ok=True)
        tree_path.write_text(json.dumps(tree, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        serialized_type = reader.serialized_type
        strings = list(semantic_strings(tree))
        objects.append({
            "path_id": reader.path_id,
            "serialized_file": getattr(reader.assets_file, "name", ""),
            "script_pointer": {
                "file_id": int(behaviour.m_Script.file_id),
                "path_id": int(behaviour.m_Script.path_id),
            },
            "script": {
                "name": getattr(script, "m_Name", ""),
                "class_name": getattr(script, "m_ClassName", ""),
                "namespace": getattr(script, "m_Namespace", ""),
                "assembly_name": getattr(script, "m_AssemblyName", ""),
                "full_name": full_name,
            },
            "serialized_type": {
                "script_type_index": serialized_type.script_type_index,
                "script_id": byte_hex(serialized_type.script_id),
                "old_type_hash": byte_hex(serialized_type.old_type_hash),
            },
            "type_tree_file": str(tree_path),
            "type_tree_field_count": len(tree),
            "pptrs": list(pptrs(tree)),
            "semantic_strings": strings,
            "effect_keys": [
                item for item in strings
                if "effect" in item["field"].casefold() or "effect" in item["value"].casefold()
            ],
        })

    missing_ids = sorted(target_ids - found_ids)
    passed = not missing_ids and not failures and len(objects) == len(target_ids)
    report = {
        "schema_version": 1,
        "status": "passed_pc_monoscript_object_closure" if passed else "partial_pc_monoscript_object_closure",
        "platform": "windows-x64",
        "pc_root": str(pc_root),
        "sources": [
            {"path": str(source), "bytes": source.stat().st_size, "sha256": sha256(source)}
            for source in sources
        ],
        "target_path_ids": sorted(target_ids),
        "missing_path_ids": missing_ids,
        "failures": failures,
        "objects": sorted(objects, key=lambda item: int(item["path_id"])),
        "policy": {
            "pc_only": True,
            "android_substitution": False,
            "legacy_rva_as_current": False,
            "runtime_execution": False,
        },
    }
    report_path = args.output / "pc_monoscript_object_closure.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": report["status"],
        "objects": len(objects),
        "missing": len(missing_ids),
        "failures": len(failures),
        "output": str(report_path),
    }))
    return 0 if passed else 3


if __name__ == "__main__":
    raise SystemExit(main())
