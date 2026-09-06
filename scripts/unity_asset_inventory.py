"""Inspect Unity containers and perform bounded, hash-verified sample exports."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

SUPPORTED_OBJECT_TYPES = {
    "Texture2D",
    "Sprite",
    "TextAsset",
    "AnimationClip",
    "AudioClip",
    "Mesh",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def safe_name(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "_", value).strip("._")
    return cleaned[:100] or fallback


def classify(path: Path, prefix: bytes) -> str:
    suffix = path.suffix.lower()
    if prefix.startswith((b"UnityFS", b"UnityRaw", b"UnityWeb")):
        return "unity_container"
    if suffix == ".assets":
        return "unity_serialized_file"
    if suffix in {".bundle", ".ab"}:
        return "unity_bundle_candidate"
    if suffix in {".skel", ".atlas", ".spine"}:
        return "spine"
    if suffix in {".acb", ".awb", ".hca", ".usm"} or prefix.startswith((b"@UTF", b"AFS2", b"CRID")):
        return "cri"
    if suffix in {".dat", ".bin", ".pack", ".bytes"}:
        return "custom_container_candidate"
    if suffix in {".png", ".jpg", ".jpeg", ".tga", ".astc", ".ktx"}:
        return "texture_file"
    if suffix in {".wav", ".mp3", ".ogg"}:
        return "audio_file"
    return "other"


def output_records(paths: list[Path]) -> list[dict[str, Any]]:
    return [
        {"path": str(path), "size": path.stat().st_size, "sha256": sha256(path)}
        for path in sorted(paths)
        if path.is_file()
    ]


def export_object(reader: Any, object_type: str, export_root: Path) -> dict[str, Any]:
    from UnityPy.tools import extractor  # type: ignore

    path_id = int(getattr(reader, "path_id", 0))
    object_name = ""
    try:
        object_name = str(getattr(reader.parse_as_object(), "m_Name", ""))
    except Exception:
        object_name = ""
    stem = f"{path_id}_{safe_name(object_name, object_type)}"
    type_root = export_root / object_type
    type_root.mkdir(parents=True, exist_ok=True)
    base = type_root / stem
    before = set(type_root.rglob("*"))
    mode = "decoded"
    try:
        if object_type == "Texture2D":
            extractor.exportTexture2D(reader, str(base))
        elif object_type == "Sprite":
            extractor.exportSprite(reader, str(base))
        elif object_type == "TextAsset":
            extractor.exportTextAsset(reader, str(base))
        elif object_type == "AudioClip":
            extractor.exportAudioClip(reader, str(base))
        elif object_type == "Mesh":
            extractor.exportMesh(reader, str(base))
        elif object_type == "AnimationClip":
            raw_path = base.with_suffix(".serialized.bin")
            raw_path.write_bytes(reader.get_raw_data())
            mode = "raw_serialized_object"
        else:
            raise ValueError(f"Unsupported object type: {object_type}")
        after = set(type_root.rglob("*"))
        created = [path for path in after - before if path.is_file()]
        if not created:
            raise RuntimeError("exporter returned without creating an output file")
        return {
            "object_type": object_type,
            "path_id": path_id,
            "name": object_name,
            "status": "success",
            "export_mode": mode,
            "outputs": output_records(created),
            "failure_reason": None,
        }
    except Exception as exc:
        return {
            "object_type": object_type,
            "path_id": path_id,
            "name": object_name,
            "status": "failed",
            "export_mode": mode,
            "outputs": [],
            "failure_reason": f"{type(exc).__name__}: {exc}",
        }


def inspect_unity(path: Path, export_root: Path, max_per_type: int) -> dict[str, Any]:
    import UnityPy  # type: ignore

    result: dict[str, Any] = {
        "parse_status": "failed",
        "object_count": 0,
        "object_types": {},
        "unity_versions": [],
        "exports": [],
        "failure_reason": None,
    }
    try:
        environment = UnityPy.load(str(path))
        readers = list(environment.objects)
        type_counts: dict[str, int] = {}
        versions: set[str] = set()
        for asset in getattr(environment, "assets", {}).values():
            version = getattr(asset, "unity_version", None)
            if version:
                versions.add(str(version))
        exported_counts: dict[str, int] = {}
        exports: list[dict[str, Any]] = []
        for reader in readers:
            object_type = getattr(getattr(reader, "type", None), "name", str(getattr(reader, "type", "unknown")))
            type_counts[object_type] = type_counts.get(object_type, 0) + 1
            if object_type not in SUPPORTED_OBJECT_TYPES:
                continue
            count = exported_counts.get(object_type, 0)
            if max_per_type >= 0 and count >= max_per_type:
                continue
            exports.append(export_object(reader, object_type, export_root))
            exported_counts[object_type] = count + 1
        result.update(
            {
                "parse_status": "success",
                "object_count": len(readers),
                "object_types": type_counts,
                "unity_versions": sorted(versions),
                "exports": exports,
            }
        )
    except Exception as exc:
        result["failure_reason"] = f"{type(exc).__name__}: {exc}"
    return result


def run(input_root: Path, output: Path, max_files: int, max_per_type: int) -> dict[str, Any]:
    input_root = input_root.resolve()
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"Refusing existing output: {output}")
    output.mkdir(parents=True)
    items: list[dict[str, Any]] = []
    unity_attempts = 0
    for path in sorted(p for p in input_root.rglob("*") if p.is_file()):
        with path.open("rb") as stream:
            prefix = stream.read(64)
        kind = classify(path, prefix)
        if kind == "other":
            continue
        record: dict[str, Any] = {
            "source_path": str(path),
            "relative_path": path.relative_to(input_root).as_posix(),
            "file_type": kind,
            "size": path.stat().st_size,
            "raw_sha256": sha256(path),
            "parse_status": "not_attempted",
            "export_status": "not_attempted",
            "failure_reason": None,
        }
        if kind in {"unity_container", "unity_serialized_file", "unity_bundle_candidate"}:
            if max_files >= 0 and unity_attempts >= max_files:
                record.update({"parse_status": "skipped_by_file_cap", "failure_reason": "max_files limit reached"})
            else:
                unity_attempts += 1
                file_export_root = output / "exports" / f"{record['raw_sha256'][:12]}_{safe_name(path.name, 'asset')}"
                details = inspect_unity(path, file_export_root, max_per_type)
                record.update(details)
                exports = details.get("exports", [])
                if details["parse_status"] == "success":
                    failures = sum(item["status"] == "failed" for item in exports)
                    successes = sum(item["status"] == "success" for item in exports)
                    record["export_status"] = "success" if successes and not failures else "partial" if successes else "no_supported_objects"
                else:
                    record["export_status"] = "failed"
        elif kind in {"spine", "cri"}:
            record.update({"parse_status": "detected", "export_status": "unsupported", "failure_reason": f"{kind} decoder not configured"})
        elif kind == "custom_container_candidate":
            record.update({"parse_status": "unverified", "export_status": "unsupported", "failure_reason": "custom compression or encryption is possible; no verified decoder"})
        else:
            record.update({"parse_status": "recognized", "export_status": "already_external_format"})
        items.append(record)

    export_records = [export for item in items for export in item.get("exports", [])]
    report = {
        "schema_version": 2,
        "status": "partial" if items else "no_supported_assets",
        "input_root": str(input_root),
        "limits": {"max_unity_files": max_files, "max_exports_per_type_per_file": max_per_type},
        "items": items,
        "summary": {
            "detected_files": len(items),
            "unity_parse_success": sum(item.get("parse_status") == "success" for item in items),
            "unity_parse_failed": sum(item.get("parse_status") == "failed" for item in items),
            "object_exports_success": sum(item.get("status") == "success" for item in export_records),
            "object_exports_failed": sum(item.get("status") == "failed" for item in export_records),
            "exported_object_types": sorted({item["object_type"] for item in export_records if item.get("status") == "success"}),
        },
    }
    (output / "unity_asset_export.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_root", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--max-files", type=int, default=-1)
    parser.add_argument("--max-exports-per-type", type=int, default=3)
    args = parser.parse_args()
    report = run(args.input_root, args.output, args.max_files, args.max_exports_per_type)
    print(json.dumps({"status": report["status"], **report["summary"]}))
    if report["summary"]["unity_parse_failed"] and not report["summary"]["unity_parse_success"]:
        sys.exit(4)


if __name__ == "__main__":
    main()
