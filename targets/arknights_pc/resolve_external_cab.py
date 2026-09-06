from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def hit_record(obj: object) -> dict[str, object]:
    record: dict[str, object] = {
        "path_id": obj.path_id,
        "type": obj.type.name,
        "serialized_file": getattr(obj.assets_file, "name", ""),
    }
    if obj.type.name == "MonoScript":
        script = obj.read()
        record["monoscript"] = {
            "name": getattr(script, "m_Name", ""),
            "class_name": getattr(script, "m_ClassName", ""),
            "namespace": getattr(script, "m_Namespace", ""),
            "assembly_name": getattr(script, "m_AssemblyName", ""),
        }
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve an Arknights external CAB against an explicit bounded file set")
    parser.add_argument("--pc-root", type=Path, required=True)
    parser.add_argument("--cab", required=True)
    parser.add_argument("--path-id", type=int, action="append", default=[])
    parser.add_argument("--file", type=Path, action="append", default=[])
    parser.add_argument("--directory", type=Path, action="append", default=[])
    parser.add_argument("--max-candidates", type=int, default=512)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    pc_root = args.pc_root.resolve()
    if not pc_root.is_dir():
        raise FileNotFoundError(pc_root)
    sources = [path.resolve() for path in args.file]
    for directory in args.directory:
        directory = directory.resolve()
        try:
            directory.relative_to(pc_root)
        except ValueError as exc:
            raise ValueError(f"Candidate directory is outside the PC root: {directory}") from exc
        if not directory.is_dir():
            raise FileNotFoundError(directory)
        sources.extend(
            path.resolve() for path in directory.iterdir()
            if path.is_file() and path.suffix.casefold() in {".ab", ".bin"}
        )
    sources = sorted(set(sources), key=lambda path: str(path).casefold())
    if not sources:
        raise ValueError("At least one --file or --directory candidate is required")
    if len(sources) > args.max_candidates:
        raise ValueError(f"Candidate count {len(sources)} exceeds --max-candidates {args.max_candidates}")
    for source in sources:
        try:
            source.relative_to(pc_root)
        except ValueError as exc:
            raise ValueError(f"Candidate file is outside the PC root: {source}") from exc
        if not source.is_file():
            raise FileNotFoundError(source)

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "arknights"))
    from vendor.ark_unpacker_lz4ak import install_unitypy_patch

    install_unitypy_patch()
    import UnityPy

    records = []
    target_ids = set(args.path_id)
    for source in sources:
        record = {
            "source": str(source),
            "bytes": source.stat().st_size if source.is_file() else None,
            "sha256": sha256(source) if source.is_file() else None,
            "serialized_files": [],
            "path_id_hits": [],
            "status": "pending",
            "error": None,
        }
        try:
            env = UnityPy.load(str(source))
            serialized = sorted({getattr(obj.assets_file, "name", "") for obj in env.objects})
            record["serialized_files"] = serialized
            record["path_id_hits"] = [hit_record(obj) for obj in env.objects if obj.path_id in target_ids]
            record["status"] = "parsed"
        except Exception as exc:
            record["status"] = "failed"
            record["error"] = f"{type(exc).__name__}: {exc}"
        records.append(record)

    cab_hosts = [
        {"source": record["source"], "serialized_file": name}
        for record in records for name in record["serialized_files"]
        if name.lower() == args.cab.lower()
    ]
    path_hits = [
        {"source": record["source"], **hit}
        for record in records for hit in record["path_id_hits"]
    ]
    hit_ids = {int(hit["path_id"]) for hit in path_hits}
    missing_path_ids = sorted(target_ids - hit_ids)
    resolved = bool(cab_hosts) and not missing_path_ids
    report = {
        "schema_version": 2,
        "status": "passed_external_cab_resolved" if resolved else "partial_external_cab_not_resolved",
        "platform": "windows-x64",
        "pc_root": str(pc_root),
        "target_cab": args.cab,
        "target_path_ids": args.path_id,
        "candidate_count": len(records),
        "cab_hosts": cab_hosts,
        "path_id_hits": path_hits,
        "missing_path_ids": missing_path_ids,
        "records": records,
        "policy": {"bounded_inputs_only": True, "android_substitution": False, "runtime_execution": False},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "candidate_count": len(records), "cab_hosts": len(cab_hosts), "path_id_hits": len(path_hits), "missing_path_ids": len(missing_path_ids), "output": str(args.output)}))
    return 0 if resolved else 3


if __name__ == "__main__":
    raise SystemExit(main())
